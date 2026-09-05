# Current engineering TODO

Updated: 2026-08-30

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

- [ ] Enable production email registration after the owner opens an Alibaba
  Cloud DirectMail account.  Verify a dedicated sending subdomain (ownership,
  SPF, DKIM, DMARC and MX), create a `trigger` sender and SMTP password, and
  place the credentials only in `/etc/map-generator/studio.env` (never Git).
  Before enabling forced login, bind a filed domain to the mainland API,
  enable HTTPS while keeping `AUTH_COOKIE_SECURE=1`, and test real delivery to
  QQ/163, Outlook and Gmail inboxes.  Keep `AUTH_REQUIRED=0` until SMTP,
  session persistence, guest-job claiming and the configured admin email have
  all passed end-to-end acceptance.
- [ ] Make Stage 10 3MF delivery slicer-native. The accepted West Lake artifact
  is manifold and passes V1–V14, but its centred XY / negative-bottom-Z
  coordinates are not placed automatically by Bambu Studio CLI. Export a
  printer-bed-compatible instance transform or positive-coordinate assembly,
  then verify the exact artifact without a temporary vertex-shifted copy.
- [ ] Complete Bambu multi-material project metadata. The current package maps
  parts to E1/E2/E3 but omits the full project filament/nozzle mapping; Bambu
  Studio 02.08.02.60 CLI crashes while slicing the three-material file. Add a
  minimal compatible `project_settings` payload (without hard-coding a user's
  printer), test exact material roles, and rerun the real slicer.
- [ ] Diagnose the real slicer's `floating cantilever` warning on the West Lake
  formal model. A geometry-identical single-extruder copy slices successfully
  at 0.12 mm (56 layers, 82.49 g), but this warning blocks final print PASS
  until it is localized and either fixed or proven to be a safe multi-body
  contact false positive.
- [x] Re-cut structural road corridors after all Block base rotation, shift,
  edge noise, and clipping. The final seam is derived from the printer profile
  as `max(min_gap_mm, 2 × extrusion_width_mm)` (0.84 mm for the default 0.4 mm
  profile), with measured intrusion evidence persisted in `design_spec.json`
  and enforced by validator rule V14. Historical 3MF files without the new
  evidence remain identifiable as legacy rather than being misreported as
  measured.
- [x] Restore bounded OSM mid-frequency road structure behind the AMap-backed
  primary skeleton.  `print-road-roles-v13.0` selects only complete existing
  corridors that add a bridge, crosslink, loop or frame axis; it rejects
  one-ended and near-parallel candidates and keeps additions visually quiet.
  Cross-city evidence: `doc/mid_frequency_corridors_v13.md`.
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
- [ ] Build a first-class natural-landscape product pipeline alongside the
  urban pipeline. Travel memories are not limited to cities: mountains, lakes,
  volcanoes, deserts, grasslands, and isolated landforms must not be treated as
  urban inputs with missing buildings. Scope, modes, data policy, 3MF parts,
  printability gates, and acceptance matrix are specified in
  `doc/natural_landscape_pipeline_plan.md`.

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

## P1 — Natural landscape product line

- Treat `urban` and `landscape` as sibling product pipelines, not as style
  presets sharing all geometry assumptions.
- Add deterministic landscape modes for isolated monoliths, volcanic lakes,
  lake shores, iconic peaks, mountain ranges, volcanic fields, and arid
  relief. An LLM may review composition but must never directly control DEM
  vertices, global Z mapping, or boolean operations.
- Make DEM and landform topology the primary evidence. Buildings and ordinary
  roads are optional context and must never be synthesized to compensate for
  naturally sparse human settlement.
- Disable urban `block_base` in landscape modes while retaining a closed,
  printable structural bottom plate.
- Add scale-aware 15/25/30 km framing. Preserve peak prominence, ridge
  continuity, crater rims, shoreline integrity, and flat water surfaces.
- Define landscape-specific three-material mappings rather than inheriting the
  urban building/road/water palette.
- Cache raw DEM, water vectors, masks, derived terrain evidence, and accepted
  artifacts on the Windows data vault using the existing provenance rules.
