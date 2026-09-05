# Terrain quality diagnostic

`tools/generate_terrain_diagnostic.py` isolates the terrain stage from the
city-generation pipeline. It fetches or reads a DEM, runs the same formal
terrain builder used by the product, and emits five review artifacts:

- a terrain-only 3MF containing the actual printable solid;
- a GLB for interactive inspection;
- a two-panel PNG with final-mesh hillshade and an oblique height view;
- `terrain_evidence.json` with the grid, elevation mapping, topology, printer
  profile, timing, and artifact-space measurements.
- `design_spec.json`, so the project validator can check the terrain-only 3MF
  against the same declared artifact contract as a full product run.

The diagnostic deliberately excludes buildings, roads, water meshes,
vegetation, and Block base. It is the fast gate before a full city rerun.

## Usage

West Lake with the built-in bbox:

```bash
.venv/bin/python tools/generate_terrain_diagnostic.py \
  --preset westlake \
  --fetch-mode tiled \
  --output-dir output/westlake_terrain_precision
```

Any other area:

```bash
.venv/bin/python tools/generate_terrain_diagnostic.py \
  --bbox 41.76,-87.77,42.00,-87.49 \
  --name chicago \
  --output-dir output/chicago_terrain_diagnostic
```

The default and production-equivalent fetch mode is `tiled`. `--fetch-mode
direct` is a control path for comparing the stitched tile result with a direct
local DEM read. An offline GeoTIFF may be selected with `--elevation-file
PATH`, which requires direct mode.

## Formal terrain gates

The current surface policy is `terrain-surface-plan-v2` and its regular-grid
topology remains `regular-printer-grid-v1`:

- QEM decimation must be disabled for formal terrain;
- grid spacing is resolved before triangulation;
- maximum triangle XY edge comes from the printer profile's dedicated terrain
  sampling bound. The balanced 0.4 mm profile uses `1.6 × 0.42 = 0.672 mm`,
  producing a nominal square-grid cell of about `0.475 mm`;
- `faces_over_2mm` and `faces_over_5mm` must both be zero;
- the terrain solid must be watertight;
- height mapping must record its robust source percentiles, resolved gamma,
  natural-scale relief, output relief, and final artifact Z span.
- the stitched DEM must record requested/tile resolution, source identity,
  effective spacing, cache hits and the physical smoothing distance;
- low-relief scenes may receive deterministic physical low-pass conditioning,
  but hilly scenes must preserve the source grid exactly. Random terrain
  texture is prohibited.

The PNG is a visual diagnostic, not a substitute for the numeric gates. The
right panel expands Z for inspection and is labelled accordingly; physical
height decisions use the recorded millimetre values.

## West Lake regression that created this tool

The rejected terrain used two QEM passes and contained a maximum top-triangle
XY edge of about `29.4 mm`, with 157 triangles over `5 mm`. The regular-grid
diagnostic first reduced the maximum to `0.840 mm`, with zero faces over
`2 mm`. The precision hardening then reduced the production bound to
`0.672 mm` without reintroducing QEM.

## 2026-09-02 West Lake precision baseline

The former production tile path always sampled each `0.05°` tile at `61×61`,
roughly 90 m per cell around Hangzhou, ignored the requested resolution, then
upsampled the blurred grid. The repaired path derives tile resolution from the
requested stitched grid; the West Lake run used `184×184` tiles, local SRTM1,
about 28 m effective spacing, and a single post-stitch smoothing pass capped at
60 m physical distance. Cache identity now includes requested resolution,
source fingerprint and sampling-policy version, so installing a better DEM
cannot silently reuse a stale low-resolution tile.

Production-tiled and direct local-DEM controls now agree within about
`0.010 mm` artifact Z span and `0.0001 mm` detail RMS. The accepted terrain-only
artifact is `output/westlake_terrain_precision_conditioned_v9/`:

- source grid `841×1024`, formal grid `373×414`;
- `155,992` vertices and `311,980` faces in the complete solid;
- maximum top-surface XY edge `0.671 mm`;
- zero faces over `2 mm` or `5 mm`;
- artifact Z span `3.497 mm`;
- project validator: `0 errors / 0 warnings`.

The corresponding full model is
`output/westlake_terrain_precision_full_v1/`. Its S9 mesh gate and independent
V1–V17 validator both pass with zero errors and zero warnings. Terrain peak Z
is `1.897 mm`; the highest landmark top is `0.398 mm`, preserving terrain as
the scene's visual height owner.
