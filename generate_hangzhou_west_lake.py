"""Generate 3D map models for Hangzhou West Lake area - 25km × 25km.

根据 most_important_doc.md 的要求生成杭州西湖附近 25km × 25km 的 3D 打印模型。

生成对象：
- Object 1: 底板 + 水体挤出（河流、西湖等突出来的部分）
- Object 2: 建筑物
- Object 3: 道路
- Object 4: 地形 + 水体镂空 + 道路融合

数据源：PBF 文件（pbf_cache/zhejiang-latest.osm.pbf）
"""

import os
import sys
import time
import numpy as np
import trimesh

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import (
    set_pbf_file_path,
    fetch_water,
    fetch_roads,
    fetch_buildings,
    fetch_vegetation,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import build_terrain_with_water_holes_manifold
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
from _TEXTURE_STYLE_OF_DEEPSEEK.config import compute_scale, WATERWAY_WIDTHS

# 杭州西湖附近 25km × 25km 范围
# 西湖中心：~30.2441°N, 120.1488°E
LAT1, LON1 = 30.13, 120.01   # 西南角
LAT2, LON2 = 30.36, 120.29   # 东北角
CITY_NAME = "hangzhou_west_lake"
OUTPUT_DIR = "output/hangzhou_west_lake"

print("=" * 70)
print("  杭州西湖 3D 地图生成器 - 25km × 25km")
print("=" * 70)

os.makedirs(OUTPUT_DIR, exist_ok=True)

# 设置 PBF 数据源
pbf_file = os.path.join(_project_root, "pbf_cache", "zhejiang-latest.osm.pbf")
if os.path.exists(pbf_file):
    print(f"\n✅ 使用 PBF 数据源: {pbf_file}")
    print(f"   文件大小: {os.path.getsize(pbf_file) / 1024 / 1024:.1f} MB")
    set_pbf_file_path(pbf_file)
else:
    print(f"\n⚠️  PBF 文件不存在，将使用 Overpass API")
    print(f"   可运行: python3 manage_pbf.py download zhejiang")

# =====================================================================
# Stage 0: 坐标系统设置
# =====================================================================
print(f"\n[Stage 0] 坐标系统设置...")
t0 = time.time()

bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
width_m = bbox["width_m"]
height_m = bbox["height_m"]
area_km2 = bbox["area_km2"]
scale = compute_scale(width_m, height_m)
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
# Stage 1: 高程数据
# =====================================================================
print(f"\n[Stage 1] 获取高程数据...")
t1 = time.time()

elevation_grid = fetch_elevation_grid(south, west, north, east, resolution=512)

if elevation_grid is None:
    print("  ❌ 高程数据获取失败！")
    sys.exit(1)

print(f"  网格形状: {elevation_grid.shape}")
print(f"  高程范围: {elevation_grid.min():.1f}m -> {elevation_grid.max():.1f}m")
print(f"  耗时: {time.time() - t1:.1f}s")

# =====================================================================
# Stage 2: 水体数据（重点关注西湖、钱塘江）
# =====================================================================
print(f"\n[Stage 2] 获取水体数据...")
t2 = time.time()

water_gdf = fetch_water(south, west, north, east)

if water_gdf is None or len(water_gdf) == 0:
    print("  ⚠️  未找到水体特征")
    water_gdf = None
else:
    water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    def estimate_water_area(geom, row):
        if geom.geom_type in ['Polygon', 'MultiPolygon']:
            return geom.area
        elif geom.geom_type in ['LineString', 'MultiLineString']:
            waterway_type = row.get('waterway', 'river')
            width = WATERWAY_WIDTHS.get(waterway_type, 60)
            return geom.length * width
        return 0
    
    water_gdf['est_area'] = water_gdf.apply(lambda r: estimate_water_area(r.geometry, r), axis=1)
    water_gdf = water_gdf.nlargest(500, 'est_area')  # 保留前 500 个最大水体
    
    print(f"  水体特征（前 500）: {len(water_gdf)}")
    print(f"  几何类型: {water_gdf.geometry.type.value_counts().to_dict()}")
    
    if 'name' in water_gdf.columns:
        named = water_gdf['name'].dropna().unique()
        # 查找西湖和钱塘江
        west_lake_found = any('西湖' in str(n) for n in named)
        qiantang_found = any('钱塘' in str(n) for n in named)
        print(f"  找到西湖: {'✅' if west_lake_found else '❌'}")
        print(f"  找到钱塘江: {'✅' if qiantang_found else '❌'}")
        print(f"  命名特征示例: {list(named[:10])}")

print(f"  耗时: {time.time() - t2:.1f}s")

# =====================================================================
# Stage 3: 道路数据（限制主要道路）
# =====================================================================
print(f"\n[Stage 3] 获取道路数据...")
t3 = time.time()

roads_gdf = fetch_roads(south, west, north, east)

if roads_gdf is not None and len(roads_gdf) > 0:
    roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    # 只保留主要道路类型
    main_roads = ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 
                  'motorway_link', 'trunk_link', 'primary_link']
    if 'highway' in roads_gdf.columns:
        roads_gdf = roads_gdf[roads_gdf['highway'].isin(main_roads)]
    
    # 限制到 5000 个特征
    if len(roads_gdf) > 5000:
        roads_gdf = roads_gdf.sample(5000, random_state=42)
    
    print(f"  道路特征（过滤后）: {len(roads_gdf)}")
    if 'highway' in roads_gdf.columns:
        print(f"  道路类型 Top 5: {roads_gdf['highway'].value_counts().head(5).to_dict()}")
