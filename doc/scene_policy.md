# SceneCharacter V5 and ScenePolicy V4

Updated: 2026-08-28

This layer fills the gap between printer constraints and city-specific visual
expression.  It is deterministic, versioned, and auditable.  It does not use a
city-name lookup and it never authors mesh vertices, global Z values, boolean
operations, or replacement geometry.

## Runtime artifacts

Every `generate_city_legacy.py` preprocessing run now writes:

- `scene_character.json`: measured scene evidence;
- `scene_policy.json`: bounded perceptual roles and trade-off order;
- `composition_spec.json`: compact scene evidence plus the resolved policy;
- `design_spec.json`: the exact formal 3MF identity and its scene-policy record.

Policy v4 uses `activation=audit_only` by default. Its measurements and
decisions are part of the formal evidence chain. The only current opt-in
consumer is a bounded garden-city building-mass adapter; it preserves the
accepted baseline carrier, falls back when area is not retained, and remains
subject to the cross-scene gates below. No local classifier is allowed to
author mesh vertices, global Z or Boolean operations.

## Measurement boundary

`aesthetic/scene_character.py` measures four groups of evidence.

### Water topology

- component count, largest-component share and frame fraction;
- edge contacts, elongation, compactness and interior holes;
- waterway length and branch/confluence evidence;
- bounded coast, river-axis, water-network, island and confluence scores.

### Road structure

- local orientation concentration and entropy;
- frame-wide direction entropy and radial alignment;
- semantic and geometric ring evidence;
- grid, ring and radial scores;
- major and structural corridor length.

### Buildings

- footprint count and frame coverage;
- axis-width and aspect distributions;
- fraction narrower than the real-world nozzle footprint;
- count- and area-weighted independent-body survival;
- compactness, slender-body fraction, occupied-cell density, and transparent
  XY regularization pressure;
- trusted-height coverage, height-mass concentration and significant height
  cells.

SceneCharacter v5 additionally measures spatial distribution rather than
using frame-wide density alone. Each 8×8 cell receives road support, building
support, neighbour continuity, completeness and road/building contradiction.
The local representation is one of `neighborhood_mass`, `hybrid_mass`,
`block_base_support`, `preserve_printable_footprints`,
`open_space_preserve`, or `water`. Low density alone never enables Block base;
it requires an urban road/building contradiction plus neighbouring or
independent urban support.

### Terrain, landform and confidence

- DEM p05/p95 relief, relief-to-frame ratio and slope distribution;
- rugged and low-slope/buildable fractions;
- crop-relative isolated prominence, peak dominance and framing center;
- ridge elongation/continuity and repeated-cone evidence;
- closed crater/rim evidence separated from open canyon/valley evidence;
- open-plain evidence for sparse steppe/desert candidates;
- local OSM holes recorded separately from scene character so poor coverage is
  not mistaken for intentional negative space.

## Policy boundary

`aesthetic/scene_policy.py` converts the measured vector into continuous
scores for coast, river axis, water network, island, confluence, grid, ring,
radial structure, terrain, compact core, polycentricity and landform identity.
Compatible scores
resolve a bounded archetype such as:

- `coast_grid_compact_core`;
- `broad_river_compact_core`;
- `ring_axis_lowrise`;
- `water_network_lowrise`;
- `mountain_harbour_clustered`;
- `terrain_confluence`.

Natural-landscape archetypes are resolved through a separate role policy:

- `isolated_monolith`;
- `iconic_peak_ridge`;
- `peak_valley_network`;
- `crater_lake_rim`;
- `canyon_valley`;
- `volcanic_field`;
- `characteristic_shore_relief`;
- `open_steppe`.

For a landscape, DEM owns geometry evidence, urban Block base is a disable
candidate, water is a separate planar part sharing one shoreline with terrain,
and roads/buildings are restricted to sparse verified context.  These are
audited decisions only; no geometry consumer is active yet.

The policy assigns three building roles (`hero_identity`, `urban_mass`,
`quiet_texture`), separates visible road ink from Block-base structural cuts,
keeps major water as continuous negative space, and records whether terrain or
a compact building core should own the scene's height hierarchy. Building
grammar `adaptive-local-printable-city-mass-v4` chooses among preserving compact
bodies, selective merge/regularization, or full anonymous-mass merge based on
printer-scaled evidence. It describes low, standard, and trusted hero relief as
semantic layer-quantized roles; it does not emit literal Z values.

`scene-character-v6` also embeds `source-block-grammar-v1`: a deterministic
measurement of current-source short-axis grain, solidity, rectangularity,
perimeter excess, orientation coherence and spatial fullness in final-model
millimetres. `scene-policy-v6` converts it to bounded pressures for aggregation,
anonymous-outline regularisation, orthogonal-direction preservation, supported
quiet-texture promotion and dense-source selection. These pressures only tune
the existing BuildingMass policy. Printer floors, source support, road/water
topology clips, exclusion bodies and area-growth guards remain hard authority.

The visual grammar is `restrained-three-value-v1`:

- black structural negative: major water and a very small number of identity
  incisions;
