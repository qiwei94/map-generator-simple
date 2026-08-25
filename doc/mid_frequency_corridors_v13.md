# AMap backbone with OSM mid-frequency corridors (v13)

## Problem

The v12 corridor matcher fixed fragmented, pixel-by-pixel road selection by
matching AMap salience to complete OSM physical corridors.  It also made the
AMap reference an almost hard gate.  At 25 km this preserved the primary
skeleton but removed too much of the medium-scale street structure that gives
a city visual density and recognizable districts.

The v13 policy keeps the AMap-backed skeleton as the visual hierarchy and adds
a bounded second pass over existing OSM corridors.  The goal is not to restore
all roads.  It is to restore the few complete medium-frequency structures that
make the retained skeleton read as a connected city.

## Selection contract

`print-road-roles-v13.0` applies the following rules:

1. Build the same complete physical OSM corridors as v12 before selection.
2. Preserve the AMap-backed v12 corridors as the primary backbone.
3. Consider only complete OSM corridors with a name or reference identity.
4. Admit a candidate only when it adds topology: a component bridge,
   two-sided crosslink, closed loop, or backbone-to-frame axis.
5. Reject one-ended local branches, link-dominated corridors and near-parallel
   duplicates of the retained backbone.
6. Use a 16 x 16 occupancy grid with capacity two to distribute additions
   instead of concentrating ink in the city centre.
7. Cap the supplement at 64 complete corridors per frame.
8. Assign supplemented roads the quiet `context` role; their topological value
   does not permit them to compete with the primary hierarchy.

Every selected coordinate comes from an existing OSM corridor.  Buffers are
used only for topology and overlap queries.  The selector never traces AMap
pixels, invents road geometry, modifies mesh geometry, changes global Z, or
performs Boolean operations.

## Exact-frame AMap cache fallback

The reusable preprocessing path works in a snapped fetch frame.  Historical
diagnostics and gallery batches sometimes cached AMap salience against the
finished exact frame instead.  A snap-frame cache miss previously made the
formal generator silently fall back to the non-AMap road policy even though a
valid exact-frame cache existed.

The formal CLI now tries the snapped cache first and then the exact cache.  An
exact raster is always paired with its own exact local bounds; it is never
stretched over the larger snapped frame.  Raw OSM extraction still reuses the
snapped fetch cache, but exact-frame sources are clipped before topology,
salience and water-group ranking.  Frame-dependent composition decisions are
therefore made only from content that can appear in the finished map.  The
selected frame and fallback are persisted in composition evidence and in the
preprocess cache fingerprint.

This second boundary is important for water as well as roads.  A Shanghai
regression ranked water groups over the larger snap frame with an exact-frame
guide.  It selected an off-frame source group instead of Dianpu River and left
a long white gap in the Huangpu River after final clipping.  With exact-frame
composition restored, the rendered water-surface ratio recovered from
`0.020047` to `0.033498`; the independent exact-frame control was `0.033345`.
The Huangpu River is continuous in the fixed formal-path output:

- failing snap composition:
  `output/domestic_shanghai_25km_v13/domestic_shanghai_25km_v13_topdown.png`
- fixed formal snap-fetch / exact-composition path:
  `output/domestic_shanghai_25km_v13_fixed/domestic_shanghai_25km_v13_fixed_topdown.png`
- independent exact-frame control:
  `output/domestic_shanghai_25km_v13_exact/domestic_shanghai_25km_v13_exact_topdown.png`

## Cross-city road diagnostics

All runs used local PBF and cached AMap evidence on the controller M1 Mac.
These are measurements of this machine, not Linux or 16 GB Intel Mac
performance conclusions.

| City / frame | Source lines | v13 visible | Added corridors | Added source features | New 16 x 16 cells | Road diagnostic |
|---|---:|---:|---:|---:|---:|---:|
| Hangzhou 25 km | 42,729 | 3,993 | 64 | 1,985 | 32 | 24.9 s |
| Beijing 25 km | 53,841 | 5,552 | 53 | 1,018 | 35 | 39.9 s |
| Shanghai 25 km | 52,685 | 5,702 | 53 | 1,696 | 36 | 43.7 s |

Parallel-duplicate rejection removed 65 Hangzhou, 102 Beijing and 168 Shanghai
candidates.  One-ended rejection removed 512, 576 and 342 candidates
respectively.  The supplement therefore does not simply re-enable the old OSM
road set.

Hangzhou's full review-only pipeline completed in about 181 seconds, compared
with 186.4 seconds in the documented v12 run.  City blocks, building grouping
and Block base remain the dominant stages; the bounded road supplement did not
materially increase the complete Hangzhou runtime.

Beijing's first formal review exposed the snap/exact cache mismatch described
above and was rejected as invalid evidence.  After the cache fallback fix, the
same command reported `amap_salience: ready`, retained 4,975 rendered road
lines and completed in 275.9 seconds.  Its 261.7-second preprocessing stage was
dominated by Block base classification (106.3 seconds) and the two building
grouping passes (49.5 and 49.0 seconds), rather than preview rendering (2.1
seconds).  This is why adding a bounded road pass does not multiply the total
generation time even though the road-only diagnostic itself is slower.

Outputs used for visual review:

- `output/domestic_hangzhou_25km_v13/domestic_hangzhou_25km_v13_topdown.png`
- `output/domestic_beijing_25km_v13_exactguide/domestic_beijing_25km_v13_exactguide_topdown.png`
- `output/domestic_beijing_25km_v13/beijing_road_skeleton.png`
- `output/domestic_shanghai_25km_v13/shanghai_road_skeleton.png`

## Tests

The unit suite covers three critical boundaries:

- a complete, previously unmasked crosslink is restored without coordinate
  changes;
- a one-ended branch is rejected;
- a near-parallel alternative is rejected.

The snap/exact cache tests prove that an exact reference is requested with its
own paired projected bounds after a snapped-cache miss and that reusable raw
sources are clipped to the finished frame before composition ranking.

```bash
PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q -m 'not slow'
# 513 passed, 2 skipped, 11 deselected
```

## Known limitations

- This is a cartographic selection policy, not a repair for factually broken
  OSM topology.
- Names and refs can be inconsistent; physical half-edge grouping remains the
  guard against giant semantic components.
- The 64-corridor cap is a safety bound, not a target.  Dense cities usually
  stop earlier because grid cells saturate.
- Visual acceptance still requires the complete top-down composition.  The
  road diagnostic alone cannot judge water, buildings or Block base.
- This branch is an experiment until the reviewed domestic outputs replace
  the current online gallery; no cloud service is changed by this work.

## Reproduction commands

```bash
PYTHONPYCACHEPREFIX=/private/tmp/map-pycache \
  .venv/bin/python tools/render_road_skeleton_diagnostic.py \
  --bbox 30.1319535,120.0200179,30.3580465,120.2799821 \
  --pbf pbf_cache/zhejiang-latest.osm.pbf \
  --output output/domestic_hangzhou_25km_v13/hangzhou_road_skeleton.png

PYTHONPYCACHEPREFIX=/private/tmp/map-pycache \
  .venv/bin/python generate_city_legacy.py \
  --bbox 30.1319535,120.0200179,30.3580465,120.2799821 \
  --pbf pbf_cache/zhejiang-latest.osm.pbf \
  --city domestic_hangzhou_25km_v13 \
  --auto-params --draft --review-png --review-only \
  --amap-salience cache --no-cache
```
