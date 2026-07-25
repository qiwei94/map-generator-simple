"""城市注册表：bbox、PBF、原型、参考作品图。

bbox 与 tmp/ 已提取的 osmium geojson 对齐（ Chicago 用 41.77 版，与
relief_studio / tmp 数据一致，非 generate_cli 的 41.76 版）。
"""

import os
from dataclasses import dataclass, field
from typing import List

from .config import CITY_DEMO_DIR

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class CityPreset:
    name: str
    bbox: tuple            # (south, west, north, east) WGS84
    pbf: str               # 相对项目根的 PBF 路径
    prototype: str         # skyline / landscape / classic
    reference_dir: str     # 相对 CITY_DEMO_DIR 的子目录名
    description: str = ""

    @property
    def pbf_abs(self) -> str:
        return os.path.join(_PROJECT_ROOT, self.pbf)

    @property
    def reference_images(self) -> List[str]:
        """返回存在的参考图路径（jpg/png）。"""
        d = os.path.join(CITY_DEMO_DIR, self.reference_dir)
        if not os.path.isdir(d):
            return []
        imgs = [
            os.path.join(d, f) for f in sorted(os.listdir(d))
            if f.lower().endswith((".jpg", ".jpeg", ".png")) and "卫星" not in f
        ]
        return imgs[:3]  # 最多 3 张，控制 VLM token


_PRESETS = {
    "chicago": CityPreset(
        name="chicago",
        bbox=(41.77, -87.77, 41.99, -87.47),
        pbf="pbf_cache/illinois-latest.osm.pbf",
        prototype="skyline",
        reference_dir="芝加哥",
        description="高密度网格 + 密歇根湖，天际线城市代表",
    ),
    "westlake": CityPreset(
        name="westlake",
        bbox=(30.13, 120.01, 30.36, 120.29),
        pbf="pbf_cache/zhejiang-latest.osm.pbf",
        prototype="landscape",
        reference_dir="杭州",
        description="西湖山水 + 钱塘江，低矮含蓄代表",
    ),
}


def get_preset(name: str) -> CityPreset:
    if name not in _PRESETS:
        raise KeyError(
            f"未知城市 '{name}'，可选: {list(_PRESETS.keys())}"
        )
    return _PRESETS[name]


def list_presets() -> List[str]:
    return list(_PRESETS.keys())


def register_preset(preset: CityPreset) -> None:
    """允许外部注册新城市（不动本文件）。"""
    _PRESETS[preset.name] = preset
