"""Pipeline orchestrator for _TEXTURE_STYLE_OF_DEEPSEEK.

Entry point: run(lat1, lon1, lat2, lon2, output_dir, city_name)

Generates a complete 3MF model matching the Urban Series reference demo style.
"""

import os
import sys
import time
from datetime import datetime

# Ensure the project root is on sys.path so terrain3d imports work
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm, project_geodataframe
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import fetch_buildings, fetch_roads, fetch_water, fetch_vegetation

from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    TERRAIN_GRID,
    get_area_class,
    compute_scale,
    BUILDING_V2_HOTSPOT_RELAX,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
from _TEXTURE_STYLE_OF_DEEPSEEK.object4_terrain_with_holes import (
    build_terrain_with_water_holes_manifold,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings, build_deepseek_buildings_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads, build_deepseek_roads_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water, build_deepseek_water_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import build_deepseek_vegetation_v3
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import (
    export_deepseek_3mf,
    split_terrain_mesh,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.validator import (
    validate_3mf,
    print_validation_report,
)


def run(lat1: float, lon1: float, lat2: float, lon2: float,
        output_dir: str = "output/deepseek",
        city_name: str = None) -> str:
    """Generate a _TEXTURE_STYLE_OF_DEEPSEEK 3MF model.

    Args:
        lat1, lon1: first corner (WGS84 degrees)
        lat2, lon2: second corner (WGS84 degrees)
        output_dir: directory for output files
        city_name: name for output file (auto-detected from coords if None)

    Returns:
        Path to the generated .3mf file.
    """
    t_start = time.time()

    # Ensure project root for imports
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Auto city name from coordinates
    if city_name is None:
        city_name = f"map_{lat1:.2f}_{lon1:.2f}"

    # Create timestamped output directory (matches main pipeline pattern)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"run_{lat1:.4f}_{lon1:.4f}_{lat2:.4f}_{lon2:.4f}_{ts}"
    output_dir = os.path.abspath(os.path.join(output_dir, run_name))
    os.makedirs(output_dir, exist_ok=True)

    output_path = os.path.join(output_dir, f"{city_name}_deepseek.3mf")

    print(f"\n{'='*60}")
    print(f"  _TEXTURE_STYLE_OF_DEEPSEEK Pipeline")
    print(f"  City: {city_name}")
    print(f"  BBox: ({lat1:.4f}, {lon1:.4f}) -> ({lat2:.4f}, {lon2:.4f})")
    print(f"  Output: {output_path}")
    print(f"{'='*60}\n")

    # ====================================================================
    # Stage 0: Coordinate system setup
    # ====================================================================
    print("[Stage 0] Computing coordinate system...")
    bbox = bbox_to_utm(lat1, lon1, lat2, lon2)
    area_km2 = bbox["area_km2"]
    area_class = get_area_class(area_km2)
    resolution = TERRAIN_GRID.get(area_class, 512)

    south, west, north, east = bbox["wgs84_bbox"]
    print(f"  Area: {area_km2:.1f} km^2 ({area_class})")
    print(f"  UTM zone: {bbox['utm_crs'].utm_zone}")
    print(f"  Resolution: {resolution}x{resolution}")

    # Compute model scale (mm per real meter) to normalize to 254mm span
    width_m = bbox["width_m"]
    height_m = bbox["height_m"]
    scale = compute_scale(width_m, height_m)
    print(f"  Scale: {scale:.6f} mm/m")

    # ====================================================================
    # Stage 1: Fetch elevation data
    # ====================================================================
    print("\n[Stage 1] Fetching elevation data...")
    t1 = time.time()
    elevation_grid = fetch_elevation_grid(south, west, north, east, resolution)
    print(f"  Grid shape: {elevation_grid.shape}")
    print(f"  Elevation range: {elevation_grid.min():.1f}m to {elevation_grid.max():.1f}m")
    print(f"  Time: {time.time() - t1:.1f}s")

    # ====================================================================
    # Stage 2: Fetch OSM data
    # ====================================================================
    print("\n[Stage 2] Fetching OSM data...")
    t2 = time.time()

    buildings_gdf = fetch_buildings(south, west, north, east)
    n_buildings = len(buildings_gdf) if buildings_gdf is not None else 0
    print(f"  Buildings: {n_buildings}")

    roads_gdf = fetch_roads(south, west, north, east)
    n_roads = len(roads_gdf) if roads_gdf is not None else 0
    print(f"  Roads: {n_roads}")

    water_gdf = fetch_water(south, west, north, east)
    n_water = len(water_gdf) if water_gdf is not None else 0
    print(f"  Water features: {n_water}")

    vegetation_gdf = fetch_vegetation(south, west, north, east)
    n_vegetation = len(vegetation_gdf) if vegetation_gdf is not None else 0
    print(f"  Vegetation features: {n_vegetation}")
    print(f"  Time: {time.time() - t2:.1f}s")

    # ====================================================================
    # Stage 3: Project to local UTM
    # ====================================================================
    print("\n[Stage 3] Projecting data to local coordinates...")
    t3 = time.time()

    utm_crs = bbox["utm_crs"]
    origin = bbox["origin"]
    utm_bbox = bbox["utm_bbox"]
    width_m = bbox["width_m"]
    height_m = bbox["height_m"]

    if buildings_gdf is not None and len(buildings_gdf) > 0:
        buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin,
                                             clip_bbox=utm_bbox)
    if roads_gdf is not None and len(roads_gdf) > 0:
        roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin,
                                         clip_bbox=utm_bbox)
    if water_gdf is not None and len(water_gdf) > 0:
        water_gdf = project_geodataframe(water_gdf, utm_crs, origin,
                                         clip_bbox=utm_bbox)
    if vegetation_gdf is not None and len(vegetation_gdf) > 0:
        vegetation_gdf = project_geodataframe(vegetation_gdf, utm_crs, origin,
                                              clip_bbox=utm_bbox)

    print(f"  Time: {time.time() - t3:.1f}s")

    # ====================================================================
    # Stage 4: Build terrain mesh (obj_4: terrain + water hollow)
    # ====================================================================
    print("\n[Stage 4] Building terrain mesh (obj_4: terrain + water hollow)...")
    t4 = time.time()

    if water_gdf is not None and len(water_gdf) > 0:
        has_roads = roads_gdf is not None and len(roads_gdf) > 0
        terrain_result = build_terrain_with_water_holes_manifold(
            elevation_grid, width_m, height_m, area_km2, scale,
            water_gdf,
            roads_gdf=roads_gdf if has_roads else None,
            enable_roads_fusion=has_roads,
            bridges_only=True,
        )
        terrain_solid = terrain_result["mesh"]
        print(f"  Terrain (with water holes) faces: {len(terrain_solid.faces):,}")
        print(f"  Watertight: {terrain_solid.is_watertight}")
        validation = terrain_result["validation"]
        print(f"  Validation: watertight={validation['watertight']}, "
              f"volume={validation['volume']:.2f} mm³")
    else:
        terrain_solid = build_deepseek_terrain(
            elevation_grid, width_m, height_m, area_km2, scale, water_gdf
        )
        print(f"  Terrain (no water data) faces: {len(terrain_solid.faces):,}")

    print(f"  Time: {time.time() - t4:.1f}s")

    # ====================================================================
    # Stage 4.5: 5 步预处理（geometry 减法 + 精度过滤）
    # ====================================================================
    print("\n[Stage 4.5] Preprocessing layers (subtraction + precision filter)...")
    t45 = time.time()

    bbox_local = (utm_bbox[0] - origin[0], utm_bbox[1] - origin[1],
                  utm_bbox[2] - origin[0], utm_bbox[3] - origin[1])
    layers = preprocess_layers(
        buildings_gdf=buildings_gdf,
        roads_gdf=roads_gdf,
        water_gdf=water_gdf,
        vegetation_gdf=vegetation_gdf,
        bbox_local=bbox_local,
        scale=scale,
        enable_hotspot=True,
        hotspot_relax=BUILDING_V2_HOTSPOT_RELAX,
        area_km2=area_km2,
    )
    print(f"  {layers.summary()}")
    print(f"  Time: {time.time() - t45:.1f}s")

    # ====================================================================
    # Stage 5: Build buildings (v3 — preprocessed polygons)
    # ====================================================================
    print("\n[Stage 5] Building buildings (v3)...")
    t5 = time.time()

    buildings_mesh = None
    landmarks_mesh = None
    if layers.BL or layers.BO:
        bldg_result = build_deepseek_buildings_v3(
            layers.BL, layers.BO, terrain_solid, scale)
        if isinstance(bldg_result, dict):
            landmarks_mesh = bldg_result.get("landmarks")
            buildings_mesh = bldg_result.get("buildings")
            n_lm = len(landmarks_mesh.faces) if landmarks_mesh is not None else 0
            n_amb = len(buildings_mesh.faces) if buildings_mesh is not None else 0
            print(f"  Landmarks mesh: {n_lm:,} faces (E5 暖砂石)")
            print(f"  Buildings mesh: {n_amb:,} faces (E1 灰，街区填充)")
        else:
            print(f"  No buildings generated (all filtered out)")
    else:
        print(f"  No building data available")
    print(f"  Time: {time.time() - t5:.1f}s")

    # ====================================================================
    # Stage 6: Build roads (v3 — preprocessed lines)
    # ====================================================================
    print("\n[Stage 6] Building roads (v3)...")
    t6 = time.time()

    if layers.roads_lines:
        try:
            roads_mesh = build_deepseek_roads_v3(layers.roads_lines, terrain_solid, scale)
            if roads_mesh is not None:
                print(f"  Road faces: {len(roads_mesh.faces):,}")
            else:
                print(f"  No roads generated")
        except Exception as e:
            print(f"  Roads processing failed (skipping): {e}")
            roads_mesh = None
    else:
        roads_mesh = None
        print(f"  No road data available")
    print(f"  Time: {time.time() - t6:.1f}s")

    # ====================================================================
    # Stage 7: Build water (v3 — preprocessed WL/WO)
    # ====================================================================
    print("\n[Stage 7] Building water plate (v3: base + WL/WO relief)...")
    t7 = time.time()

    if layers.WL or layers.WO:
        origin_x, origin_y = origin
        water_bbox_x_min = utm_bbox[0] - origin_x
        water_bbox_y_min = utm_bbox[1] - origin_y
        water_bbox_x_max = utm_bbox[2] - origin_x
        water_bbox_y_max = utm_bbox[3] - origin_y

        water_mesh = build_deepseek_water_v3(
            layers.WL, layers.WO,
            water_bbox_x_min, water_bbox_y_min,
            water_bbox_x_max, water_bbox_y_max, scale,
            terrain_mesh=terrain_solid)
        if water_mesh is not None:
            print(f"  Water faces: {len(water_mesh.faces):,}")
        else:
            print(f"  No water features generated")
    else:
        water_mesh = None
        print(f"  No water data available")
    print(f"  Time: {time.time() - t7:.1f}s")

    # ====================================================================
    # Stage 8: Build vegetation (v3 — preprocessed VL/VO)
    # ====================================================================
    print("\n[Stage 8] Building vegetation features (v3)...")
    t8 = time.time()

    if layers.VL or layers.VO:
        vegetation_mesh = build_deepseek_vegetation_v3(
            layers.VL, layers.VO, terrain_solid, scale)
        if vegetation_mesh is not None:
            print(f"  Vegetation faces: {len(vegetation_mesh.faces):,}")
        else:
            print(f"  No vegetation features generated")
    else:
        vegetation_mesh = None
        print(f"  No vegetation data available")
    print(f"  Time: {time.time() - t8:.1f}s")

    # ====================================================================
    # Stage 8.5: Build block_base (v3 — PNG layer 1.5 暖米色城市底)
    # ====================================================================
    print("\n[Stage 8.5] Building block_base (v3 — city tessellation)...")
    t85 = time.time()

    if layers.block_base:
        block_base_mesh = build_deepseek_block_base_v3(
            layers.block_base, terrain_solid, scale)
        if block_base_mesh is not None:
            print(f"  BlockBase faces: {len(block_base_mesh.faces):,}")
        else:
            print(f"  No block_base mesh generated")
    else:
        block_base_mesh = None
        print(f"  No block_base polygons available")
    print(f"  Time: {time.time() - t85:.1f}s")

    # ====================================================================
    # Stage 9: Export 3MF（buildings 已拆为 landmarks + ambient 两个 sub-mesh）
    # ====================================================================
    print("\n[Stage 9] Preparing & exporting 3MF...")
    t9 = time.time()

    # 不再 split — 整个 terrain solid 作为单一 watertight closed sub-mesh
    print(f"  Terrain solid faces: {len(terrain_solid.faces):,} (single closed mesh)")

    meshes = {
        "terrain": terrain_solid,
        "buildings": buildings_mesh,    # block_fill 街区填充 (E1 灰)
        "landmarks": landmarks_mesh,    # 地标个体 (E5 暖砂石)
        "roads": roads_mesh,
        "water": water_mesh,
        "vegetation": vegetation_mesh,
        "block_base": block_base_mesh,  # PNG layer 1.5 暖米色城市底 (E6)
    }

    export_deepseek_3mf(meshes, output_path)
    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  File size: {file_size_mb:.1f} MB")
    print(f"  Time: {time.time() - t9:.1f}s")

    # ====================================================================
    # Stage 10: Validate
    # ====================================================================
    print("\n[Stage 10] Validating...")
    validation = validate_3mf(output_path)
    print_validation_report(validation)

    # ====================================================================
    # Summary
    # ====================================================================
    total_time = time.time() - t_start
    print(f"Total pipeline time: {total_time:.1f}s")
    print(f"Output: {output_path}")

    return output_path


# CLI entry point
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="_TEXTURE_STYLE_OF_DEEPSEEK pipeline"
    )
    parser.add_argument("--lat1", type=float, required=True, help="South latitude")
    parser.add_argument("--lon1", type=float, required=True, help="West longitude")
    parser.add_argument("--lat2", type=float, required=True, help="North latitude")
    parser.add_argument("--lon2", type=float, required=True, help="East longitude")
    parser.add_argument("--output-dir", default="output/deepseek",
                        help="Output directory")
    parser.add_argument("--city-name", default=None,
                        help="City name for output file")

    args = parser.parse_args()
    run(args.lat1, args.lon1, args.lat2, args.lon2,
        output_dir=args.output_dir,
        city_name=args.city_name)
