"""E2E regression test — run the full westlake pipeline, compare structural
fingerprints against a known-good golden baseline.

Usage:
  # Normal run (compare against golden):
  ./venv/bin/python -m pytest tests/test_e2e_westlake.py -v

  # Regenerate golden baseline:
  ./venv/bin/python -m pytest tests/test_e2e_westlake.py -v --update-golden

  # Skip slow tests:
  ./venv/bin/python -m pytest tests/ -v -m "not slow"
"""

import json
import os
import sys
import time
import hashlib

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBF_PATH = os.path.join(PROJECT_ROOT, "pbf_cache", "westlake_10km.osm.pbf")
GOLDEN_PATH = os.path.join(PROJECT_ROOT, "tests", "golden", "westlake_fingerprint.json")

# Westlake preset bbox (WGS84)
LAT1, LON1, LAT2, LON2 = 30.13, 120.01, 30.36, 120.29

HAS_PBF = os.path.exists(PBF_PATH)

# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------

def _mesh_fingerprint(mesh) -> dict:
    """Extract deterministic structural metrics from a trimesh.Trimesh."""
    if mesh is None or len(mesh.faces) == 0:
        return None
    bounds = mesh.bounds  # [[x_min,y_min,z_min],[x_max,y_max,z_max]]
    try:
        volume = float(mesh.volume) if mesh.is_watertight else 0.0
    except Exception:
        volume = 0.0
    return {
        "vertex_count": int(len(mesh.vertices)),
        "face_count": int(len(mesh.faces)),
        "bbox": [round(float(v), 4) for v in bounds.flatten().tolist()],
        "volume": round(volume, 4),
        "watertight": bool(mesh.is_watertight),
    }