else:
    print(f"  未找到道路")
    roads_gdf = None

print(f"  耗时: {time.time() - t3:.1f}s")

# =====================================================================
# Stage 4: 建筑数据（限制到 10000 个）
# =====================================================================
print(f"\n[Stage 4] 获取建筑数据...")
t4 = time.time()

buildings_gdf = fetch_buildings(south, west, north, east)

if buildings_gdf is not None and len(buildings_gdf) > 0:
    buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    
    if len(buildings_gdf) > 10000:
        buildings_gdf = buildings_gdf.sample(10000, random_state=42)
    
    print(f"  建筑特征（采样后）: {len(buildings_gdf)}")
    if 'est_height' in buildings_gdf.columns:
        print(f"  平均高度: {buildings_gdf['est_height'].mean():.1f}m")
        print(f"  最高建筑: {buildings_gdf['est_height'].max():.1f}m")
else:
    print(f"  未找到建筑")
    buildings_gdf = None

print(f"  耗时: {time.time() - t4:.1f}s")

# =====================================================================
# Object 1: 底板 + 水体挤出
# =====================================================================
print(f"\n{'=' * 70}")
print("  对象 1: 底板 + 水体挤出")
print("=" * 70)
print("  说明: 钱塘江、西湖等水体高于底板，被雕刻出来")
t_obj1 = time.time()

if water_gdf is not None and len(water_gdf) > 0:
    water_mesh = build_deepseek_water(
        water_gdf, bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale
    )
    
    if water_mesh is not None:
        print(f"  顶点数: {len(water_mesh.vertices)}")
        print(f"  面数: {len(water_mesh.faces)}")
        print(f"  水密性: {water_mesh.is_watertight}")
        
        obj1_path = os.path.join(OUTPUT_DIR, f"obj1_water_plate_{CITY_NAME}.3mf")
        meshes = {
            'terrain_surface': None,
            'terrain_walls': None,
            'buildings': None,
            'roads': None,
            'water': water_mesh,
            'vegetation': None,
        }
        export_deepseek_3mf(meshes, obj1_path, extruders=4)
        print(f"  ✅ 导出: {obj1_path} ({os.path.getsize(obj1_path)/1024/1024:.2f} MB)")
    else:
        print("  ❌ 水体网格生成失败")
else:
    print("  ⏭️  跳过: 无水体数据")

print(f"  耗时: {time.time() - t_obj1:.1f}s")

# =====================================================================
# Object 4: 地形 + 水体镂空（必须在建筑之前完成）
# =====================================================================
print(f"\n{'=' * 70}")
print("  对象 4: 地形 + 水体镂空 + 道路融合")
print("=" * 70)
print("  说明: 地形表面，水体区域镂空，桥梁融合到地形")
t_obj4 = time.time()

terrain_mesh = None

