"""产品级渲染器：对标 Reso/lution Urban Series 美学.

核心视觉规则：
- 水体 = 纯黑 (#000000)
- 建筑 = 白→灰渐变（高度越高越亮）
- 表面 = hillshade + AO + 边缘暗化 + 微纹理
- 背景 = 纯白或纯黑（可选）
- 无坐标轴、无网格、无标注
"""

import numpy as np
from pathlib import Path

from .relief_map import (
    compute_hillshade,
    compute_ambient_occlusion,
    compute_edge_darkening,
)


def render_relief(
    relief_data: dict,
    output_path: str,
    *,
    city_name: str = "",
    style: str = "mono_light",
    z_exaggeration: float = 3.0,
    light_azimuth: float = 315.0,
    light_altitude: float = 45.0,
    ao_radius: int = 5,
    ao_strength: float = 0.35,
    edge_strength: float = 0.3,
    grain_strength: float = 0.03,
    height_gamma: float = 0.6,
    output_size_px: int = 3000,
    dpi: int = 300,
) -> str:
    """渲染建筑浮雕图.

    Args:
        relief_data: build_relief_heightmap() 的输出
        output_path: 输出 PNG 路径
        city_name: 城市名（用于文件名）
        style: 渲染风格
            "mono_light" — 白底黑水，建筑白→灰（参考作品风格）
            "mono_dark" — 黑底，建筑灰→白
            "warm" — 暖色调（米白/深棕）
        z_exaggeration: 高度夸张系数（越大 3D 感越强）
        light_azimuth: 光源方位角
        light_altitude: 光源仰角
        ao_radius: AO 采样半径
        ao_strength: AO 强度
        edge_strength: 边缘暗化强度
        grain_strength: 表面颗粒纹理强度
        height_gamma: 高度→亮度映射的 gamma（<1 压缩高光，>1 增强对比）
        output_size_px: 输出图像边长（像素）
        dpi: 输出 DPI

    Returns:
        output_path
    """
    heightmap = relief_data["heightmap"]
    water_mask = relief_data["water_mask"]
    grid_size = relief_data["grid_size"]
    bbox_utm = relief_data["bbox_utm"]

    # 像素对应的实地尺寸
    min_x, min_y, max_x, max_y = bbox_utm
    pixel_size = (max_x - min_x) / grid_size

    # ── 1. 高度归一化 ──
    h_max = heightmap[heightmap > 0].max() if (heightmap > 0).any() else 1.0
    h_norm = np.zeros_like(heightmap)
    building_mask = heightmap > 0
    h_norm[building_mask] = (heightmap[building_mask] / h_max) ** height_gamma

    # ── 2. Hillshade（3D 浮雕感）──
    hillshade = compute_hillshade(
        heightmap,
        azimuth=light_azimuth,
        altitude=light_altitude,
        z_exaggeration=z_exaggeration,
        pixel_size=pixel_size,
    )

    # ── 3. Ambient Occlusion（缝隙暗化）──
    ao = compute_ambient_occlusion(heightmap, radius=ao_radius, strength=ao_strength)

    # ── 4. 边缘暗化（建筑轮廓）──
    edges = compute_edge_darkening(heightmap, sigma=1.5)
    edge_factor = 1.0 - edge_strength * (1.0 - edges)

    # ── 5. 微纹理（手工质感）──
    grain = _generate_grain(grid_size, grain_strength)

    # ── 6. 合成 ──
    if style == "mono_light":
        img = _compose_mono_light(
            h_norm, hillshade, ao, edge_factor, grain,
            building_mask, water_mask,
        )
    elif style == "mono_dark":
        img = _compose_mono_dark(
            h_norm, hillshade, ao, edge_factor, grain,
            building_mask, water_mask,
        )
    elif style == "warm":
        img = _compose_warm(
            h_norm, hillshade, ao, edge_factor, grain,
            building_mask, water_mask,
        )
    else:
        raise ValueError(f"Unknown style: {style}")

    # ── 7. 输出 ──
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 缩放到目标尺寸
    if grid_size != output_size_px:
        img = _resize_image(img, output_size_px)

    _save_png(img, str(out), dpi=dpi)
    print(f"  [renderer] saved: {out} ({output_size_px}x{output_size_px}px)")
    return str(out)


