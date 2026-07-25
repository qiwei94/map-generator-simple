"""评审图包渲染：干净三色俯视 + 高度场 hillshade。

纯 PIL + numpy 管线（零新增依赖）。
配色对标参考作品：白纸底 / 灰城市底 / 白建筑 / 纯黑水 / 深灰道路缝隙。

抗锯齿：masks 与合成均在 SUPERSAMPLE× 栅格上进行，最终 LANCZOS 降采样——
消除 12m/px 二值填充的"狗啃"台阶边缘（几何简化容差由闭环参数空间负责）。
输出的 masks（降采样后为 [0,1] 浮点覆盖率）同时供 metrics 计算。
"""

import os

import numpy as np
from PIL import Image, ImageDraw

from _TEXTURE_STYLE_OF_DEEPSEEK import config as _cfg

from .config import REVIEW_GRID_SIZE, HEIGHT_DSM_SIZE

# ─── 调色板（0-255）──────────────────────────────────────────────────
_PAPER = (247, 247, 245)
_BLOCK_BASE = (217, 217, 217)
_VEGETATION = (196, 196, 196)
_BUILDING = (255, 255, 255)
_BUILDING_EDGE = (138, 138, 138)
_ROAD = (74, 74, 74)
_WATER = (0, 0, 0)

SUPERSAMPLE = 2  # 抗锯齿超采样倍数


def _iter_polys(polys):
    """展平 Polygon/MultiPolygon，跳过空。"""
    for p in polys:
        if p is None or p.is_empty:
            continue
        if p.geom_type == "Polygon":
            yield p
        elif hasattr(p, "geoms"):
            for g in p.geoms:
                if not g.is_empty and g.geom_type == "Polygon":
                    yield g


class _Rasterizer:
    """bbox → 像素的 PIL 栅格化器（uint8 mask / float32 DSM 两种画布）。"""

    def __init__(self, extent, width: int, height: int):
        self.xmin, self.ymin, self.xmax, self.ymax = extent
        self.W, self.H = width, height
        self._sx = (width - 1) / max(self.xmax - self.xmin, 1e-6)
        self._sy = (height - 1) / max(self.ymax - self.ymin, 1e-6)

    def to_px(self, x, y):
        return ((x - self.xmin) * self._sx, (self.ymax - y) * self._sy)

    def new_canvas(self, float_mode: bool = False):
        img = Image.new("F" if float_mode else "L", (self.W, self.H), 0)
        return img, ImageDraw.Draw(img)

    def draw_poly(self, draw, poly, value):
        try:
            draw.polygon([self.to_px(x, y) for x, y in poly.exterior.coords],
                         fill=value)
            for hole in poly.interiors:
                draw.polygon([self.to_px(x, y) for x, y in hole.coords], fill=0)
        except Exception:
            pass


def _rasterize_mask(raster: _Rasterizer, polys) -> np.ndarray:
    img, draw = raster.new_canvas(float_mode=False)
    for p in _iter_polys(polys):
        raster.draw_poly(draw, p, 1)
    return np.array(img)


def _downscale_mask(mask_u8: np.ndarray, out_size: int) -> np.ndarray:
    """uint8 mask → LANCZOS 降采样 → [0,1] 浮点覆盖率（亚像素精度）。"""
    img = Image.fromarray(mask_u8 * 255, mode="L")
    img = img.resize((out_size, out_size), Image.LANCZOS)
    return np.array(img).astype(np.float32) / 255.0