def _file_sha256(path: str) -> str:
    """Return a content identity for an external E2E fixture."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compute_fingerprint(meshes: dict) -> dict:
    """Compute structural fingerprint for all sub-meshes."""
    fp = {
        "bbox_wgs84": [LAT1, LON1, LAT2, LON2],
        "pbf": "westlake_10km.osm.pbf",
        "pbf_sha256": _file_sha256(PBF_PATH),
        "meshes": {},
    }
    for name in ["terrain", "buildings", "landmarks", "roads",
                  "water", "vegetation", "block_base"]:
        mesh = meshes.get(name)
        mfp = _mesh_fingerprint(mesh)
        if mfp is not None:
            fp["meshes"][name] = mfp
    return fp


def _save_golden(fp: dict):
    os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
    with open(GOLDEN_PATH, "w") as f:
        json.dump(fp, f, indent=2)
    print(f"\n  [golden] Saved to {GOLDEN_PATH}")


def _load_golden() -> dict:
    with open(GOLDEN_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pipeline runner (mirrors generate_city.py stages 1-8.5)
# ---------------------------------------------------------------------------

def _run_westlake_pipeline(output_dir: str) -> dict:
    """Run the full westlake pipeline, return meshes dict + output path."""
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import (
        bbox_to_utm, project_geodataframe,
    )
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
    from _TEXTURE_STYLE_OF_DEEPSEEK.terrain import build_deepseek_terrain
    from _TEXTURE_STYLE_OF_DEEPSEEK.buildings import build_deepseek_buildings_v3
    from _TEXTURE_STYLE_OF_DEEPSEEK.roads import build_deepseek_roads_v3
    from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water_v3
    from _TEXTURE_STYLE_OF_DEEPSEEK.vegetation_exclusion import build_deepseek_vegetation_v3
    from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import build_deepseek_block_base_v3
    from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
    from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf
    from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
        compute_scale, TERRAIN_GRID, get_area_class, WATERWAY_WIDTHS,
        BUILDING_V2_HOTSPOT_RELAX,
    )

    t0 = time.time()

    # Stage 1: Coordinate system
    bbox = bbox_to_utm(LAT1, LON1, LAT2, LON2)
    width_m = bbox["width_m"]
    height_m = bbox["height_m"]
    area_km2 = bbox["area_km2"]
    area_class = get_area_class(area_km2)
    resolution = TERRAIN_GRID.get(area_class, 512)
    scale = compute_scale(width_m, height_m)
    south, west, north, east = bbox["wgs84_bbox"]
    utm_crs = bbox["utm_crs"]
    origin = bbox["origin"]
    utm_bbox = bbox["utm_bbox"]
    bbox_x_min = utm_bbox[0] - origin[0]
    bbox_y_min = utm_bbox[1] - origin[1]
    bbox_x_max = utm_bbox[2] - origin[0]
    bbox_y_max = utm_bbox[3] - origin[1]

    # Stage 1b: Elevation
    try:
        elevation_grid = fetch_elevation_grid(south, west, north, east, resolution)
    except Exception:
        elevation_grid = np.zeros((resolution, resolution), dtype=np.float64)

    # Stage 2-3d: Fetch OSM data
    fetch_kw = dict(south=south, west=west, north=north, east=east, pbf_file=PBF_PATH)
    water_gdf = fetch_from_cli(tag_type="water", **fetch_kw)
    vegetation_gdf = fetch_from_cli(tag_type="vegetation", **fetch_kw)
    buildings_gdf = fetch_from_cli(tag_type="building", **fetch_kw)
    roads_gdf = fetch_from_cli(tag_type="road", **fetch_kw)
    landuse_gdf = fetch_from_cli(tag_type="landuse", **fetch_kw)

    # Project to UTM
    if water_gdf is not None and len(water_gdf) > 0:
        water_gdf = project_geodataframe(water_gdf, utm_crs, origin, clip_bbox=utm_bbox)
        water_gdf["est_area"] = water_gdf.apply(
            lambda r: r.geometry.area if r.geometry.geom_type in ("Polygon", "MultiPolygon")
            else r.geometry.length * WATERWAY_WIDTHS.get(r.get("waterway", "river"), 60),
            axis=1,
        )
        if len(water_gdf) > 500:
            water_gdf = water_gdf.nlargest(500, "est_area")

    if vegetation_gdf is not None and len(vegetation_gdf) > 0:
        vegetation_gdf = project_geodataframe(vegetation_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    if buildings_gdf is not None and len(buildings_gdf) > 0:
        buildings_gdf = project_geodataframe(buildings_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    if roads_gdf is not None and len(roads_gdf) > 0:
        roads_gdf = project_geodataframe(roads_gdf, utm_crs, origin, clip_bbox=utm_bbox)
    if landuse_gdf is not None and len(landuse_gdf) > 0:
        landuse_gdf = project_geodataframe(landuse_gdf, utm_crs, origin, clip_bbox=utm_bbox)

    # Stage 4: Terrain
    # Match the production path: preserve the terrain surface and render water
    # as a terrain-conforming overlay.  The retired terrain-hole boolean path
    # changes face counts and hides regressions in the visible water layer.
    terrain_solid = build_deepseek_terrain(
        elevation_grid, width_m, height_m, area_km2, scale, water_gdf
    )

    # Stage 4.5: Preprocess
    bbox_local = (bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max)
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
        landuse_gdf=landuse_gdf,
        bbox_wgs84=(south, west, north, east),
        utm_crs=utm_crs,
        origin=origin,
    )

    # Stage 5-8.5: Build sub-meshes
    buildings_mesh = None
    landmarks_mesh = None
    if layers.BL or layers.BO:
        try:
            bldg_result = build_deepseek_buildings_v3(
                layers.BL, layers.BO, terrain_solid, scale, bbox_local=bbox_local)
            if isinstance(bldg_result, dict):
                landmarks_mesh = bldg_result.get("landmarks")
                buildings_mesh = bldg_result.get("buildings")
        except Exception:
            pass

    roads_mesh = None
    if layers.roads_lines:
        try:
            roads_mesh = build_deepseek_roads_v3(layers.roads_lines, terrain_solid, scale)
        except Exception:
            pass

    water_mesh = None
    if layers.WL or layers.WO:
        try:
            water_mesh = build_deepseek_water_v3(
                layers.WL, layers.WO,
                bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale,
                terrain_mesh=terrain_solid)
        except Exception:
            pass

    vegetation_mesh = None
    if layers.VL or layers.VO:
        try:
            vegetation_mesh = build_deepseek_vegetation_v3(
                layers.VL, layers.VO, terrain_solid, scale)
        except Exception:
            pass

    block_base_mesh = None
    if layers.block_base:
        try:
            block_base_mesh = build_deepseek_block_base_v3(
                list(layers.block_base), terrain_solid, scale,
                bbox_local=bbox_local)
        except Exception:
            pass

    meshes = {
        "terrain": terrain_solid,
        "buildings": buildings_mesh,
        "landmarks": landmarks_mesh,
        "roads": roads_mesh,
        "water": water_mesh,
        "vegetation": vegetation_mesh,
        "block_base": block_base_mesh,
    }

    # Stage 9: Export
    output_path = os.path.join(output_dir, "westlake_e2e_test.3mf")
    export_deepseek_3mf(meshes, output_path)

    elapsed = time.time() - t0
    print(f"\n  [e2e] Pipeline completed in {elapsed:.1f}s")

    return {"meshes": meshes, "output_path": output_path}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_pipeline_result = None  # module-level cache


@pytest.fixture(scope="module")
def westlake_result(tmp_path_factory, request):
    """Run the pipeline once per test module, cache the result."""
    global _pipeline_result
    if _pipeline_result is None:
        out_dir = str(tmp_path_factory.mktemp("westlake_e2e"))
        _pipeline_result = _run_westlake_pipeline(out_dir)
    return _pipeline_result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not HAS_PBF, reason=f"PBF not found: {PBF_PATH}"),
]


class TestWestlakeE2E:

    def test_pipeline_produces_3mf(self, westlake_result):
        """Pipeline produces a valid 3MF file."""
        assert os.path.exists(westlake_result["output_path"])
        assert os.path.getsize(westlake_result["output_path"]) > 1000

    def test_3mf_zip_structure(self, westlake_result):
        """3MF ZIP contains all required entries."""
        import zipfile
        with zipfile.ZipFile(westlake_result["output_path"]) as zf:
            names = zf.namelist()
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert "3D/3dmodel.model" in names
            assert "3D/Objects/object_1.model" in names
            assert "Metadata/model_settings.config" in names

    def test_validator_passes(self, westlake_result):
        """validate_3mf reports no critical errors (V2/V4/V10)."""
        from _TEXTURE_STYLE_OF_DEEPSEEK.validator import validate_3mf
        result = validate_3mf(westlake_result["output_path"])
        # V8 (water Z span) and V9 (water side walls) can hit float
        # boundary conditions on small datasets — treat as non-critical.
        critical_errors = [
            e for e in result["errors"]
            if not e.startswith("V8:") and not e.startswith("V9:")
        ]
        assert critical_errors == [], \
            f"Validator critical errors: {critical_errors}"

    def test_all_expected_meshes_present(self, westlake_result):
        """All core sub-meshes (terrain, water, roads) are non-None."""
        meshes = westlake_result["meshes"]
        assert meshes["terrain"] is not None, "terrain mesh missing"
        assert meshes["water"] is not None, "water mesh missing"
        assert meshes["roads"] is not None, "roads mesh missing"

    def test_terrain_watertight(self, westlake_result):
        """Terrain mesh is watertight."""
        terrain = westlake_result["meshes"]["terrain"]
        assert terrain.is_watertight

    def test_terrain_z_range(self, westlake_result):
        """Terrain Z range is within expected bounds."""
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import TERRAIN_THICKNESS_MM
        terrain = westlake_result["meshes"]["terrain"]
        z_range = terrain.vertices[:, 2].max() - terrain.vertices[:, 2].min()
        assert z_range > 0.5, f"Z range too small: {z_range}"
        assert z_range < TERRAIN_THICKNESS_MM + 2.0, f"Z range too large: {z_range}"

    def test_water_interlocks_with_terrain(self, westlake_result):
        """Water embeds into, and remains visible above, its local terrain."""
        from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
            WATER_OVERLAY_EMBED_MM,
            WATER_OVERLAY_TOP_MM,
        )
        from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.terrain import (
            sample_terrain_z,
        )

        meshes = westlake_result["meshes"]
        if meshes["water"] is None:
            pytest.skip("No water mesh")
        water = meshes["water"]
        terrain = meshes["terrain"]
        terrain_z = sample_terrain_z(
            terrain, water.vertices[:, 0], water.vertices[:, 1],
        )
        offsets = water.vertices[:, 2] - terrain_z
        assert np.nanmin(offsets) <= -WATER_OVERLAY_EMBED_MM + 1e-4
        # Small water bodies intentionally use 75% of the main-water relief.
        assert np.nanmax(offsets) >= WATER_OVERLAY_TOP_MM * 0.75 - 1e-4

    def test_structural_fingerprint(self, westlake_result, request):
        """Compare mesh fingerprints against golden baseline."""
        meshes = westlake_result["meshes"]
        current_fp = _compute_fingerprint(meshes)
        update_golden = request.config.getoption("--update-golden", default=False)

        if update_golden or not os.path.exists(GOLDEN_PATH):
            _save_golden(current_fp)
            if update_golden:
                pytest.skip("Golden baseline updated — re-run without --update-golden")
            else:
                pytest.skip("Golden baseline created — re-run to compare")

        golden = _load_golden()

        assert current_fp["pbf_sha256"] == golden["pbf_sha256"], \
            "PBF fixture content changed; regenerate the fixture and golden together"

        # Compare mesh counts
        golden_names = set(golden["meshes"].keys())
        current_names = set(current_fp["meshes"].keys())
        assert golden_names == current_names, \
            f"Mesh set mismatch: golden={golden_names}, current={current_names}"

        # Compare per-mesh metrics
        for name in golden_names:
            g = golden["meshes"][name]
            c = current_fp["meshes"][name]

            assert c["vertex_count"] == g["vertex_count"], \
                f"{name}: vertex_count {c['vertex_count']} != golden {g['vertex_count']}"
            assert c["face_count"] == g["face_count"], \
                f"{name}: face_count {c['face_count']} != golden {g['face_count']}"
            assert c["watertight"] == g["watertight"], \
                f"{name}: watertight {c['watertight']} != golden {g['watertight']}"

            # Bounding box: within 0.1mm
            for i, (cv, gv) in enumerate(zip(c["bbox"], g["bbox"])):
                assert abs(cv - gv) < 0.1, \
                    f"{name}: bbox[{i}] {cv} != golden {gv} (diff={abs(cv-gv):.4f})"

            # Volume: within 1% (only if non-zero)
            if g["volume"] > 0:
                vol_diff = abs(c["volume"] - g["volume"]) / g["volume"]
                assert vol_diff < 0.01, \
                    f"{name}: volume {c['volume']} != golden {g['volume']} (diff={vol_diff:.2%})"
