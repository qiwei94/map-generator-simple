"""AI vision review: score PNG output and suggest parameter adjustments.

Layer 1 of the AI aesthetic evaluation system (Spec §10.1).
Uses Claude Sonnet via the Anthropic SDK for structured vision analysis.
"""

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .city_profile import CityProfile
from .param_resolver import ResolvedParams


@dataclass
class ReviewResult:
    """Structured output from AI review."""
    scores: dict = field(default_factory=dict)
    overall: int = 0
    issues: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    raw_response: str = ""


# ─── Parameter bounds for clamping AI suggestions ────────────────────
_PARAM_BOUNDS = {
    "z_gamma": (0.25, 0.70),
    "terrain_thickness_mm": (3.0, 6.0),
    "building_density_threshold": (0.001, 0.05),
    "building_print_limit_m2": (1000.0, 8000.0),
    "road_width_multiplier": (2.0, 8.0),
    "vegetation_min_area_m2": (1000.0, 50000.0),
    "building_v2_road_tier": (2, 5),
    "brick_perlin_amp": (2.0, 8.0),
    "elevation_smoothing_sigma": (1.0, 5.0),
}

_REVIEW_PROMPT = """你是一位地图艺术品的视觉审美评审。
这是一张 {city_name} 的 3D 打印城市地图预览图（PNG 俯视渲染）。
目标风格：手绘砖石质感，层次分明，适合作为桌面摆件。

当前生成参数：
{params_summary}

城市特征：
{profile_summary}

请对以下维度打分（1-5）并给出具体修改建议：
1. 构图平衡 (balance) — 信息分布是否均衡
2. 信息密度 (density) — 太空(=1)还是太挤(=5)
3. 层次可读性 (readability) — 路/建筑/水/地形能否一眼分清
4. 风格一致性 (style) — 砖石纹理在当前比例下是否自然
5. 主体突出度 (emphasis) — 主要水体/地标是否足够显眼
6. 整体美感 (overall) — 直觉审美判断

输出严格 JSON（不要 markdown code fence）：
{{
  "scores": {{"balance": N, "density": N, "readability": N, "style": N, "emphasis": N, "overall": N}},
  "issues": ["问题1", "问题2"],
  "suggestions": [
    {{"param": "参数名", "direction": "increase/decrease", "magnitude": "slight/moderate/strong", "reason": "原因"}}
  ]
}}

约束：
- suggestions 最多 3 条
- param 必须是以下之一: z_gamma, terrain_thickness_mm, building_density_threshold, building_print_limit_m2, road_width_multiplier, vegetation_min_area_m2, building_v2_road_tier, brick_perlin_amp, elevation_smoothing_sigma
- direction 只能是 increase 或 decrease
"""


def ai_review_png(
    png_path: str,
    profile: CityProfile,
    current_params: ResolvedParams,
    city_name: str = "unknown",
    max_rounds: int = 3,
    api_key: Optional[str] = None,
) -> tuple[ResolvedParams, ReviewResult]:
    """Run AI review loop.

    Returns (adjusted_params, final_review).
    If API key is unavailable, returns (current_params, empty ReviewResult).
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("[ai_review] ANTHROPIC_API_KEY not set, skipping AI review")
        return current_params, ReviewResult()

    params = current_params
    last_review = ReviewResult()

    for round_idx in range(max_rounds):
        review = _call_review(png_path, profile, params, city_name, key)
        last_review = review

        if review.overall >= 4:
            print(f"[ai_review] Round {round_idx + 1}: score={review.overall} ≥ 4, passed")
            break

        print(f"[ai_review] Round {round_idx + 1}: score={review.overall} < 4, "
              f"applying {len(review.suggestions)} suggestions")

        params = _apply_suggestions(params, review.suggestions)

        # In a real implementation, we'd re-generate PNG here and re-review.
        # For now, we apply suggestions and break after first adjustment.
        break

    return params, last_review


def _call_review(
    png_path: str,
    profile: CityProfile,
    params: ResolvedParams,
    city_name: str,
    api_key: str,
) -> ReviewResult:
    """Call Claude API with the PNG for structured review."""
    try:
        import anthropic
    except ImportError:
        print("[ai_review] anthropic package not installed")
        return ReviewResult()

    with open(png_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    params_summary = "\n".join(
        f"  {k}: {v}" for k, v in params.to_dict().items()
        if not k.startswith("_")
    )
    profile_summary = "\n".join(
        f"  {k}: {v}" for k, v in profile.to_dict().items()
    )

    prompt = _REVIEW_PROMPT.format(
        city_name=city_name,
        params_summary=params_summary,
        profile_summary=profile_summary,
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_data,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    raw = response.content[0].text
    return _parse_review(raw)


def _parse_review(raw: str) -> ReviewResult:
    """Parse JSON response from Claude."""
    try:
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]

        data = json.loads(text)
        return ReviewResult(
            scores=data.get("scores", {}),
            overall=data.get("scores", {}).get("overall", 0),
            issues=data.get("issues", []),
            suggestions=data.get("suggestions", []),
            raw_response=raw,
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return ReviewResult(raw_response=raw)


def _apply_suggestions(
    params: ResolvedParams,
    suggestions: list[dict],
) -> ResolvedParams:
    """Apply AI suggestions with bounds clamping."""
    import copy
    adjusted = copy.deepcopy(params)

    _MAGNITUDE_MAP = {"slight": 0.1, "moderate": 0.25, "strong": 0.4}

    for sug in suggestions[:3]:
        param_name = sug.get("param", "")
        direction = sug.get("direction", "")
        magnitude = sug.get("magnitude", "moderate")

        if not hasattr(adjusted, param_name):
            continue
        if param_name not in _PARAM_BOUNDS:
            continue

        current = getattr(adjusted, param_name)
        lo, hi = _PARAM_BOUNDS[param_name]
        factor = _MAGNITUDE_MAP.get(magnitude, 0.25)

        if direction == "increase":
            new_val = current + (hi - current) * factor
        elif direction == "decrease":
            new_val = current - (current - lo) * factor
        else:
            continue

        # Clamp
        if isinstance(current, int):
            new_val = int(round(max(lo, min(hi, new_val))))
        else:
            new_val = max(lo, min(hi, new_val))

        setattr(adjusted, param_name, new_val)
        adjusted._reasons[param_name] = (
            f"AI review: {direction} {magnitude} "
            f"({current} → {new_val}, reason: {sug.get('reason', '')})"
        )

    return adjusted
