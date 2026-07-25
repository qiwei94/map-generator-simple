"""浮雕高度图生成：建筑 → 栅格化高度场 + 水体遮罩.

核心思路：将建筑 footprint 光栅化为高度网格（DSM），
每个像素取该位置最高建筑的高度值。
"""

import numpy as np
import geopandas as gpd
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from shapely.geometry import box


def build_relief_heightmap(
    buildings_gdf: gpd.GeoDataFrame,
    water_gdf: gpd.GeoDataFrame | None,
    bbox_utm: tuple[float, float, float, float],
    grid_size: int = 2048,
    height_cap: float = 500.0,
    height_floor: float = 2.0,
) -> dict:
    """将建筑矢量数据光栅化为高度图.

    Args:
        buildings_gdf: UTM 投影的建筑 GeoDataFrame（需有 'height' 列）
        water_gdf: UTM 投影的水体 GeoDataFrame（Polygon/MultiPolygon）
        bbox_utm: (min_x, min_y, max_x, max_y) UTM 坐标
        grid_size: 输出栅格边长（像素）
        height_cap: 高度上限（超过截断）
        height_floor: 最低建筑高度（低于此值视为噪声）

    Returns:
        dict with keys:
            heightmap: (grid_size, grid_size) float32, 单位=米
            water_mask: (grid_size, grid_size) bool, True=水体
            transform: rasterio affine transform
            bbox_utm: 输入的 bbox
            grid_size: 栅格尺寸
    """
    min_x, min_y, max_x, max_y = bbox_utm
    transform = from_bounds(min_x, min_y, max_x, max_y, grid_size, grid_size)

    # ── 1. 建筑高度光栅化 ──
    # 准备 (geometry, height_value) 对
    shapes = []
    for _, row in buildings_gdf.iterrows():
        h = float(row["height"])
        if h < height_floor:
            continue
        h = min(h, height_cap)
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        shapes.append((geom, h))

    # 按高度升序排列（replace 模式下后画的覆盖先画的 → 最高建筑胜出）
    shapes.sort(key=lambda x: x[1])

    print(f"  [relief_map] rasterizing {len(shapes)} buildings → {grid_size}x{grid_size}...")

    # replace 模式：最后绘制的高度值覆盖之前的
    heightmap = rasterize(
        shapes,
        out_shape=(grid_size, grid_size),
        transform=transform,
        fill=0.0,
        dtype="float32",
    )

    # ── 2. 水体遮罩 ──
    water_mask = np.zeros((grid_size, grid_size), dtype=bool)
    if water_gdf is not None and len(water_gdf) > 0:
        water_shapes = [(geom, 1) for geom in water_gdf.geometry if geom and not geom.is_empty]
        if water_shapes:
            water_mask = rasterize(
                water_shapes,
                out_shape=(grid_size, grid_size),
                transform=transform,
                fill=0,
                dtype="uint8",
            ).astype(bool)

    # 水体区域高度归零
    heightmap[water_mask] = 0.0

    # ── 3. 形态学膊胀：让建筑融合为连续质量体 ──
    # 参考作品中建筑不是独立的，而是连成一片，道路是缝隙
    building_pixels_before = (heightmap > 0).sum()
    heightmap = _dilate_buildings(heightmap, iterations=2)
    heightmap[water_mask] = 0.0  # 重新确保水体为空
    building_pixels_after = (heightmap > 0).sum()

    # 统计
    building_pixels = (heightmap > 0).sum()
    total_pixels = grid_size * grid_size
    water_pixels = water_mask.sum()
    print(f"  [relief_map] coverage: building={building_pixels/total_pixels*100:.1f}%, "
          f"water={water_pixels/total_pixels*100:.1f}%")
    print(f"  [relief_map] height range: {heightmap[heightmap>0].min():.1f} - "
          f"{heightmap.max():.1f}m" if heightmap.max() > 0 else "  [relief_map] no buildings")

    return {
        "heightmap": heightmap,
        "water_mask": water_mask,
        "transform": transform,
        "bbox_utm": bbox_utm,
        "grid_size": grid_size,
    }