- Validate real scenes in increasing complexity: Matterhorn, Uluru, Changbai
  Tianchi, a Qinghai Lake shore frame, an Alpine peak-valley-lake frame, then
  selected Xinjiang and Inner Mongolia landforms.

Acceptance criteria:

- Sparse buildings and roads are reported as expected scene evidence, not as
  an urban-data failure.
- Water is planar and meets terrain without gaps, floating shells, or white
  shoreline omissions.
- The terrain is a closed printable solid with adequate bottom/wall thickness,
  bounded slopes, no isolated layers, and validator/slicer evidence.
- Composition review confirms a recognizable landform hierarchy rather than a
  generic noisy heightfield.
- Every output preserves its source, crop, DEM resolution, relief policy,
  material roles, feature counts, and validation evidence in DesignSpec.

## P0 — Finish the reference-style building-role consumer

- [x] Add a guarded formal consumer, currently
  `building-mass-candidate-v6` / `scale-aware-block-topology-v2`. It is
  active only when the versioned scene policy resolves a measured
  `water_terrain_garden_city`; inactive/error/area-loss cases preserve the
  accepted baseline. It changes BO polygons and layer-quantized relative
  relief only, never terrain vertices, global Z, booleans, road or water
  geometry.
- [ ] Keep the active consumer release-gated until a real 25 km garden-city
  3MF passes the project validator and slicer checks. PNG success and unit
  tests are not formal print acceptance.
- Reclassify current `BL`: retain only provenance-backed named/semantic or
  trusted-height anchors as `hero_identity`; demote ordinary large/printable
  buildings into neighbourhood `urban_mass`.
- [x] Replace complete density-guarded block fill with bounded source-seeded
  clusters. Record literal footprint support, inferred cluster area, recursive
  splits, agglomerations, regularity rejections and boundary-limited leaves;
  reject candidates that look clean only because unsupported fill dominates.
- [x] Add reference-measured silhouette gates: anonymous mass targets high
  solidity, low perimeter excess and few effective outline turns. Bounded
  simplify/soften/convex-hull candidates must stay inside road/water/hero safe
  clips and within area-change budgets. Real boundary concavities are retained
  and reported.
- [x] Route building abstraction by measured data quality rather than city
  name. Complete/continuous sources use a 12%-capped compact union;
  heterogeneous supported sources may use cluster infill; very sparse,
  fragmented sources preserve only local settlements. Persist the route,
  thresholds and measurements in A/B evidence.
- [ ] Raise the West Lake source-seeded component-width distribution without
  returning to full-block slabs. The current p50 is 0.811 mm: printable and
  visually calmer, but below the provisional 1.30 mm aggregation prior. Treat
  1.30 mm as a soft reference signal, not a hard polygon clamp.

### 2026-08-29 fixed-cache routing regression

`scene-policy-v5` and `building-mass-candidate-v5` were evaluated against
trusted 25 km caches without downloading data or changing the public service.
The review follows `map-model-review`: numeric checks can reject a regression,
but PNG similarity cannot grant print acceptance.

| Scene | Route | Composition | Land coverage | Raster components | Candidate inferred fraction | Width p50 | Solidity p50 | Two-sided seam |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Beijing | `complete_source_union` | composed, 1.0132x area | 0.3166 → 0.3198 | 3540 → 3022 | 0.4137 | 0.591 mm | 0.971 | 2.178 nozzles |
| Shanghai | `complete_source_union` | composed, 1.0147x area | 0.2902 → 0.2937 | 3307 → 2631 | 0.3719 | 0.578 mm | 0.975 | 2.175 nozzles |
| West Lake | `supported_cluster_infill` | composed, 1.0140x area | 0.2727 → 0.2767 | 1437 → 1071 | 0.6775 | 0.811 mm | 0.985 | 2.142 nozzles |
| Berlin | `complete_source_union` | guarded baseline fallback | unchanged 0.3969 | unchanged 6783 | rejected candidate 0.7737 | 0.585 mm | 0.853 | 2.294 nozzles |
| Cairo | `sparse_local_preserve` | policy-preserved baseline | unchanged 0.0685 | unchanged 1336 | candidate not eligible | 1.067 mm | n/a for final | 2.178 nozzles |