- neutral substrate: terrain, block field and quiet urban mass;
- white relief texture: hero buildings, merged urban mass and low density
  texture.

## Trade-off contract

Hard constraints are evaluated first:

1. source truth;
2. print survival;
3. valid closed geometry.

Within those bounds, urban optimization priority is:

1. city identity;
2. dominant-structure continuity;
3. visual hierarchy;
4. mid-frequency density;
5. ordinary feature fidelity.

Ink is therefore a hard ceiling, not the definition of city quality.  Identity
structures receive a guaranteed role before remaining budget is assigned to
quiet context.

Landscape optimization instead prioritizes landform identity, silhouette/rim
continuity, water-terrain relationship, relief hierarchy, and only then sparse
human context.  It does not fill naturally empty land with urban mass.

## Activation gates

Before `scene-policy-v5` may control additional urban generation:

1. compare fixed 25 km crops for Chicago, Shanghai, Beijing, Suzhou and Hong
   Kong;
2. verify scene classification, dominant structure and data-confidence
   evidence by human review;
3. run material-only A/B before geometry changes;
4. then enable one consumer at a time: building roles, road roles, water roles,
   terrain/material presentation;
5. require a real 3MF, saved DesignSpec, slicer evidence and project validator
   `0 errors / 0 warnings` for every promoted policy revision.

AI or human visual review may evaluate candidates and tune future policy
versions.  Runtime geometry remains deterministic and bounded by project data
and the selected printer profile.

Before landscape consumers may activate, run the real DEM acceptance matrix
in `doc/natural_landscape_pipeline_plan.md`: Matterhorn, Uluru, Changbai
Tianchi, Qinghai Lake, an Alps crop, and selected Xinjiang/Inner Mongolia
frames.  Each must prove named-landform recognizability, framing completeness,
water/terrain mating, printable closed geometry, slicer survival, and validator
`0 errors / 0 warnings`.

## Initial real-cache diagnostic

The read-only Mac diagnostic reused existing trusted 25 km pipeline caches or
local PBF extraction caches; it did not download data and did not regenerate
geometry:

| Scene | Footprints | Count below nozzle | Area below nozzle | Regularization pressure | Quiet-texture need | Audit strategy |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Chicago | 701,974 | 99.568% | 79.868% | 0.7495 | 0.8873 | `merge_and_regularize` |
| West Lake | 35,375 | 92.430% | 55.500% | 0.6541 | 0.5462 | `merge_and_regularize` |
| Beijing | 60,326 | 92.489% | 64.119% | 0.7002 | 0.8034 | `merge_and_regularize` |
| Shanghai | 74,953 | 93.647% | 63.537% | 0.6774 | 0.7367 | `merge_and_regularize` |

All reports rated OSM internal consistency `high`. The count-versus-area gap is
important: most literal bodies fail independently, but a meaningful share of
their total area can survive after neighbourhood merge and regularization. This
supports the reference strategy of calm urban masses plus quiet texture rather
than deleting most building evidence.

These diagnostics also expose unresolved scene-classification work. Beijing's
fragmented OSM water evidence currently over-scores `water_network`, and the
cached runs contain no DEM. Therefore archetype and height ownership remain
provisional even though the building print-pressure evidence is useful. This is
one reason policy v4 remains `audit_only` by default.

## Ten-city spatial building diagnostic

A read-only 25 km diagnostic used local PBF or trusted project caches and did
not download new data. The measurement is local and deterministic; city names
are labels in this table, not resolver inputs.

| Scene | Footprints | Frame coverage | Completeness | Continuity | Coverage CV | Gap cells | Resolved distribution |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Chicago | 701,974 | 17.83% | 0.884 | 0.854 | 0.295 | 0 | continuous urban fabric |
| Paris | 883,516 | 23.66% | 0.882 | 0.852 | 0.490 | 0 | continuous urban fabric |
| Berlin | 380,767 | 13.58% | 0.863 | 0.829 | 0.519 | 0 | continuous urban fabric |
| New York | 519,920 | 14.25% | 0.866 | 0.829 | 0.538 | 0 | continuous fabric with large open area; water cross-check required |
| Beijing | 60,326 | 10.66% | 0.818 | 0.772 | 0.488 | 2 | mixed complete urban fabric |
| Shanghai | 74,953 | 9.42% | 0.778 | 0.723 | 0.609 | 2 | mixed complete urban fabric |
| West Lake | 35,375 | 5.50% | 0.756 | 0.695 | 0.894 | 4 | heterogeneous urban mosaic |
| Guangzhou | 32,195 | 5.87% | 0.704 | 0.635 | 0.941 | 5 | heterogeneous urban mosaic |
| Suzhou | 31,497 | 4.13% | 0.628 | 0.539 | 1.139 | 8 | fragmented or incomplete urban evidence |
| Cairo | 52,279 | 2.92% | 0.643 | 0.552 | 1.662 | 10 | fragmented or incomplete urban evidence |

