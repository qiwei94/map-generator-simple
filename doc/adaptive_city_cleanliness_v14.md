# Adaptive city cleanliness v14

## Problem

The v13 corridor-first selector restored complete OSM paths and removed the
most obvious mask fragments.  Real 25 km renders still exposed two
city-dependent failure modes:

- every city could receive up to 64 mid-frequency corridors even when the
  AMap-backed skeleton was already spatially dense;
- near-parallel carriageways and frontage roads were collapsed only when the
  two physical records shared the same semantic identity;
- named ponds as small as 50,000 m² could become primary black water masses,
  so cities with many mapped lakes accumulated distracting black noise.

The intent of v14 is to make the supplement budget depend on the composition
that already exists, not on a fixed city-wide ink target.  OSM coordinates,
topology and Block base inputs remain unchanged.

## Road policy

The 16 × 16 composition grid now resolves a deterministic supplement budget
from unserved cells.  One complete corridor is allowed per three unserved
cells, clamped to 20–64 corridors.  A dense skeleton therefore receives fewer
additions while a sparse frame still has room to recover topology-critical
crosslinks.  The resolved inputs and budget are written to the composition
evidence as `dynamic_budget`.

Parallel collapse now uses two real nozzle widths.  Same-identity components
retain the historical one-sided overlap rule.  Different identities require
both routes to overlap by at least 70%, preventing intersections and short
shared approaches from being removed.  The better-supported cartographic
route wins; all rejected roads remain available to topology and Block base.

## Water policy

At city scale, a name alone no longer promotes a small pond to primary water.
Primary surfaces require one of:

- Wikidata/Wikipedia identity;
- river/canal/riverbank semantics;
- at least 60% reference-map water support;
- landmark semantics plus at least 0.04% of the finished frame area.

Ordinary visible water must occupy at least 0.015% of the finished frame (and
still pass the nozzle-based printable-area floor).  Smaller water remains in
the source data used by terrain and block topology but is omitted from the
high-contrast material composition.  Demotions and drops are auditable in the
water-role evidence.

## Real-city evidence on the controller M1 Mac

These timings and counts describe the controller only; they are not Linux or
16 GB Intel Mac performance conclusions.

| City | v13 visible roads | v14 visible roads | v13 mid corridors | v14 mid corridors | dark pixels `<180` |
|---|---:|---:|---:|---:|---:|
| Guangzhou 25 km | 4,490 | 4,114 | 41 | 29 | 19.33% → 18.50% |
| Suzhou 25 km | 3,421 | 2,800 | 63 | 37 | 25.31% → 23.29% |
| Beijing 25 km regression | 4,975 | 4,087 | 38 | 20 | visually retained ring/radial hierarchy |
| Shanghai 25 km regression | 5,520 | 4,503 | 53 | 20 | Huangpu River remains continuous to the frame |

Candidate outputs:

- `output/domestic_guangzhou_25km_v14_clean/`
- `output/domestic_suzhou_25km_v14_clean/`
- `output/domestic_beijing_25km_v14_regression/`
- `output/domestic_shanghai_25km_v14_regression/`

## Tests

The focused road, water and preprocessing suites cover:

- dynamic budget contraction as the reference skeleton fills the frame;
- cross-identity parallel-strip collapse at the physical print scale;
- complete crosslinks, one-ended-branch rejection and corridor continuity;
- main-lake/main-river retention, named-pond demotion and isolated-water drop.

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/map-pycache \
  .venv/bin/pytest -q \
  tests/test_road_roles.py \
  tests/test_layer_preprocess.py \
  tests/test_water_roles.py \
  tests/test_water_supplement.py
```