Interpretation:

- Beijing and Shanghai remove fragments without creating a dominant slab;
  their 0.58–0.59 mm median components remain below the reference-style soft
  prior, so both remain human-review candidates rather than promoted output.
- West Lake verifies the middle route: it lowers fragmentation and preserves
  the largest-component share, while its 67.75% inferred candidate fraction
  is still too high for automatic promotion. Guangzhou and Suzhou need the
  same fixed-cache regression on the Windows data node; their required cache
  bundle is not present on the controller Mac.
- Berlin proves the guard is necessary: the 12%-capped candidate cannot carry
  enough of a very dense, sub-nozzle city and is rejected. The accepted
  baseline remains byte-for-byte equivalent at raster level.
- Cairo is no longer allowed to turn weak, fragmented building evidence into
  stronger inferred urban mass. The route explicitly preserves the accepted
  baseline instead of relying on an accidental area-loss fallback.
- None of these PNG/geometry diagnostics is a real 3MF print verdict. Formal
  acceptance still requires saved DesignSpec, strict project validator
  `0 errors / 0 warnings`, and slicer evidence.

The fixed-cache evaluator now uses `building-mass-topology-cache-v2`. Its key
contains only source provenance and controls that actually affect road/water
topology. A v1 cache may be re-keyed only after exact provenance plus target
width, hard floor and boundary-inset verification; unrelated building-policy
version changes no longer force an expensive topology rebuild.

Verification: `.venv/bin/pytest -m 'not slow'` → `677 passed, 2 skipped,
11 deselected`; `git diff --check` passed.

### 2026-08-29 mid-frequency role regression

`building-mass-candidate-v6` keeps the v5 geometry boundaries and introduces
two separately measured operations: complete datasets may join two already
compact source groups with a 35% cumulative merge cap while the 12% per-group
hull cap remains; components that are still narrower than 60% of the soft
visual target or fail the silhouette policy are retained as two-layer
`quiet_texture` rather than widened, deleted, or presented at seven-layer
standard relief.

| Scene | Route / result | Safe merges | Standard mass (count / width p50 / solidity p50 / turns p50) | Land coverage | Raster components | Largest-component ink | Seam |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |
| West Lake | supported / composed | 229 | 483 / 1.141 mm / 0.993 / 7 | 0.2727 → 0.2770 | 1437 → 1073 | 0.02163 → 0.02128 | 2.142 nozzles |
| Beijing | complete / composed | 491 | 196 / 1.029 mm / 1.000 / 7 | 0.3166 → 0.3212 | 3540 → 2978 | 0.00609 → 0.00614 | 2.178 nozzles |
| Shanghai | complete / composed | 310 | 167 / 1.054 mm / 1.000 / 7 | 0.2902 → 0.2944 | 3307 → 2594 | 0.01861 → 0.01988 | 2.175 nozzles |
| Chicago | complete / guarded fallback | 44 | no accepted standard mass | unchanged 0.3782 | unchanged 14515 | unchanged 0.00402 | 2.297 nozzles |
| Berlin | complete / guarded fallback | 184 | rejected candidate: 26 / 1.028 mm / 0.994 / 8 | unchanged 0.3969 | unchanged 6783 | unchanged 0.01349 | 2.294 nozzles |
| Cairo | sparse / policy baseline | 0 | not eligible | unchanged 0.0685 | unchanged 1336 | unchanged 0.02953 | 2.178 nozzles |

The standard-relief silhouettes now sit inside the reference-demo solidity
range for West Lake, Beijing and Shanghai while their short-axis medians remain
below the provisional 1.30 mm prior. This is a useful hierarchy improvement,
not an aesthetic pass: human inspection is still required, and West Lake's
67.75% inferred candidate fraction remains too high for automatic promotion.
Chicago and Berlin confirm that the existing dense-city carrier still needs a
separate layered solution; v6 correctly preserves their accepted baselines.
No result in this table is formal print acceptance.