This confirms the working hypothesis: total building density is insufficient.
Paris and Chicago support broad neighbourhood mass; Beijing and Shanghai need
mass with hybrid edges; West Lake and Guangzhou need local Block-base support
without filling every quiet cell; Suzhou and Cairo require cross-source gap
review before stronger abstraction. New York demonstrates the inverse guard:
a large empty band is preserved and sent to water/coast verification rather
than being filled as missing city.

## Experimental building-role consumer

`building-mass-candidate-v6` is the current deterministic consumer of the
audit policy. Its review API remains isolated; the formal path can consume it
only through explicit scene-policy activation and baseline-area fallback.

Its inputs are source building polygons, complete topology blocks, the selected
printer profile, and optional versioned scene-policy evidence. Its decisions
are bounded as follows:

1. measure regularization pressure at the real-world nozzle footprint;
2. coarsen only low-order road cuts whose occupied printable cores remain
   below the fixed physical component band; preserve major/visible roads and
   every water boundary;
3. rank occupied blocks by footprint density and count;
4. resolve sparse, quiet-texture, and urban-mass roles;
5. keep every result within a road/water topology block;
6. inset each side by at least half the final two-extrusion road seam plus
   simplification and numerical safety reserves;
7. subtract existing excluded identity bodies;
8. form multiple source-seeded neighbourhood clusters rather than replacing a
   complete dense block;
9. split long transitive/U-shaped unions and agglomerate only nearby small
   clusters whose actual clipped silhouette meets the compactness budget;
10. regularize shallow anonymous concavities toward softened convex or near-
   convex outlines, but never across a road, water, crop or protected-identity
   boundary;
11. require a printable morphological core after all shaping;
12. quantize relief through printer layers;
13. record inferred cluster area separately from literal source support;
14. allow the lower incomplete-OSM fill gate only with cross-source
    `neighborhood_mass` evidence and at least six source footprints;
15. retain any unresolved narrow or boundary-limited source component as
    low-relief quiet texture instead of widening, deleting, or presenting it
    as standard-height mass.

Before candidate shaping, `building-representation-router-v1` consumes the
same city-agnostic evidence and selects one of three carriers:

- `complete_source_union`: continuous or sufficiently complete mixed urban
  fabric uses source-seeded compact unions. Per-group hull growth remains
  capped at 12%; joining two already compact groups has a separately recorded
  35% cumulative cap. The 1.30 mm visual prior cannot force broad fill.
- `supported_cluster_infill`: heterogeneous/incomplete urban data with local
  urban support may use the stronger, separately measured cluster abstraction.
- `sparse_local_preserve`: very low coverage plus fragmented continuity keeps
  local settlements as quiet/source-supported texture and prohibits broad
  inferred mass. When an accepted baseline carrier exists, composition keeps
  that baseline unchanged instead of promoting an inferred candidate.

The router persists completeness, continuity, suspected gap fraction,
footprint coverage, thresholds and reason. It never consumes a city name and
cannot control mesh vertices, global Z values or Boolean operations.

The accompanying fixed-cache evaluator always reports
`human_review_required` and `formal_print_acceptance=not_evaluated`. Metrics
may show fragment, density or coverage regressions, but cannot promote a style.

The evaluator now composes candidate mass with the same accepted low-frequency
carrier used by the formal adapter. The earlier diagnostic accidentally
removed that carrier and therefore overstated West Lake's density loss. With
the corrected shared composition, Chicago gains 7.85% building-carrier area
and Beijing gains 2.42%, while Shanghai and West Lake fail the minimum area-
gain gate and fall back to their baseline masks. This is the intended safe
result: a local strategy may help a complete dense city without silently
hollowing a mixed scene. This
does **not** classify West Lake 25 km as `landscape`: the crop is a
water-dominant mixed urban scene and must retain Block base plus quiet city
mass while lake, river and shoreline own the composition. Existing `BL`
classification is also too broad; semantic/provenance-backed hero selection
and anonymous-BL demotion are the next required slice.

## Cross-source urban identity guard

Scene routing must not infer wilderness from one incomplete source. When the
mainland AMap style-7 reference is explicitly available, the audit records how
widely its road hierarchy is distributed across land cells and combines that
with OSM building evidence. One corridor through a mountain is insufficient;
a spatially distributed road network plus non-trivial vector buildings can
override a false `landscape` result. AMap remains read-only evidence and never
creates road/building geometry, mesh, Z values, or Boolean operations.

The exact West Lake 25 km cross-check on 2026-08-27 measured:

- 45,005 clipped OSM road features and 35,675 building features;
- no `possible_data_gap` cells in the 8×8 scene grid;
- 92.19% of usable AMap land cells containing road hierarchy evidence;
- 85.94% containing major/arterial evidence;
- 16.79% AMap green coverage across land and 9.88% OSM water coverage.

The resolved class is therefore `mixed`, with archetype
`water_terrain_garden_city`. Its bounded strategy retains urban Block base and
quiet mid-frequency city mass while lake, river, green relief and shoreline
own the visual composition. The pure natural-landscape strategy remains
disabled. Evidence is saved under
`output/scene_crosscheck_westlake_25km_v1/`.