if elevation_grid is not None:
    result = build_terrain_with_water_holes_manifold(
        elevation_grid=elevation_grid,
        width_m=width_m,
        height_m=height_m,
        area_km2=area_km2,
        scale=scale,
        water_gdf=water_gdf,
        roads_gdf=roads_gdf,
        enable_roads_fusion=(roads_gdf is not None and len(roads_gdf) > 0),
    )
    
    terrain_mesh = result["mesh"]
    stats = result["stats"]
    
    print(f"  顶点数: {len(terrain_mesh.vertices)}")
    print(f"  面数: {len(terrain_mesh.faces)}")
    print(f"  水密性: {terrain_mesh.is_watertight}")
    print(f"  布尔运算次数: {len(stats.get('boolean_ops', []))}")
    if stats.get("roads_faces", 0) > 0:
        print(f"  融合的道路面: {stats['roads_faces']}")
    
    obj4_path = os.path.join(OUTPUT_DIR, f"obj4_terrain_{CITY_NAME}.3mf")
    meshes = {
        "terrain_surface": terrain_mesh,
        "terrain_walls": None,
        "buildings": None,
        "roads": None,
        "water": None,
        "vegetation": None,
    }
    export_deepseek_3mf(meshes, obj4_path, extruders=4)
    print(f"  ✅ 导出: {obj4_path} ({os.path.getsize(obj4_path)/1024/1024:.2f} MB)")
else:
    print("  ⏭️  跳过: 无高程数据")

print(f"  耗时: {time.time() - t_obj4:.1f}s")

# =====================================================================
# Object 2: 建筑（需要 Object 4 的 terrain_mesh）
# =====================================================================
print(f"\n{'=' * 70}")
print("  对象 2: 建筑")
print("=" * 70)
t_obj2 = time.time()

if buildings_gdf is not None and len(buildings_gdf) > 0 and terrain_mesh is not None:
    building_mesh = build_deepseek_buildings(buildings_gdf, terrain_mesh, area_km2, scale)
    
    if building_mesh is not None:
        print(f"  顶点数: {len(building_mesh.vertices)}")
        print(f"  面数: {len(building_mesh.faces)}")
        print(f"  水密性: {building_mesh.is_watertight}")
        
        obj2_path = os.path.join(OUTPUT_DIR, f"obj2_buildings_{CITY_NAME}.3mf")
        meshes = {
            'terrain_surface': None,
            'terrain_walls': None,
            'buildings': building_mesh,
            'roads': None,
            'water': None,
            'vegetation': None,
        }
        export_deepseek_3mf(meshes, obj2_path, extruders=4)
        print(f"  ✅ 导出: {obj2_path} ({os.path.getsize(obj2_path)/1024/1024:.2f} MB)")
    else:
        print("  ❌ 建筑网格生成失败")
else:
    reason = []
    if buildings_gdf is None or len(buildings_gdf) == 0:
        reason.append("无建筑数据")
    if terrain_mesh is None:
        reason.append("无地形网格")
    print(f"  ⏭️  跳过: {', '.join(reason)}")

print(f"  耗时: {time.time() - t_obj2:.1f}s")

# =====================================================================
# Object 3: 道路
# =====================================================================
print(f"\n{'=' * 70}")
print("  对象 3: 道路")
print("=" * 70)
t_obj3 = time.time()

if roads_gdf is not None and len(roads_gdf) > 0:
    road_mesh = build_deepseek_roads(roads_gdf, scale)
    
    if road_mesh is not None:
        print(f"  顶点数: {len(road_mesh.vertices)}")
        print(f"  面数: {len(road_mesh.faces)}")
        print(f"  水密性: {road_mesh.is_watertight}")
        
        obj3_path = os.path.join(OUTPUT_DIR, f"obj3_roads_{CITY_NAME}.3mf")
        meshes = {
            'terrain_surface': None,
            'terrain_walls': None,
            'buildings': None,
            'roads': road_mesh,
            'water': None,
            'vegetation': None,
        }
        export_deepseek_3mf(meshes, obj3_path, extruders=4)
        print(f"  ✅ 导出: {obj3_path} ({os.path.getsize(obj3_path)/1024/1024:.2f} MB)")
    else:
        print("  ❌ 道路网格生成失败")
else:
    print("  ⏭️  跳过: 无道路数据")

print(f"  耗时: {time.time() - t_obj3:.1f}s")

# =====================================================================
# 总结
# =====================================================================
print(f"\n{'=' * 70}")
print(f"  杭州西湖模型生成完成")
print(f"{'=' * 70}")
print(f"\n输出文件在: {OUTPUT_DIR}/")
for f in sorted(os.listdir(OUTPUT_DIR)):
    if f.endswith('.3mf'):
        path = os.path.join(OUTPUT_DIR, f)
        print(f"  {f} ({os.path.getsize(path)/1024/1024:.2f} MB)")

print(f"\n{'=' * 70}\n")