def _hillshade(heightmap: np.ndarray, pixel_size: float,
               azimuth: float = 315.0, altitude: float = 45.0,
               z_exaggeration: float = 1.0) -> np.ndarray:
    """简化 hillshade（中心差分），输出 [0,1]。"""
    z = heightmap * z_exaggeration
    dy, dx = np.gradient(z, pixel_size)
    slope = np.arctan(np.sqrt(dx ** 2 + dy ** 2))
    aspect = np.arctan2(-dy, dx)
    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)
    shade = (np.sin(alt) * np.cos(slope)
             + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(shade, 0, 1).astype(np.float32)


def render_review_bundle(layers, ctx: dict, road_width_multiplier: float,
                         out_dir: str, tag: str) -> dict:
    """渲染评审图包。

    Returns:
        dict: topdown/height 路径 + dsm + 各 mask（[0,1] 浮点，供 metrics）
    """
    os.makedirs(out_dir, exist_ok=True)
    extent = ctx["bbox_local"]
    xmin, ymin, xmax, ymax = extent
    G, D = REVIEW_GRID_SIZE, HEIGHT_DSM_SIZE
    S = SUPERSAMPLE
    GR = G * S                       # 合成用超采样栅格
    meters_per_px = (xmax - xmin) / GR

    raster_g = _Rasterizer(extent, GR, GR)   # 超采样（俯视）
    raster_d = _Rasterizer(extent, D, D)     # 原尺寸（DSM）

    # ── masks（2x 栅格化 → 降到 G 得 [0,1] 覆盖率；2x 二值版用于合成）──
    def _mask2x(polys):
        return _rasterize_mask(raster_g, polys).astype(bool)

    water_2x = _mask2x(list(layers.WL) + list(layers.WO))
    building_2x = _mask2x(list(layers.BO) + [p for p, _ in layers.BL])
    block_2x = _mask2x(layers.block_base)
    veg_2x = _mask2x(list(layers.VL) + list(layers.VO))

    water_mask = _downscale_mask(water_2x.astype(np.uint8), G)
    building_mask = _downscale_mask(building_2x.astype(np.uint8), G)
    block_mask = _downscale_mask(block_2x.astype(np.uint8), G)
    veg_mask = _downscale_mask(veg_2x.astype(np.uint8), G)

    # ── 高度 DSM（BO 统一聚合高度垫底，BL 按高度升序后画压上）──
    dsm_img, dsm_draw = raster_d.new_canvas(float_mode=True)
    agg_h = float(_cfg.BUILDING_AGGREGATE_HEIGHT_MM)
    for p in _iter_polys(layers.BO):
        raster_d.draw_poly(dsm_draw, p, agg_h)
    for p, h in sorted(((p, float(h)) for p, h in layers.BL
                        if p is not None and not p.is_empty),
                       key=lambda x: x[1]):
        raster_d.draw_poly(dsm_draw, p, h)
    dsm = np.array(dsm_img, dtype=np.float32)
    water_mask_d = _rasterize_mask(raster_d,
                                   list(layers.WL) + list(layers.WO)).astype(bool)

    # ── 合成俯视（2x 栅格）──
    img = np.full((GR, GR, 3), _PAPER, dtype=np.uint8)
    img[block_2x] = _BLOCK_BASE
    img[veg_2x] = _VEGETATION
    img[building_2x] = _BUILDING

    # 建筑描边（2x 下 1px，降采样后呈平滑过渡）
    if building_2x.any():
        from scipy.ndimage import binary_erosion
        edge = building_2x & ~binary_erosion(building_2x)
        img[edge] = _BUILDING_EDGE

    pil_img = Image.fromarray(img, mode="RGB")

    # 道路（2x 画线；同步产出 road_mask）
    road_canvas, road_draw = raster_g.new_canvas(float_mode=False)
    if layers.roads_lines:
        draw = ImageDraw.Draw(pil_img)
        road_widths = getattr(_cfg, "ROAD_WIDTHS", {})
        default_w = float(getattr(_cfg, "ROAD_DEFAULT_WIDTH_M", 10.0))
        for item in layers.roads_lines:
            line, highway = item[0], item[1]
            if line is None or line.is_empty:
                continue
            w_m = road_widths.get(highway, default_w) * road_width_multiplier
            w_px = max(1, int(round(w_m / meters_per_px)))
            geoms = line.geoms if hasattr(line, "geoms") and not hasattr(
                line, "coords") else [line]
            for g in geoms:
                try:
                    pts = [raster_g.to_px(x, y) for x, y in g.coords]
                except Exception:
                    continue
                if len(pts) >= 2:
                    draw.line(pts, fill=_ROAD, width=w_px)
                    road_draw.line(pts, fill=1, width=w_px)
    road_mask = _downscale_mask(np.array(road_canvas), G)

    # 水体最后压上（纯黑）
    img2 = np.array(pil_img)
    img2[water_2x] = _WATER
    pil_img = Image.fromarray(img2, mode="RGB")

    # 2x → 1x LANCZOS（抗锯齿的关键一步）
    pil_img = pil_img.resize((G, G), Image.LANCZOS)
    topdown_path = os.path.join(out_dir, f"{tag}_topdown.png")
    pil_img.save(topdown_path)

    # ── 高度视角（hillshade）──
    hs = _hillshade(dsm, pixel_size=(xmax - xmin) / D, z_exaggeration=30.0)
    base = 0.45 + 0.55 * np.clip(dsm / max(dsm.max(), 1e-6), 0, 1) ** 0.6
    h_img = np.clip(base * (0.5 + 0.5 * hs), 0, 1)
    h_img[water_mask_d] = 0.0
    height_path = os.path.join(out_dir, f"{tag}_height.png")
    Image.fromarray((h_img * 255).astype(np.uint8), mode="L").save(height_path)

    return {
        "topdown": topdown_path,
        "height": height_path,
        "dsm": dsm,
        "water_mask": water_mask,
        "building_mask": building_mask,
        "block_mask": block_mask,
        "veg_mask": veg_mask,
        "road_mask": road_mask,
    }
