"""Aesthetic Loop — 城市地图美学闭环自动调参.

「生成 → 评审 → 调参 → 再生成」闭环，B 内核（可计算指标）+ A/C 可插拔（VLM/CLIP）。

设计原则（与主线计划的 5 项锁定决策一致）:
- 独立目录，只读复用 _TEXTURE_STYLE_OF_DEEPSEEK / auto_params，不改存量行为
- 参数注入走运行时猴补丁 + 显式 override（消费端均已函数级 import）
- 地形类参数前置定死，闭环只迭代便宜的图层/渲染参数
- 评审锚定参考作品（city_demo）
"""

from .presets import CityPreset, get_preset, list_presets
from .loop import AestheticLoop, LoopResult

__all__ = [
    "CityPreset",
    "get_preset",
    "list_presets",
    "AestheticLoop",
    "LoopResult",
]
