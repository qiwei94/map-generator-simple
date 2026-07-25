"""可插拔 VLM 评审（A 路线增强，非闭环必需）.

有 ANTHROPIC_API_KEY 且显式开启时，把评审图包 + 参考作品发给视觉模型，
返回 0-10 整体分，与 B 内核指标分按权重混合。
无 key / 无 anthropic 包 / 任何异常 → 返回 None（闭环回退纯指标）。
"""

import base64
import json
import os

_PROMPT = """你是 3D 打印城市地图的审美评审。目标风格对标 Reso/lution Urban Series：
白建筑 / 灰地形 / 纯黑水，三色分离锐利；建筑肌理连续规整；市中心高度错落有致。

{city_line}{ref_line}
两张生成图：第一张俯视（评密度/规整/水体），第二张高度场明暗图（评高度错落）。

输出严格 JSON（不要 markdown fence）：
{{"overall": N, "issues": ["问题1", "问题2"]}}
overall 为 0-10 整体美感分（参考作品水准 = 9-10）。"""


def _img_block(path: str) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    media = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return {"type": "image",
            "source": {"type": "base64", "media_type": media, "data": data}}


def vlm_score(bundle: dict, reference_images: list, city_name: str,
              api_key: str = None) -> dict | None:
    """返回 {"overall": 0-10, "issues": [...]} 或 None（不可用）。"""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    content = []
    refs = [p for p in (reference_images or []) if os.path.exists(p)][:2]
    for p in refs:
        content.append(_img_block(p))
    ref_line = f"前 {len(refs)} 张是参考作品（目标效果）。" if refs else "无参考图。"
    content.append(_img_block(bundle["topdown"]))
    content.append(_img_block(bundle["height"]))
    content.append({"type": "text", "text": _PROMPT.format(
        city_line=f"城市: {city_name}。", ref_line=ref_line)})

    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=300,
            messages=[{"role": "user", "content": content}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        overall = float(data.get("overall", 0))
        overall = max(0.0, min(10.0, overall))
        return {"overall": overall, "issues": data.get("issues", [])}
    except Exception as e:
        print(f"  [vlm] scoring failed (fallback to metrics): {e}")
        return None
