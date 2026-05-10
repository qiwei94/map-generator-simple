"""测试 Object 1: 底板 + 水体挤出

分步测试：
Step 1: 获取水体数据
Step 2: 分析水体数据（面积、类型）
Step 3: 生成水体网格
Step 4: 导出 3MF 文件
Step 5: 验证结果

范围：杭州西湖 25km × 25km
"""

import sys
import os
import time

sys.path.insert(0, '.')
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import WATERWAY_WIDTHS

# 杭州西湖附近 25km × 25km 范围
LAT1, LON1 = 30.13, 120.01   # 西南角
LAT2, LON2 = 30.36, 120.29   # 东北角
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/object1_test"

print("=" * 70)
print("  Object 1 测试：底板 + 水体挤出")
print("  范围：杭州西湖 25km × 25km")
print("=" * 70)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# =====================================================================
# Step 0: 设置 PBF 数据源
# =====================================================================
print(f"\n[Step 0] 设置 PBF 数据源...")
pbf_file = os.path.join(os.path.dirname(__file__), 'pbf_cache', 'zhejiang-latest.osm.pbf')
if os.path.exists(pbf_file):
    print(f"  ✅ PBF 文件: {pbf_file}")
    print(f"     大小: {os.path.getsize(pbf_file) / 1024 / 1024:.1f} MB")
    set_pbf_file_path(pbf_file)
else:
    print(f"  ⚠️  PBF 文件不存在")
    print(f"     运行: python3 manage_pbf.py download zhejiang")
    sys.exit(1)

# =====================================================================
# Step 1: 坐标系统设置
# =====================================================================
print(f"\n[Step 1] 坐标系统设置...")
t0 = time.time()

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = 196.0 / max(width_m, height_m)
south, west, north, east = bbox["wgs84_bbox"]
utm_crs = bbox["utm_crs"]
origin = bbox["origin"]
utm_bbox = bbox["utm_bbox"]

bbox_x_min = utm_bbox[0] - origin[0]
bbox_y_min = utm_bbox[1] - origin[1]
bbox_x_max = utm_bbox[2] - origin[0]
bbox_y_max = utm_bbox[3] - origin[1]

print(f"  范围: ({LAT1}, {LON1}) -> ({LAT2}, {LON2})")
print(f"  宽度: {width_m:.0f}m, 高度: {height_m:.0f}m")
print(f"  面积: {area_km2:.1f} km²")
print(f"  比例尺: {scale:.6f} mm/m")
print(f"  耗时: {time.time() - t0:.1f}s")

# =====================================================================
# Step 2: 获取水体数据
# =====================================================================
print(f"\n[Step 2] 获取水体数据...")
t2 = time.time()

water_gdf = fetch_water(south, west, north, east)

if water_gdf is None or len(water_gdf) == 0:
    print("  ❌ 未获取到水体数据")
    sys.exit(1)

print(f"  ✅ 原始水体特征: {len(water_gdf)}")
print(f"  耗时: {time.time() - t2:.1f}s")

# =====================================================================
# Step 3: 投影到本地坐标
# =====================================================================
print(f"\n[Step 3] 投影到本地坐标...")
t3 = time.time()

water_proj = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)

print(f"  ✅ 投影后: {len(water_proj)} 个特征")
print(f"  几何类型分布:")
geom_counts = water_proj.geometry.type.value_counts()
for geom_type, count in geom_counts.items():
    print(f"    - {geom_type}: {count}")
print(f"  耗时: {time.time() - t3:.1f}s")

# =====================================================================
# Step 4: 分析水体数据
# =====================================================================
print(f"\n[Step 4] 分析水体数据...")

# 计算估算面积
def estimate_water_area(geom, row):
    if geom.geom_type in ['Polygon', 'MultiPolygon']:
        return geom.area
    elif geom.geom_type in ['LineString', 'MultiLineString']:
        waterway_type = row.get('waterway', 'river')
        width = WATERWAY_WIDTHS.get(waterway_type, 60)
        return geom.length * width
    return 0

water_proj['est_area'] = water_proj.apply(lambda r: estimate_water_area(r.geometry, r), axis=1)

# 按面积排序
water_sorted = water_proj.nlargest(20, 'est_area')

print(f"  Top 20 最大水体:")
print(f"  {'排名':<4} {'面积(m²)':<12} {'面积(km²)':<12} {'类型':<15} {'名称'}")
print(f"  {'-'*70}")

for i, (_, row) in enumerate(water_sorted.iterrows()):
    area = row['est_area']
    geom_type = row.geometry.geom_type
    
    # 获取类型标签
    water_type = row.get('water', '')
    waterway = row.get('waterway', '')
    natural = row.get('natural', '')
    
    if water_type:
        type_str = f"water={water_type}"
    elif waterway:
        type_str = f"waterway={waterway}"
    elif natural:
        type_str = f"natural={natural}"
    else:
        type_str = "unknown"
    
    name = row.get('name', '')
    if name is None or (isinstance(name, float) and str(name) == 'nan'):
        name = ""
    
    print(f"  {i+1:<4} {area:<12.0f} {area/1e6:<12.2f} {type_str:<15} {name[:30]}")

