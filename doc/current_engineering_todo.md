# Current engineering TODO

Updated: 2026-08-25

This file records active work only. Historical session notes remain under `doc/`.

## Progress checkpoint — 2026-08-18

Completed foundations on `agent/web-premium-studio`:

- [x] Dense-city cache/RSS fix; the formerly OOMing large Paris gallery now completes 4/4 styles.
- [x] Portable osmium runs through the active Python interpreter; native-free real extraction returned 823 road features.
- [x] Legacy full 3MF and fast gallery draft persist an atomic `design_spec.json` sidecar.
- [x] Terrain-draped vegetation splits point-touching/pinched topology into closed edge-manifold shells.
- [x] Validator V12 checks finite/in-bounds/closed edge-manifold vegetation instead of requiring horizontal faces.
- [x] Real 25.2 km² Paris 3MF accepted at 12/12 rules, 0 errors, 0 warnings.

Still open:

- [ ] Data-quality-aware `block-base-mode auto` policy and cross-region A/B acceptance.
- [ ] DesignSpec coverage for every 3MF-producing entry and explicit exact-vs-snap measurement scope.
- [ ] Remaining selective geometry backports: exact terrain interpolation, road footprint clipping,
  bridge separation, all-layer printability gates, and a small real structural golden fixture.
- [ ] Global PBF/DEM storage rollout; the existing ~80 PBF cache is not global coverage.
- [ ] Introduce a hybrid spatial-data layer: PostgreSQL/PostGIS for indexed,
  versioned vector geometry and derived corridor evidence; keep raw PBF, DEM,
  previews, GLB, PNG, and 3MF in data-disk/object storage. Start with domestic
  road/water/building layers and persist AMap-to-OSM complete-corridor matches
  so jobs and users can reuse them. Do not treat the database as a replacement
  for raw-source archives or as an automatic cartographic-quality solution.
- [ ] Overseas multi-source evidence fusion is designed but traffic-paused until
  the next billing month. Resume from `doc/global_data_fusion_plan.md`; do not
  start Overture/JRC/Hydro/Microsoft downloads before the user re-enables them.

Full evidence and handoff: `doc/session_2026_08_18_rescue_summary.md`.

## P0 — Data-quality-aware block base

Block base is a visual fallback for incomplete urban data. It must not be
enabled or textured merely because a city is in a particular country.

- Add `--block-base-mode auto|off|flat|textured` while preserving explicit
  user overrides.
- Resolve `auto` from measured input quality, not city or country names.
- Candidate signals: printable building-footprint coverage, occupied-block
  ratio, building density, semantic-classification coverage, and other urban
  evidence already present in the extracted data.
- Use a conservative policy: choose `off` only when building coverage is
  demonstrably strong; use `textured` only when semantic evidence is strong;
  otherwise fall back to `flat`.
- Save the requested mode, resolved mode, metrics, thresholds, policy version,
  and human-readable reason in `design_spec.json`.
- Add deterministic unit tests for sparse, medium, dense, and explicit-override
  cases. Include boundary-value tests around every threshold.
- Run real A/B validation on at least one sparse-data Chinese area and one
  dense-data US or European area. Compare geometry counts and slicer previews,
  not only command exit status.
- Keep the existing edge-retreat behavior after mode resolution.

Acceptance criteria:

- The same input always resolves to the same mode.
- Explicit `off`, `flat`, and `textured` never get silently overridden.
- Dense inputs can disable block base; sparse inputs retain useful infill.
- Every 3MF output is accompanied by its resolved `design_spec.json`.
- Project validation reports 0 errors / 0 warnings and required feature counts
  are non-zero where source data contains those features.

## P0 — Selective correctness backports from geometry-quality-foundation

Backport behavior and tests selectively; do not merge the branch wholesale.

- Portable osmium fallback when native `osmium` is absent, retaining the newer
  v0.2 water-relation fixes in `tools/osmium_pyosmium.py`.
- Exact regular-grid terrain interpolation so roads, buildings, water, and
  vegetation use the same triangle surface as the terrain mesh.
- Clip buffered road polygons to the printable terrain footprint.
- Union normal roads and bridges separately; raise bridge decks without
  incorrectly increasing their printable thickness or lifting a connected
  non-bridge network.
- Build terrain-draped vegetation as closed edge-manifold shells, including
  polygons with holes and components that touch only at a point.
- Extend validation from archive/schema checks to per-layer printability gates:
  closed/watertight, edge-manifold, finite coordinates, in-bounds geometry,
  non-zero expected feature counts, and sane Z ranges.
- Preserve a structural golden fingerprint for a small real fixture (feature
  counts, parts, bounds, face counts/tolerances) so “no exception” cannot pass
  as a successful generation.
- Keep dependency setup reproducible for both pip and conda, but benchmark on
  the target 16GB Mac before drawing performance conclusions.

## P1 — Evaluate, do not directly port

- Terrain-conforming water overlays: compare as an alternate water strategy,
  not a replacement for the current West Lake/Qiantang River presentation.
  The experiment must preserve river/lake hierarchy and material constraints.
- Terrain repair/decimation changes: retain the current fast simplification
  path unless the alternate path proves equal or better on memory, runtime,
  surface fidelity, and manifold output on the target Mac.
- Apply the geometry branch's fail-closed approach to textured block base:
  repair successfully, fall back to flat, or fail validation. Do not export a
  known non-watertight textured mesh as if it succeeded.

## Explicit non-goals

- Do not hard-code “China = block base on” or “US/Europe = off”.
- Do not let an LLM control mesh vertices, global Z values, or boolean ops.
- Do not replace the current high-quality v0.2 visual pipeline wholesale with
  the less visually tuned geometry-foundation pipeline.
- Do not treat a successful process exit or a readable 3MF archive as proof of
  a correct printable model.