def compute_hillshade(
    heightmap: np.ndarray,
    azimuth: float = 315.0,
    altitude: float = 45.0,
    z_exaggeration: float = 1.0,
    pixel_size: float = 1.0,
) -> np.ndarray:
    """计算山体阴影（hillsahde）用于 3D 浮雕效果.

    Args:
        heightmap: 高度图 (H, W)
        azimuth: 光源方位角（度，北=0，顺时针）
        altitude: 光源仰角（度）
        z_exaggeration: Z 轴夸张系数
        pixel_size: 像素对应的实地尺寸（米）

    Returns:
        hillshade: (H, W) float32, 范围 [0, 1]
    """
    z = heightmap * z_exaggeration

    # 梯度（中心差分）
    dy, dx = np.gradient(z, pixel_size)

    # 坡度和坡向
    slope = np.arctan(np.sqrt(dx**2 + dy**2))
    aspect = np.arctan2(-dy, dx)

    # 光源角度
    az_rad = np.radians(360.0 - azimuth + 90.0)
    alt_rad = np.radians(altitude)

    # Hillshade 公式
    shade = (
        np.sin(alt_rad) * np.cos(slope)
        + np.cos(alt_rad) * np.sin(slope) * np.cos(az_rad - aspect)
    )

    return np.clip(shade, 0, 1).astype(np.float32)


def compute_ambient_occlusion(
    heightmap: np.ndarray,
    radius: int = 5,
    strength: float = 0.4,
) -> np.ndarray:
    """简易环境光遮蔽（AO）：低洼处更暗.

    通过比较每个像素与周围邻域的高度差来估算遮蔽。

    Args:
        heightmap: 高度图
        radius: 采样半径（像素）
        strength: AO 强度

    Returns:
        ao: (H, W) float32, 范围 [0, 1], 1=无遮蔽
    """
    from scipy.ndimage import maximum_filter

    # 邻域最大值
    local_max = maximum_filter(heightmap, size=radius * 2 + 1)

    # 高度差 → 遮蔽
    diff = local_max - heightmap
    max_diff = max(diff.max(), 1.0)
    ao = 1.0 - strength * (diff / max_diff)

    return np.clip(ao, 0, 1).astype(np.float32)


def compute_edge_darkening(
    heightmap: np.ndarray,
    sigma: float = 2.0,
) -> np.ndarray:
    """建筑边缘暗化：增强轮廓感.

    在高度突变处（建筑边缘）添加暗线，模拟参考作品中
    建筑之间的缝隙/暗沟效果。

    Args:
        heightmap: 高度图
        sigma: 边缘检测尺度

    Returns:
        edges: (H, W) float32, 范围 [0, 1], 0=强边缘（暗），1=无边缘
    """
    from scipy.ndimage import gaussian_filter

    # 梯度幅值
    dy, dx = np.gradient(heightmap)
    grad_mag = np.sqrt(dx**2 + dy**2)

    # 归一化
    smoothed = gaussian_filter(grad_mag, sigma=sigma)
    max_grad = max(smoothed.max(), 1e-6)
    edges = 1.0 - np.clip(smoothed / max_grad, 0, 1)

    return edges.astype(np.float32)


def _dilate_buildings(
    heightmap: np.ndarray,
    iterations: int = 2,
) -> np.ndarray:
    """形态学膊胀：让建筑像素向外扩展，融合为连续体.

    使用 maximum_filter 实现膊胀：每个空像素取邻域内的最大高度。
    这样建筑边缘会向外“生长”，小缝隙（道路）被填充。

    Args:
        heightmap: 高度图
        iterations: 膊胀迭代次数（每次扩展 1 像素）

    Returns:
        膊胀后的高度图
    """
    from scipy.ndimage import maximum_filter

    result = heightmap.copy()
    for _ in range(iterations):
        # 3x3 最大值滤波 = 1像素膊胀
        dilated = maximum_filter(result, size=3)
        # 只扩展有建筑的区域（不让噪声扩散）
        # 新像素的高度 = 邻域最大值 × 0.7（边缘略低，模拟建筑边缘退缩）
        new_pixels = (result == 0) & (dilated > 0)
        result[new_pixels] = dilated[new_pixels] * 0.7

    return result
