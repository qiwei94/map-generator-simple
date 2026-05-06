"""对象4 v3：分别生成地形和水体，再用 Manifold 布尔差集镂空。

策略：
1. 生成精细地形网格（对象4主体）
2. 生成独立水体网格（使用 build_deepseek_water）
3. 用 Manifold 布尔差集：terrain - water
4. 导出结果

关键改进：
- 水体使用 build_deepseek_water() 生成（与工具一致）
- 地形和水体是两个独立对象，布尔运算更可靠
"""

import os
import sys
import time
import numpy as np
import trimesh

# 确保项目根目录在path中
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import build_terrain_mesh, build_terrain_with_base
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.config import PRINT_BASE_THICKNESS_M, PRINT_COLORS
from _TEXTURE_STYLE_OF_DEEPSEEK._bridge import trimesh_to_manifold, manifold_to_trimesh

# 杭州西湖区域（25km × 25km）
LAT1, LON1 = 30.1375, 120.020   # south-west
LAT2, LON2 = 30.3625, 120.280   # north-east
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/object4_v3"

# =====================================================================
print("=" * 70)
print("  对象4 v3 - 杭州（西湖区域）")
print("  策略：分别生成地形和水体，再布尔差集镂空")
print("=" * 70)

# =====================================================================
# Stage 0: 数据获取和坐标设置
# =====================================================================
print(f"\n[Stage 0] 数据获取...")
t0 = time.time()

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)

print(f"  Bounding box: ({LAT1}, {LON1}) → ({LAT2}, {LON2})")
print(f"  Width: {width_m:.0f}m, Height: {height_m:.0f}m")
print(f"  Area: {area_km2:.1f} km²")
print(f"  Scale: {scale:.6f} mm/m")
print(f"  Time: {time.time() - t0:.1f}s")

# =====================================================================
# Stage 1: 获取高程数据
# =====================================================================
print(f"\n[Stage 1] 获取高程数据...")
t1 = time.time()

south, west, north, east = bbox["wgs84_bbox"]

# 使用512分辨率
elevation_grid = fetch_elevation_grid(south, west, north, east, resolution=512)

if elevation_grid is None:
    print("  ERROR: 高程数据获取失败！")
    sys.exit(1)

print(f"  Elevation grid: {elevation_grid.shape}")
print(f"  Elevation range: {elevation_grid.min():.1f}m → {elevation_grid.max():.1f}m")
print(f"  Time: {time.time() - t1:.1f}s")

# =====================================================================
# Stage 2: 获取水体数据
# =====================================================================
print(f"\n[Stage 2] 获取水体数据...")
t2 = time.time()

water_gdf = fetch_water(south, west, north, east)

if water_gdf is None or len(water_gdf) == 0:
    print("  ERROR: 水体数据获取失败！")
    sys.exit(1)

utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print(f"  Water features: {len(water_gdf)}")
print(f"  Time: {time.time() - t2:.1f}s")

# =====================================================================
# Stage 3: 生成精细地形（带底座）
# =====================================================================
print(f"\n[Stage 3] 生成精细地形（带底座）...")
t3 = time.time()

# Step 3.1: 生成地形表面
terrain_surface = build_terrain_mesh(
    elevation_grid, width_m, height_m, area_km2
)
print(f"  地形表面: {len(terrain_surface.vertices)} vertices, {len(terrain_surface.faces)} faces")

# Step 3.2: 添加底座使其 watertight
origin_x, origin_y = origin
terrain_bbox_x_min = utm_bbox[0] - origin_x
terrain_bbox_y_min = utm_bbox[1] - origin_y
terrain_bbox_x_max = utm_bbox[2] - origin_x
terrain_bbox_y_max = utm_bbox[3] - origin_y

terrain_solid = build_terrain_with_base(
    terrain_surface,
    base_thickness_m=PRINT_BASE_THICKNESS_M,
    terrain_colors={
        "terrain_low": PRINT_COLORS["terrain_low"],
        "terrain_high": PRINT_COLORS["terrain_high"],
    }
)

print(f"  地形实体: {len(terrain_solid.vertices)} vertices, {len(terrain_solid.faces)} faces")
print(f"  Watertight: {terrain_solid.is_watertight}")
print(f"  Z range: {terrain_solid.bounds[0][2]:.2f} → {terrain_solid.bounds[1][2]:.2f} mm")
print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: 生成水体网格（独立对象）
# =====================================================================
print(f"\n[Stage 4] 生成水体网格...")
t4 = time.time()

# 使用 build_deepseek_water 生成水体（与工具一致）
water_mesh = build_deepseek_water(
    water_gdf,
    terrain_bbox_x_min, terrain_bbox_y_min,
    terrain_bbox_x_max, terrain_bbox_y_max,
    scale,
)

if water_mesh is None or len(water_mesh.faces) == 0:
    print("  ERROR: 水体网格生成失败！")
    sys.exit(1)

print(f"  水体网格: {len(water_mesh.vertices)} vertices, {len(water_mesh.faces)} faces")
print(f"  Z range: {water_mesh.bounds[0][2]:.2f} → {water_mesh.bounds[1][2]:.2f} mm")
print(f"  Watertight: {water_mesh.is_watertight}")
print(f"  Time: {time.time() - t4:.1f}s")

# =====================================================================
# Stage 5: Manifold 布尔差集（terrain - water）
# =====================================================================
print(f"\n[Stage 5] Manifold 布尔差集（terrain - water）...")
t5 = time.time()

try:
    # 转换为 Manifold
    terrain_m = trimesh_to_manifold(terrain_solid)
    water_m = trimesh_to_manifold(water_mesh)
    
    print(f"  地形 Manifold: {terrain_m.num_vert()} vertices")
    print(f"  水体 Manifold: {water_m.num_vert()} vertices")
    
    # 布尔差集
    print(f"  执行布尔差集运算...")
    terrain_with_holes_m = terrain_m - water_m
    
    # 转回 trimesh
    terrain_final = manifold_to_trimesh(terrain_with_holes_m)
    
    print(f"  最终网格: {len(terrain_final.vertices)} vertices, {len(terrain_final.faces)} faces")
    print(f"  Watertight: {terrain_final.is_watertight}")
    print(f"  Volume: {terrain_final.volume if terrain_final.is_watertight else 0:.2f} mm³")
    print(f"  Z range: {terrain_final.bounds[0][2]:.2f} → {terrain_final.bounds[1][2]:.2f} mm")
    
except Exception as e:
    print(f"  ERROR: 布尔运算失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print(f"  Time: {time.time() - t5:.1f}s")

# =====================================================================
# Stage 6: 导出3MF
# =====================================================================
print(f"\n[Stage 6] 导出3MF...")
t6 = time.time()

os.makedirs(OUTPUT_DIR, exist_ok=True)
output_path = os.path.join(OUTPUT_DIR, f"terrain_final_v3_{CITY_NAME}.3mf")

meshes = {
    "terrain_surface": terrain_final,
    "terrain_walls": None,
    "buildings": None,
    "roads": None,
    "water": None,
    "vegetation": None,
}

export_deepseek_3mf(meshes, output_path, extruders=4)

file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
print(f"  Output: {output_path}")
print(f"  File size: {file_size_mb:.2f} MB")
print(f"  Time: {time.time() - t6:.1f}s")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  对象4 v3 实现完成")
print(f"{'=' * 70}")
print(f"\n下一步操作:")
print(f"  1. 打开3MF文件: {output_path}")
print(f"  2. 检查水体是否正确镂空")
print(f"  3. 确认后继续添加道路和桥梁")
print(f"\n{'=' * 70}\n")