Verification: `.venv/bin/pytest -q -m 'not slow'` → `681 passed, 2 skipped,
11 deselected`; all six A/B runs used fixed local caches and the same 196 mm
model span. Timings are controller-Mac warm-cache diagnostics, not product
promises.
- [ ] Add a layered dense-city composition: preserve Chicago's quiet low-
  frequency urban carrier and place compact source-seeded mid-frequency mass
  above it without XY/volume overlap. The v4 Chicago candidate has reference-
  like shape (solidity p50 0.9913; 7-turn median) and valid 2.294-nozzle seams,
  but retains too little baseline carrier area and correctly triggers guarded
  fallback. Do not fix this by weakening silhouette rules or globally
  inflating clusters.
- Add a water-dominant mixed-urban density carrier. The first West Lake A/B
  reduced fragments but collapsed mid-frequency coverage to 4.40%, so it must
  not inherit the dense-city resolver unchanged. It must retain Block base and
  quiet urban mass; do not route West Lake 25 km through the pure natural-
  landscape pipeline.
- Retire the historical West Lake `prototype="landscape"` label after auditing
  its gallery/metric callers. Scene routing must use measured water/urban
  evidence and identify this crop as water-dominant mixed urban; do not let the
  old preset silently select landscape variants.
- [x] Add an audit-only cross-source urban identity guard. The exact West Lake
  25 km run combines distributed AMap road/green evidence with OSM buildings
  and resolves `mixed / water_terrain_garden_city`; it does not inject AMap
  raster geometry into the model.
- [x] Route the real web gallery through the same AMap cross-source evidence,
  OSM-vector corroboration and active scene policy as formal generation.
  West Lake 25 km now resolves `urban` for gallery variants and
  `mixed / water_terrain_garden_city` for semantic policy instead of inheriting
  the stale `prototype="landscape"` decision. A weak/single external corridor
  remains landscape.
- [ ] Complete the classification matrix on Beijing, Shanghai, Suzhou,
  Guangzhou and one true natural landscape before deleting the deterministic
  OSM-only fallback heuristic.
- [x] Preserve topology-derived blocks rather than reusing the already filtered
  final `block_base` as the mother geometry. The latter can cover much less
  area than the source building field (especially in Chicago).
- [x] Measure post-shape road/water boundary clearance and printable cores, not
  only configured inset values.
- [x] Add fixed-model-span scale-aware topology. West Lake and Chicago now
  dissolve only low-order cuts while preserving major/visible roads and water;
  final component p50 is 1.326/1.414 mm and measured two-sided seams are both
  2.142 nozzles. Detailed evidence: `doc/reference_city_demo_design_language.md`.
- [x] Add an explicit trusted local, version-keyed topology cache to the fixed-
  cache evaluator. West Lake topology preparation falls from roughly 300 s to
  a cache-hit full diagnostic of about 37 s on this Mac. This is a development
  result, not a 16 GB Intel Mac, Windows, cloud worker or production SLA.
- [x] West Lake 25 km active A/B (2026-08-27): baseline BO 1,596 → two-tier
  output 2,462 components; union-area gain 1.0945×; quiet/mass relief
  0.24/0.84 mm; observed two-sided seam >= 2.1 nozzle widths. Tightening the
  full-block evidence gate reduced density-guarded whole-block fills 726 → 524
  and inferred candidate-area share 80.4% → 72.9%. Review score improved
  5.81 → 6.03. This is visual/geometry evidence, not yet slicer acceptance.
- Promote one role at a time and rerun Chicago, Beijing, Shanghai, West Lake,
  Suzhou and a terrain/harbour city.

Acceptance criteria:

- no city-name lookup or LLM-authored geometry;
- no empty block is filled and no body crosses topology cuts;
- actual two-sided seam meets the selected printer profile after shaping;
- ordinary component count falls without erasing the dominant city identity;
- trusted heroes remain sparse and provenance-backed;
- real 3MF + saved DesignSpec + slicer evidence + validator 0 errors / 0 warnings.

## Explicit non-goals

- Do not hard-code “China = block base on” or “US/Europe = off”.
- Do not let an LLM control mesh vertices, global Z values, or boolean ops.
- Do not replace the current high-quality v0.2 visual pipeline wholesale with
  the less visually tuned geometry-foundation pipeline.
- Do not treat a successful process exit or a readable 3MF archive as proof of
  a correct printable model.