# ─── 合成函数 ──────────────────────────────────────────────────────────


def _compose_mono_light(
    h_norm, hillshade, ao, edge_factor, grain,
    building_mask, water_mask,
) -> np.ndarray:
    """白底 + 黑水 + 白灰建筑（参考作品风格）.

    建筑亮度 = f(高度) × hillshade × AO × edge
    - 低矮建筑: 中灰 (~0.55)
    - 高层建筑: 亮白 (~0.95)
    - 水体: 纯黑 (0.0)
    - 无建筑陆地: 浅灰底 (~0.42)
    """
    H, W = h_norm.shape

    # 基础亮度：高度映射
    base = np.full((H, W), 0.42, dtype=np.float32)  # 地面底色
    base[building_mask] = 0.52 + 0.43 * h_norm[building_mask]

    # 光照
    shade = np.ones_like(hillshade) * 0.90  # 地面平坦光照
    shade[building_mask] = 0.45 + 0.55 * hillshade[building_mask]

    # 合成
    img = base * shade * ao * edge_factor

    # 建筑区域加微弱纹理（手工质感）
    img[building_mask] += grain[building_mask] * 0.6

    # 水体 = 纯黑
    img[water_mask] = 0.0

    # 无建筑无水的陆地：干净浅灰 + 极微弱纹理
    land_no_building = ~building_mask & ~water_mask
    img[land_no_building] = 0.40 + grain[land_no_building] * 0.3

    return np.clip(img, 0, 1)


def _compose_mono_dark(
    h_norm, hillshade, ao, edge_factor, grain,
    building_mask, water_mask,
) -> np.ndarray:
    """黑底 + 灰白建筑（戏剧性更强）."""
    H, W = h_norm.shape

    base = np.full((H, W), 0.05, dtype=np.float32)  # 近黑背景
    base[building_mask] = 0.35 + 0.60 * h_norm[building_mask]

    shade = np.ones_like(hillshade) * 0.3
    shade[building_mask] = 0.4 + 0.6 * hillshade[building_mask]

    img = base * shade * ao * edge_factor + grain
    img[water_mask] = 0.0

    return np.clip(img, 0, 1)


def _compose_warm(
    h_norm, hillshade, ao, edge_factor, grain,
    building_mask, water_mask,
) -> np.ndarray:
    """暖色调（米白建筑 + 深棕水）."""
    H, W = h_norm.shape
    img_gray = _compose_mono_light(
        h_norm, hillshade, ao, edge_factor, grain,
        building_mask, water_mask,
    )

    # 灰度 → 暖色
    img_rgb = np.stack([img_gray, img_gray * 0.95, img_gray * 0.88], axis=-1)
    # 水体偏深蓝
    img_rgb[water_mask] = [0.02, 0.03, 0.06]

    return np.clip(img_rgb, 0, 1)


# ─── 工具函数 ──────────────────────────────────────────────────────────


def _generate_grain(size: int, strength: float) -> np.ndarray:
    """生成微颗粒纹理（模拟石膏/树脂表面）."""
    rng = np.random.default_rng(42)
    grain = rng.normal(0, strength, (size, size)).astype(np.float32)
    # 轻微模糊使颗粒更自然
    from scipy.ndimage import gaussian_filter
    grain = gaussian_filter(grain, sigma=0.5)
    return grain


def _resize_image(img: np.ndarray, target_size: int) -> np.ndarray:
    """缩放图像到目标尺寸."""
    from PIL import Image

    if img.ndim == 2:
        pil_img = Image.fromarray((img * 255).astype(np.uint8), mode="L")
    else:
        pil_img = Image.fromarray((img * 255).astype(np.uint8), mode="RGB")

    pil_img = pil_img.resize((target_size, target_size), Image.LANCZOS)
    return np.array(pil_img).astype(np.float32) / 255.0


def _save_png(img: np.ndarray, path: str, dpi: int = 300):
    """保存为 PNG."""
    from PIL import Image

    if img.ndim == 2:
        pil_img = Image.fromarray((img * 255).astype(np.uint8), mode="L")
    else:
        pil_img = Image.fromarray((img * 255).astype(np.uint8), mode="RGB")

    pil_img.save(path, dpi=(dpi, dpi))