# 检查关键水体
print(f"\n  关键水体检查:")
named_waters = water_proj[water_proj['name'].notna()]

# 西湖
west_lake = named_waters[named_waters['name'].str.contains('西湖', na=False)]
print(f"    西湖: {'✅ 找到' if len(west_lake) > 0 else '❌ 未找到'} ({len(west_lake)} 个特征)")
if len(west_lake) > 0:
    wl_area = west_lake['est_area'].sum()
    print(f"         总面积: {wl_area/1e6:.2f} km² (预期 ~6.5 km²)")

# 钱塘江
qiantang = named_waters[named_waters['name'].str.contains('钱塘|Qiantang', na=False)]
print(f"    钱塘江: {'✅ 找到' if len(qiantang) > 0 else '❌ 未找到'} ({len(qiantang)} 个特征)")
if len(qiantang) > 0:
    qt_area = qiantang['est_area'].sum()
    print(f"         总面积: {qt_area/1e6:.2f} km²")

# 西溪湿地
xixi = named_waters[named_waters['name'].str.contains('西溪', na=False)]
print(f"    西溪湿地: {'✅ 找到' if len(xixi) > 0 else '❌ 未找到'} ({len(xixi)} 个特征)")

# =====================================================================
# Step 5: 限制水体数量（避免数据过大）
# =====================================================================
print(f"\n[Step 5] 筛选水体特征...")

# 保留前 500 个最大水体
MAX_WATER_FEATURES = 500
if len(water_proj) > MAX_WATER_FEATURES:
    water_filtered = water_proj.nlargest(MAX_WATER_FEATURES, 'est_area')
    print(f"  ⚠️  原始 {len(water_proj)} 个，筛选后保留 {len(water_filtered)} 个")
else:
    water_filtered = water_proj
    print(f"  ✅ 全部 {len(water_filtered)} 个水体都保留")

# =====================================================================
# Step 6: 生成水体网格
# =====================================================================
print(f"\n[Step 6] 生成水体网格...")
t6 = time.time()

water_mesh = build_deepseek_water(
    water_filtered, 
    bbox_x_min, bbox_y_min, 
    bbox_x_max, bbox_y_max, 
    scale
)

if water_mesh is None:
    print("  ❌ 水体网格生成失败")
    sys.exit(1)

print(f"  ✅ 网格生成成功")
print(f"  顶点数: {len(water_mesh.vertices)}")
print(f"  面数: {len(water_mesh.faces)}")
print(f"  水密性: {water_mesh.is_watertight}")
print(f"  体积: {water_mesh.volume:.2f} mm³")
print(f"  Z 范围: {water_mesh.bounds[0][2]:.2f} -> {water_mesh.bounds[1][2]:.2f} mm")
print(f"  XY 范围: ({water_mesh.bounds[0][0]:.1f}, {water_mesh.bounds[0][1]:.1f}) -> ({water_mesh.bounds[1][0]:.1f}, {water_mesh.bounds[1][1]:.1f}) mm")
print(f"  耗时: {time.time() - t6:.1f}s")

# =====================================================================
# Step 7: 导出 3MF 文件
# =====================================================================
print(f"\n[Step 7] 导出 3MF 文件...")
t7 = time.time()

output_path = os.path.join(OUTPUT_DIR, f"obj1_water_plate_{CITY_NAME}.3mf")

meshes = {
    'terrain_surface': None,
    'terrain_walls': None,
    'buildings': None,
    'roads': None,
    'water': water_mesh,
    'vegetation': None,
}

export_deepseek_3mf(meshes, output_path, extruders=4)

file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
print(f"  ✅ 导出成功")
print(f"  文件: {output_path}")
print(f"  大小: {file_size_mb:.2f} MB")
print(f"  耗时: {time.time() - t7:.1f}s")

# =====================================================================
# 总结
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  Object 1 测试完成")
print(f"{'=' * 70}")
print(f"\n📊 测试结果:")
print(f"  - 水体特征数: {len(water_filtered)}")
print(f"  - 网格顶点数: {len(water_mesh.vertices)}")
print(f"  - 网格面数: {len(water_mesh.faces)}")
print(f"  - 水密性: {'✅ 是' if water_mesh.is_watertight else '❌ 否'}")
print(f"  - 文件路径: {output_path}")
print(f"\n🎯 下一步:")
print(f"  1. 在 Bambu Studio 中打开 3MF 文件")
print(f"  2. 检查水体是否正确挤出")
print(f"  3. 检查西湖、钱塘江等主要水体是否完整")
print(f"  4. 确认无误后继续测试 Object 4（地形）")
print(f"\n{'=' * 70}\n")
