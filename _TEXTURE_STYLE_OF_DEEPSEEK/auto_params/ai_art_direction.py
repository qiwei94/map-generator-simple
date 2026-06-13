"""AI art direction: style strategy for new cities (Layer 2, Spec §10.2).

Called once for new cities to get strategic emphasis/de-emphasis decisions
before the rules engine runs. Outputs param_overrides that feed into
resolve_params(user_overrides=...).
"""

import base64
import json
import os
from typing import Optional

from .city_profile import CityProfile


_ART_DIRECTION_PROMPT = """你是一位 3D 打印城市地图的艺术总监。

城市: {city_name}
城市特征:
{profile_summary}

这是一个城市 3D 打印摆件项目。目标风格：手绘砖石质感，层次分明。

基于这座城市的地理特征，请给出艺术指导：
1. 应该强调什么元素？（地形/水体/建筑/道路/植被）
2. 应该弱化什么元素？
3. 风格建议（一句话描述这座城市的地图应该呈现什么感觉）
4. 参数建议

可调参数及范围：
- z_gamma: 0.25-0.70 (越小地形越平缓)
- building_v2_road_tier: 2-5 (越小保留的路越少)
- road_width_multiplier: 2.0-8.0 (路的粗细)
- vegetation_min_area_m2: 1000-50000 (越大过滤越多小植被)
- building_density_threshold: 0.001-0.05 (越大建筑越少)

输出严格 JSON:
{{
  "emphasis": ["元素1", "元素2"],
  "de_emphasis": ["元素1"],
  "style_notes": "一句话风格描述",
  "param_overrides": {{
    "参数名": 数值
  }}
}}
"""


def ai_art_direction(
    profile: CityProfile,
    city_name: str,
    reference_pngs: Optional[list[str]] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Get AI art direction for a city.

    Returns dict with keys: emphasis, de_emphasis, style_notes, param_overrides.
    If API unavailable, returns empty dict (no-op).
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("[ai_art_direction] ANTHROPIC_API_KEY not set, skipping")
        return {}

    try:
        import anthropic
    except ImportError:
        print("[ai_art_direction] anthropic package not installed")
        return {}

    profile_summary = "\n".join(
        f"  {k}: {v}" for k, v in profile.to_dict().items()
    )

    prompt = _ART_DIRECTION_PROMPT.format(
        city_name=city_name,
        profile_summary=profile_summary,
    )

    content = [{"type": "text", "text": prompt}]

    # Attach reference PNGs if provided
    if reference_pngs:
        for png_path in reference_pngs[:3]:
            if os.path.exists(png_path):
                with open(png_path, "rb") as f:
                    img_data = base64.standard_b64encode(f.read()).decode()
                content.insert(0, {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_data,
                    },
                })

    client = anthropic.Anthropic(api_key=key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text
    return _parse_art_direction(raw)


def _parse_art_direction(raw: str) -> dict:
    """Parse art direction JSON response."""
    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]

        data = json.loads(text)
        return {
            "emphasis": data.get("emphasis", []),
            "de_emphasis": data.get("de_emphasis", []),
            "style_notes": data.get("style_notes", ""),
            "param_overrides": data.get("param_overrides", {}),
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}
