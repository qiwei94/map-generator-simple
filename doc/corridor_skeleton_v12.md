# Corridor-first road skeleton (v12)

## Problem

The original OSM road and water networks were generally continuous, but too
dense for a 15/25 km printable composition.  Applying the AMap raster mask to
individual OSM ways found a useful visual skeleton, but it also selected only
the locally overlapping fragments.  Later continuity restoration and ring
link exceptions then tried to repair the resulting gaps.  This produced
short branches, repeated patches and city-specific instability.

## Contract

`print-road-roles-v12.9` changes the unit of selection from an OSM feature to
a complete physical corridor:

1. Keep the full OSM candidate network for topology and Block base.
2. Build endpoint topology before reading any AMap score.
3. Pair source half-edges.  Each endpoint can have at most one continuation.
   A four-way junction becomes two through pairs; a T-junction becomes one
   through pair plus an independent branch.
4. Prefer a shared OSM identity, then natural direction and compatible road
   class.  Exact cross-name/class transitions are allowed; an aligned geometry
   gap is allowed only inside one explicit semantic identity.
5. Form complete source paths/rings from those pairs.
6. Use AMap overlap only as evidence for selecting and assigning the role of
   the complete corridor.  AMap pixels are never copied as replacement
   geometry.
7. Collapse overlapping parallel carriageways below the printable separation.
8. Reject a short corridor atomically using frame- and nozzle-scaled limits.
   Rejected roads remain available to topology and Block base.

No post-selection road tracer, coordinate patch, anonymous ring-link append,
mesh operation, Boolean operation or global Z decision is part of this pass.

## Why half-edge pairing matters

A simple union-find over every connected same-name road is unsafe.  One wrong
turn inside a cyclic urban network can merge hundreds of streets into a giant
component.  Half-edge pairing constrains every corridor component to a path or
ring instead of an arbitrarily branching road-network blob.

## Print-scale policy

The minimum complete-corridor spans are relative to the finished frame and
also bounded by the resolved nozzle footprint:

| Role | Minimum frame span | Minimum nozzle lengths |
|---|---:|---:|
| primary | 1.5% | 4 |
| secondary | 2.2% | 6 |
| context | 3.2% | 8 |

These are city-independent physical limits.  There are no city coordinates
or fixed target road-ink percentage in the matcher.

## Real-data evidence

All tests below used cached domestic data and disabled network fallback.

### Hangzhou 25 km, final composition

- source road lines: 42,729
- topology / structural candidates: 31,580 / 20,527
- AMap-guided visible selection: 1,949 of 13,711 candidates
- rendered road lines after extraction: 1,923
- complete water groups: 2 selected from 191; zero gap bridges
- review-only elapsed time: 186.4 seconds
- road-only skeleton selection is about 30 seconds; city-block and Block base
  work remains the dominant cost
- output: `output/domestic_hangzhou_25km_v12_8/`

The subsequent v12.9 removal of the anonymous ring-link append has no Hangzhou
geometry effect because that exception selected zero Hangzhou features.

### Beijing 25 km, road skeleton

- source lines in the exact frame: 53,841
- physical corridors: 4,419
- selected corridors / source features: 205 / 4,534
- cross-identity/class continuation pairs: 2,120
- elapsed time: 26.2 seconds
- output: `output/domestic_beijing_25km_v12_9/`

Removing the old post-selection anonymous ring-link exception removed 328
source features, but changed only about 52 raster pixels at 2048 px because
most were sub-pixel or overlapped the retained ring.  This is an architecture
cleanup, not evidence of a large visual improvement.

### Shanghai 25 km, road skeleton

- source lines in the exact frame: 52,685
- physical corridors: 2,506
- selected corridors / source features: 199 / 4,006
- cross-identity/class continuation pairs: 915
- elapsed time: 29.0 seconds
- output: `output/domestic_shanghai_25km_v12_8/`

Shanghai did not use the removed anonymous ring-link exception, so its v12.8
geometry is equivalent to v12.9 for this change.

## Water scope

The existing water selector already groups line candidates by complete named
water identity, selects at most a few city-defining corridors at large scale,
and treats printable surface polygons as the primary water evidence.  The
Hangzhou validation selected two complete line groups and required no synthetic
gap bridge.  Physical cross-name river topology is a separate follow-up; it
must not be mixed into the road change without multi-city evidence.

## Known limitations

- AMap is a read-only visual hierarchy reference, not authoritative vector
  geometry or road classification.
- Cross-identity joins require a factual shared OSM endpoint.  Deliberately
  refusing to invent a cross-name geometric gap can still leave a corridor
  split where OSM topology itself is broken.
- OSM source ways can still contain unusual terminal geometry.  The algorithm
  removes complete short corridors, not arbitrary terminal coordinates.
- The road-only diagnostic is for structure and timing; gallery acceptance
  must use the final image with water, buildings and Block base.
- Linux/cloud timings must not be presented as conclusions about a 16 GB Mac.

## Acceptance commands

```bash
PYTHONPYCACHEPREFIX=/private/tmp/map-pycache \
  .venv/bin/pytest -q -m "not slow"

PYTHONPYCACHEPREFIX=/private/tmp/map-pycache \
  .venv/bin/python tools/render_road_skeleton_diagnostic.py \
  --bbox 39.7911535,116.2610224,40.0172465,116.5537776 \
  --pbf pbf_cache/beijing-latest.osm.pbf \
  --output output/domestic_beijing_25km_v12_9/beijing_road_skeleton.png
```
