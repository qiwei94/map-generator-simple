"""可插拔 VLM 评审（A 路线增强，非闭环必需）.

有 VLM_API_KEY 且 use_vlm=True 时，把评审图包 + 参考作品发给视觉模型，
返回 0-10 整体分，与 B 内核指标分按权重混合。
无 key / 无 openai 包 / 任何异常 → 返回 None（闭环回退纯指标）。

endpoint / model 硬编码（非秘密）；唯一秘密 VLM_API_KEY 从 .env 读取。
"""
from __future__ import annotations

import base64
import json
import os

# ─── 固定配置（非秘密，不入 .env）────────────────────────────────────
VLM_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
VLM_MODELS = [
    "qwen3.8-max-preview",  # 最强推理
    "qwen3.7-plus",         # 稳定 + 视觉
    "qwen3.6-flash",        # 快速
]
VLM_SPREAD_THRESHOLD = 3.0  # 模型间极差超过此值 → 标记 human_review

# ─── Prompt ─────────────────────────────────────────────────────────
_PROMPT = """\
你是 3D 打印城市地图的审美评审专家。目标风格对标 Reso/lution Urban Series：
白建筑 / 灰地形 / 纯黑水，三色分离锐利；建筑肌理连续规整；市中心高度错落有致。

{city_line}{ref_line}
接下来两张生成图：第一张俯视图（评密度/规整/水体形态），第二张高度场明暗图（评天际线错落）。

请从以下维度评分（0-10），并给出简短理由：
1. density: 建筑覆盖率是否饱满但不拥挤
2. regularity: 街区轮廓是否规整锐利（无狗啃/毛刺）
3. height_variation: 高度是否有层次（中心高外围低）
4. water_form: 水体形态是否自然清晰
5. overall: 整体美感（综合以上 + 对比参考作品）

输出严格 JSON（不要 markdown fence）：
{{"density": N, "regularity": N, "height_variation": N, "water_form": N, "overall": N, "issues": ["问题1", "问题2"]}}
overall 为 0-10 整体美感分（参考作品水准 = 9-10）。issues 列出最突出的 1-3 个缺陷。"""


def _load_env():
    """尝试加载项目根 .env（python-dotenv 可选依赖）。"""
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=False)
    except ImportError:
        pass


def _img_to_data_url(path: str) -> str:
    """图片文件 → OpenAI vision data URL。"""
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    media = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return f"data:{media};base64,{data}"


def vlm_score(bundle: dict, reference_images: list, city_name: str,
              api_key: str = None) -> dict | None:
    """VLM 美学评分。

    Args:
        bundle: render_review_bundle 返回的 dict（含 topdown/height 路径）
        reference_images: 参考作品图路径列表
        city_name: 城市名
        api_key: 显式传入（优先）或从 env VLM_API_KEY 读取

    Returns:
        {"overall": 0-10, "density": N, ..., "issues": [...]} 或 None（不可用）
    """
    _load_env()
    key = api_key or os.environ.get("VLM_API_KEY")
    if not key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        print("  [vlm] openai package not installed; pip install openai")
        return None

    # 构建 messages content（OpenAI vision 格式）
    content = []

    # 参考图（最多 2 张，控制 token）
    refs = [p for p in (reference_images or []) if os.path.exists(p)][:2]
    for p in refs:
        content.append({
            "type": "image_url",
            "image_url": {"url": _img_to_data_url(p), "detail": "low"},
        })
    ref_line = f"前 {len(refs)} 张是参考作品（目标效果）。" if refs else "无参考图。"

    # 生成图（俯视 + 高度场）
    for key_name in ("topdown", "height"):
        path = bundle.get(key_name)
        if path and os.path.exists(path):
            content.append({
                "type": "image_url",
                "image_url": {"url": _img_to_data_url(path), "detail": "high"},
            })

    # 文本 prompt
    content.append({
        "type": "text",
        "text": _PROMPT.format(
            city_line=f"城市: {city_name}。",
            ref_line=ref_line,
        ),
    })

    client = OpenAI(api_key=key, base_url=VLM_BASE_URL, timeout=120.0)
    models_result = {}

    for model in VLM_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=400,
                messages=[{"role": "user", "content": content}],
            )
            text = resp.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(text)

            overall = max(0.0, min(10.0, float(data.get("overall", 0))))
            entry = {"overall": overall}
            for dim in ("density", "regularity", "height_variation", "water_form"):
                if dim in data:
                    entry[dim] = max(0.0, min(10.0, float(data[dim])))
            entry["issues"] = data.get("issues", [])[:3]
            models_result[model] = entry
            print(f"  [vlm] {model}: overall={overall:.1f}")
        except json.JSONDecodeError as e:
            print(f"  [vlm] {model}: JSON parse failed: {e}")
        except Exception as e:
            print(f"  [vlm] {model}: failed ({e})")

    if not models_result:
        return None

    # 分歧检测：极差 > 阈值 → 标记 human_review，不自动混合
    overalls = [m["overall"] for m in models_result.values()]
    spread = max(overalls) - min(overalls)
    avg_overall = round(sum(overalls) / len(overalls), 2)

    result = {"overall": avg_overall, "models": models_result,
              "spread": round(spread, 2)}

    if spread > VLM_SPREAD_THRESHOLD:
        result["human_review"] = True
        print(f"  [vlm] ⚠ 模型分歧大 (spread={spread:.1f} > {VLM_SPREAD_THRESHOLD})"
              f"，标记 human_review，该轮 VLM 分不自动混合")
    else:
        result["human_review"] = False

    return result
