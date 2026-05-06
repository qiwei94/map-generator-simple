"""测试对象4（地形+道路融合+水体镂空） - 杭州案例。

完整流程：
1. 获取杭州数据
2. 实现对象4
3. 导出3MF
4. 自动验证
5. 生成验证文档
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
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_water, fetch_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale

# 杭州西湖区域（25km × 25km）
LAT1, LON1 = 30.1375, 120.020   # south-west
LAT2, LON2 = 30.3625, 120.280   # north-east
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/object4_validation"

# =====================================================================
print("=" * 70)
print("  对象4验证测试 - 杭州（西湖区域）")
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
# Stage 1: 获取高程数据（第一步：提高分辨率生成精细地形）
# =====================================================================
print(f"\n[Stage 1] 获取高分辨率高程数据...")
print(f"  [策略] 使用512分辨率获取更精细的地形纹理")
t1 = time.time()

south, west, north, east = bbox["wgs84_bbox"]

# 使用512分辨率的高程数据（原256，现在提高到512）
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

# 处理所有主要水体特征（增加到100个，确保钱塘江等主要水体都被处理）
MAX_WATER_FEATURES = 100  # 处理前100个最大水体特征
if len(water_gdf) > MAX_WATER_FEATURES:
    # 选择面积最大的水体特征
    water_gdf['area'] = water_gdf.geometry.area
    water_gdf = water_gdf.nlargest(MAX_WATER_FEATURES, 'area')
    print(f"  [限制] 仅处理前 {MAX_WATER_FEATURES} 个最大水体特征")
else:
    print(f"  [处理] 处理全部 {len(water_gdf)} 个水体特征")

print(f"  Water features: {len(water_gdf)}")
print(f"  Time: {time.time() - t2:.1f}s")

# =====================================================================
# Stage 2.5: 获取道路数据（第三步：添加道路和桥梁）
# =====================================================================
print(f"\n[Stage 2.5] 获取道路数据...")
t2_5 = time.time()

# 获取主要道路（用于桥梁显示）
roads_gdf = fetch_roads(south, west, north, east)

if roads_gdf is not None and len(roads_gdf) > 0:
    roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    print(f"  Road features: {len(roads_gdf)}")
    if 'highway' in roads_gdf.columns:
        print(f"  Highway types: {roads_gdf['highway'].value_counts().head(10).to_dict()}")
else:
    print(f"  No roads found")
    roads_gdf = None

print(f"  Time: {time.time() - t2_5:.1f}s")

# =====================================================================
# Stage 3: 实现对象4（地形+道路融合+水体镂空）
# =====================================================================
print(f"\n[Stage 3] 实现对象4（Manifold布尔运算）...")
t3 = time.time()

# 第三步：启用道路融合，实现钱塘江上的桥梁
result = build_terrain_with_water_holes_manifold(
    elevation_grid=elevation_grid,
    width_m=width_m,
    height_m=height_m,
    area_km2=area_km2,
    scale=scale,
    water_gdf=water_gdf,
    roads_gdf=roads_gdf,  # 启用道路融合
    enable_roads_fusion=(roads_gdf is not None and len(roads_gdf) > 0),
)

terrain_mesh = result["mesh"]
stats = result["stats"]
validation = result["validation"]

print(f"  Time: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: 导出3MF
# =====================================================================
print(f"\n[Stage 4] 导出3MF...")
t4 = time.time()

os.makedirs(OUTPUT_DIR, exist_ok=True)
output_path = os.path.join(OUTPUT_DIR, f"terrain_final_{CITY_NAME}.3mf")

meshes = {
    "terrain_surface": terrain_mesh,  # 道路已融合到地形中
    "terrain_walls": None,
    "buildings": None,
    "roads": None,  # 道路已通过布尔并集融合到地形表面
    "water": None,
    "vegetation": None,
}

# 输出统计信息
if stats.get("roads_faces", 0) > 0:
    print(f"\n  [道路融合] {stats['roads_faces']} 个道路面已融合到地形")

# 将地形作为单独对象导出（使用terrain_surface颜色）
export_deepseek_3mf(
    meshes,
    output_path,
    extruders=4,
)

file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
print(f"  Output: {output_path}")
print(f"  File size: {file_size_mb:.2f} MB")
print(f"  Time: {time.time() - t4:.1f}s")

# =====================================================================
# Stage 5: 生成验证文档
# =====================================================================
print(f"\n[Stage 5] 生成验证文档...")

validation_report_path = os.path.join(OUTPUT_DIR, f"validation_terrain_final_{CITY_NAME}.md")

with open(validation_report_path, 'w') as f:
    f.write(f"# terrain_final验证报告 - {CITY_NAME}\n\n")
    f.write(f"> **实现日期**: {time.strftime('%Y-%m-%d')}\n")
    f.write(f"> **Manifold方法**: 布尔差集（terrain - water）\n")
    f.write(f"> **验证状态**: 待确认\n\n")
    f.write("---\n\n")

    f.write("## 一、实现方案\n\n")
    f.write("### 1.1 布尔运算逻辑\n\n")
    f.write("```python\n")
    f.write("# Step 1: 地形重建\n")
    f.write("terrain_mesh = build_deepseek_terrain(...)\n")
    f.write("terrain_m = trimesh_to_manifold(terrain_mesh)\n\n")
    f.write("# Step 2: 水体布尔差集镂空\n")
    f.write("water_columns_m = trimesh_to_manifold(water_columns)\n")
    f.write("terrain_with_holes_m = terrain_m - water_columns_m\n\n")
    f.write("# Step 3: 转回trimesh\n")
    f.write("terrain_final = manifold_to_trimesh(terrain_with_holes_m)\n")
    f.write("```\n\n")

    f.write("### 1.2 关键参数\n\n")
    f.write("| 参数名 | 值 | 说明 |\n")
    f.write("|--------|---|------|\n")
    f.write(f"| Z_bottom | {terrain_mesh.bounds[0][2]:.2f} mm | 水体挤出柱底部 |\n")
    f.write(f"| Z_top | {terrain_mesh.bounds[1][2]:.2f} mm | 水体挤出柱顶部 |\n")
    f.write(f"| 水体数量 | {len(water_gdf)} | 镂空的水体特征数 |\n")
    f.write(f"| Scale | {scale:.6f} | 比例尺（mm/m） |\n\n")

    f.write("---\n\n")
    f.write("## 二、生成模型验证\n\n")
    f.write("### 2.1 3MF文件信息\n\n")
    f.write(f"- **文件路径**: `{output_path}`\n")
    f.write(f"- **文件大小**: {file_size_mb:.2f} MB\n")
    f.write(f"- **顶点数**: {validation['n_vertices']}\n")
    f.write(f"- **面数**: {validation['n_faces']}\n")
    f.write(f"- **Watertight**: {validation['watertight']}\n\n")

    f.write("### 2.2 网格质量检查\n\n")
    f.write("| 检查项 | 结果 | 是否通过 |\n")
    f.write("|--------|------|---------|\n")
    f.write(f"| Watertight | {validation['watertight']} | {'✅' if validation['watertight'] else '❌'} |\n")
    f.write(f"| Volume | {validation['volume']:.2f} mm³ | {'✅' if validation['volume'] > 0 else '❌'} |\n")
    f.write(f"| Z范围 | {validation['bounds'][0][2]:.2f} → {validation['bounds'][1][2]:.2f} mm | ✅ |\n")
    f.write(f"| 顶点数 | {validation['n_vertices']} | ✅ |\n")
    f.write(f"| 面数 | {validation['n_faces']} | ✅ |\n\n")

    f.write("### 2.3 布尔运算统计\n\n")
    f.write("| 操作 | 描述 | 状态 |\n")
    f.write("|------|------|------|\n")
    for op in stats["boolean_ops"]:
        f.write(f"| {op} | Manifold布尔运算 | ✅ |\n")
    f.write("\n")

    f.write("---\n\n")
    f.write("## 三、几何特征验证\n\n")
    f.write("### 3.1 水体镂空验证\n\n")
    f.write("**预期特征**: 水体区域应完全镂空，与对象1（底板+水体）能完美嵌合。\n\n")
    f.write("**验证方法**:\n")
    f.write("1. 打开3MF文件，检查水体区域是否为空洞\n")
    f.write("2. 与对象1（water_plate.3mf）对比，确认嵌合关系\n")
    f.write("3. 检查空洞边缘是否封闭\n\n")

    f.write("### 3.2 地形起伏验证\n\n")
    f.write(f"**高程数据范围**: {elevation_grid.min():.1f}m → {elevation_grid.max():.1f}m\n\n")
    f.write("**验证方法**:\n")
    f.write("1. 检查地形是否有起伏（杭州有山体）\n")
    f.write("2. 检查西湖、钱塘江区域是否正确镂空\n")
    f.write("3. 检查地形厚度是否合理（约4mm）\n\n")

    f.write("---\n\n")
    f.write("## 四、问题与疑问\n\n")
    f.write("### 4.1 实现过程中的问题\n\n")
    f.write("- [待填写] 水体挤出柱Z范围是否合理？\n")
    f.write("- [待填写] 镂空后地形是否仍然watertight？\n")
    f.write("- [待填写] 与对象1的嵌合是否完美？\n\n")

    f.write("### 4.2 待确认的疑问\n\n")
    f.write("- **问题1**: 水体镂空后，地形底部是否完全封闭？\n")
    f.write("- **问题2**: 多个水体特征是否都被正确镂空？\n")
    f.write("- **问题3**: 模型是否可以正常3D打印？\n\n")

    f.write("---\n\n")
    f.write("## 五、确认清单\n\n")
    f.write("请用户确认以下内容：\n\n")
    f.write("- [ ] **模型质量**: 3MF文件能正常打开，网格无明显错误\n")
    f.write("- [ ] **水体镂空**: 水体区域完全镂空，边缘封闭\n")
    f.write("- [ ] **地形起伏**: 高程数据正确渲染，有起伏特征\n")
    f.write("- [ ] **与对象1嵌合**: 能与对象1（底板+水体）完美嵌合\n")
    f.write("- [ ] **打印可行性**: 可以进行3D打印（如需要）\n\n")

    f.write("---\n\n")
    f.write("**用户反馈**: [待填写]\n\n")
    f.write("**确认状态**: 待确认\n\n")
    f.write("**下一步**: [继续对象3（植被镂空）/修改对象4]\n\n")

print(f"  Validation report: {validation_report_path}")

# =====================================================================
# Summary
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  对象4验证完成 - 请查看验证文档")
print(f"{'=' * 70}")
print(f"\n下一步操作:")
print(f"  1. 打开3MF文件: {output_path}")
print(f"  2. 检查验证文档: {validation_report_path}")
print(f"  3. 确认后输入 'continue' 继续对象3（植被镂空）")
print(f"  4. 如有问题输入 'modify' 修改对象4")
print(f"\n{'=' * 70}\n")