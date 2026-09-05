# Reference city-demo design language

Updated: 2026-08-28

This note reverse-engineers transferable design principles from the physical
city-model references stored in the Windows `city_demo` library. It does not
authorize copying another creator's typography, branding, photography, or
exact artwork. The goal is to learn the visual grammar and express it through
this project's own data, DesignSpec, printable geometry, and validation rules.

## Evidence and scope

The Windows library currently contains 33 city folders. Two learning cohorts
have been inspected. Cohort 1 contains Chicago, New York, London, Paris,
Shanghai, Suzhou, Beijing, Singapore, Hong Kong, and Chongqing. Cohort 2
contains Tokyo, Xiamen, Guangzhou, Cairo, Hangzhou, Berlin, Los Angeles, Rome,
Moscow, and Athens. Together they span orthogonal grids, river cities, water
networks, coastlines, islands, ring/radial structure, flat low-rise fields, and
strong terrain. The remaining 12 folders are held out for evaluation and must
not be used to tune the first policy implementation.

Chicago and every cohort-2 3MF use the same restrained material family: white
`#FFFFFF`, neutral grey `#8E9089`, and black `#000000` (Los Angeles carries an
additional duplicate white channel). Their project settings specify a 0.4 mm
nozzle, 0.16 mm layer height, 0.42 mm line/outer-wall width, two wall loops, and
thin-wall detection disabled. These are direct package measurements rather
than estimates from photographs.

This is a visual/design study, not print acceptance for our artifacts. Formal
acceptance still requires a real 3MF, saved `design_spec.json`, slicer evidence,
and the project validator reporting zero errors and zero warnings.

## The design grammar

### 1. One city, one dominant geographic sentence

The successful references do not ask every layer to speak equally:

- New York is the long island and surrounding water voids.
- London is the repeated bend of the Thames inside a quiet urban field.
- Paris is the Seine loop plus a radial street grain.
- Shanghai is the Huangpu S-curve dividing two urban fabrics.
- Suzhou is a dense canal/lake network with multiple black water anchors.
- Beijing is a rectilinear/ring field with a restrained central axis.
- Singapore is a compact coast and harbour composition.
- Hong Kong is an island/harbour/terrain silhouette before it is a road map.
- Chongqing is a confluence and terrain composition before it is a building map.

A 25 km crop works when it contains this full sentence. More detail is not a
substitute for a meaningful divider, bend, ring, coastline, confluence, or
landform.

### 2. Three visual roles, not seven competing data classes

The physical pieces read as three value roles:

1. **Black negative space / structural cut** — sea, lake, major river and a
   small number of identity-defining incisions.
2. **Neutral substrate** — terrain, block field and quiet urban mass.
3. **White relief / fine texture** — buildings and the dense low-frequency
   grain that catches light.

The current GLB preview exposes terrain, block base, vegetation, water, roads,
buildings, landmarks, and world as separate colours. That is useful for
diagnosis, but it reads like a GIS/game board. The reference family is more
coherent because semantic layers collapse into a few perceptual roles.

### 3. Density comes from quiet texture, not loud corridors

Fine streets, parcel/block boundaries, and low building relief create a
continuous silver-white texture. Major roads are not all promoted into dark,
equally thick ribbons. This lets a dense city still feel intricate without
becoming visually black.

For our pipeline this means:

- keep complete road/water identities for topology;
- use minimum printable gaps and low relief for supporting texture;
- reserve heavy contrast for a small structural skeleton;
- do not restore density by adding isolated fragments or widening every road.

### 4. Height is selective and compressed

The oblique photographs show relief, but most of the city remains low and
consistent. A compact core or a handful of landmarks can rise above it. Our
current Chicago GLB instead contains many tall columns spread across the whole
frame, making height look like raw data rather than composition.

The transferable hierarchy is:

- exact or high-confidence landmark height for a small named set;
- bounded core emphasis for the skyline cluster;
- compressed background-building height;
- very low block/road texture outside identity areas.

#### Chicago 25 km slender-building audit

The accepted-height-roles draft GLB confirms that the current mismatch is not
only subjective:

- `landmarks` contains 2,664 disconnected building bodies. 1,352 (50.8%) have
  an axis-aligned minimum XY width below the 0.42 mm extrusion width. Their
  median minimum width is 0.418 mm while median height is 3.12 mm; median
  height/minimum-width is 8.5.
- `buildings` contains 13,622 disconnected low-relief bodies. 12,324 (90.5%)
  are narrower than 0.42 mm and 9,375 (68.8%) have XY aspect ratio above 4.
  They are only 0.625 mm high, but still read as long splinters rather than a
  calm urban field.
- At this crop scale, 0.42 model mm represents about 54 real metres. Exact
  one-building/one-footprint preservation is therefore incompatible with a
  normal 0.4 mm FDM process for a large fraction of the source buildings.

The current narrow-building rule is insufficient: it lowers Z for only some
exact-height buildings. It does not regularize XY, demote anonymous narrow
bodies, or reject slivers created after block intersection and road/water
clearance.

The reference-compatible building policy should therefore be:

1. **Hero identity buildings** — retain only named/high-confidence identity
   evidence; regularize undersized footprints with a bounded, orientation-
   preserving minimum-width algorithm and cap unsupported height/slenderness.
2. **Urban mass buildings** — merge anonymous footprints into contiguous,
   printable neighbourhood masses rather than extruding thousands of narrow
   individuals.
3. **Quiet density texture** — encode remaining density as low relief or block
   texture; it must not become tall independent needles.
4. **Post-subtraction sliver guard** — after all block, road, water, and crop
   operations, apply minimum-width/compactness checks again. Area alone is not
   a sufficient printability test.
5. **Hard evidence** — DesignSpec and the validator should record counts below
   extrusion width, high-aspect bodies, height/width outliers, demotions,
   regularizations, and removals. A formal printable artifact should fail when
   non-exempt bodies violate the selected printer profile.

#### Implemented West Lake height-role evidence

The 25 km `water_terrain_garden_city` acceptance run now applies that policy to
the final clipped geometry rather than only to source footprints:

- anonymous `background_stylized` footprints are routed into city mass;
- sub-0.42 mm hero fragments are demoted after exact-frame clipping;
- independent hero height/short-axis is capped at 4;
- terrain-owned scenes cap hero absolute top below the terrain peak and reject
  a flat/missing DEM before formal export.

The accepted artifact contains 37 hero components: 0 below 0.42 mm, 0 above a
4:1 height/width ratio, median width 0.655 mm, median height 1.440 mm, maximum
absolute hero Z 0.823 mm versus terrain peak 1.518 mm. Quiet and urban mass
remain at 0.24 mm and 0.84 mm, close to the measured Hangzhou reference tiers
of 0.2142 mm and 0.8002 mm. This matches the reference hierarchy without
copying reference geometry.

This is a bounded algorithm/DesignSpec policy. An LLM may review whether the
hierarchy reads correctly, but must not directly set vertices or global Z.

#### Region-first height-mass experiment

The next A/B reverses the historical building-first order without copying any
reference geometry.  Source hero footprints are returned to the building
field before topology-bounded aggregation.  Four measured urban emphasis
zones then promote twelve existing aggregate components, with no new
footprints and no cross-road/water merging.  Trusted source height is consumed
only as a zone-level relative signal; final relief is quantized to printer
layers and remains subject to terrain peak ownership.

For the same West Lake 25 km frame, the result changes 37 narrow individual
hero components (median width 0.655 mm) into 12 aggregate height masses
(median width 1.495 mm).  Both versions have zero sub-extrusion components and
zero height/width ratios above 4, but the region-first version reduces maximum
absolute hero Z from 0.823 mm to 0.400 mm while terrain remains at 1.518 mm.
The strict V1–V17 validator passes with zero errors and warnings.  This is
evidence for the design hypothesis, not proof of the proprietary reference
construction method and not yet a default production policy.

### 5. Water is composition space

Water is normally the darkest, cleanest field. It is not filled with equal-width
minor channels, label artifacts, or grey noise. Large water establishes the
silhouette; medium water supports identity; minor water survives only when it
adds a coherent local network and remains printable.

The web/GLB preview may use a restrained blue-grey to distinguish real sea from
the world background. The printable three-material profile should still test a
black/recessed water role because that is where the reference pieces obtain
their strongest contrast.

### 6. Soft physical presentation supports the object

The reference photographs use warm neutral surroundings, diffuse side light,
matte materials, and shallow depth of field for close presentation views. These
choices make white relief legible without introducing extra colours.

The whole-city flyover has a different job: it should keep the complete city
legible. Its reviewed defaults therefore retain 35 mm, a 30-degree downward
pitch, a distant coast/river trajectory, stable helicopter motion, and depth of
field disabled. A close macro shot can be a separate deliverable.

## What should change in our pipeline

### Ten-city adaptive grammar

The ten reviewed references do **not** use one uniform city treatment. They
share a restrained material family and a common low/mid/hero vocabulary, then
shift emphasis according to measurable scene structure. The observations
below come from the supplied model photographs; exact heights and dimensions
cannot be recovered from perspective photographs and are therefore not stated
as measurements.

| City | Observed dominant treatment | Building treatment inferred from the photographs | Deterministic profile target |
| --- | --- | --- | --- |
| Chicago | lake edge + orthogonal grid + one compact skyline | dense rounded low relief across the land field; tall bodies concentrated near the lakefront core | `coast_grid_compact_core` |
| New York | island silhouette + broad water voids + multiple urban cores | almost continuous low texture; height concentrated in a small number of Manhattan waterfront clusters | `island_metropolis_clustered_core` |
| London | meandering river through a quiet irregular field | predominantly low and even; only sparse local height accents around identity areas | `meandering_river_lowrise` |
| Paris | Seine loop + radial/irregular street grain | very low continuous texture; isolated monuments do not turn the whole centre into a skyline | `river_radial_lowrise` |
| Shanghai | broad S-shaped river splitting two urban fabrics | low background with a compact riverfront high-rise group; water remains the principal divider | `broad_river_compact_core` |
| Suzhou | lakes and canal network + fine low-rise grain | nearly uniform low building relief; density and water seams matter more than height | `water_network_lowrise` |
| Beijing | orthogonal/ring/axis order + sparse water | broad, nearly level urban carpet; identity is carried by footprints, rings and axes rather than many towers | `ring_axis_lowrise` |
| Singapore | coast/harbour/islands + several developed nodes | several moderate coastal height clusters separated by large quiet low-rise fields and water | `coastal_polycentric` |
| Hong Kong | mountain/harbour/island silhouette | terrain owns most Z; buildings form compact waterfront clusters and remain subordinate to landform | `mountain_harbour_clustered` |
| Chongqing | two-river confluence + strong ridges/slopes | terrain and river bends dominate; irregular building texture follows buildable slopes without becoming a uniform tall forest | `terrain_confluence` |

#### Read-only 3MF evidence

All ten reference projects contain a real 25 km 3MF. A read-only streaming
inspection on the Windows source machine found the same nominal XY envelope
(approximately 254 × 254 mm) and the same package structure (two mesh-bearing
model entries) for every city. The geometry inside that common envelope is not
uniform:

| City | Vertices | Triangles | Total Z span (mm) | Distinct Z values at 0.01 mm |
| --- | ---: | ---: | ---: | ---: |
| Chicago | 275,666 | 516,260 | 5.643 | 334 |
| New York | 115,433 | 201,970 | 4.875 | 355 |
| London | 302,804 | 579,116 | 4.419 | 336 |
| Paris | 207,169 | 377,482 | 3.970 | 329 |
| Shanghai | 55,217 | 91,672 | 6.208 | 274 |
| Suzhou | 176,440 | 324,610 | 3.610 | 325 |
| Beijing | 318,199 | 597,206 | 4.242 | 316 |
| Singapore | 159,010 | 303,624 | 4.178 | 295 |
| Hong Kong | 608,553 | 1,206,658 | 9.824 | 801 |
| Chongqing | 725,141 | 1,423,244 | 5.756 | 558 |

The coordinate scan does not separate terrain Z from building Z, so it cannot
prove individual building heights. Combined with the photographs, however, it
strongly supports different geometric emphasis: Hong Kong and Chongqing carry
far more terrain/height complexity; Suzhou and Paris are substantially flatter;
Shanghai achieves the second-largest non-terrain-city Z span with by far the
fewest triangles, consistent with a sparse compact skyline rather than uniform
height everywhere.

The source meshes use a nominal 254 mm envelope and are assembled at roughly
0.7874 scale, producing the documented 200 mm finished city. At 25 km over
200 mm, a 0.42 mm line represents about 52.5 real metres. Even these references
cannot preserve ordinary building footprints one-for-one. Their photographs
show systematic footprint regularization, merging and low-relief abstraction,
not literal raw-building extrusion.

#### Cohort-2 building simplification evidence

The read-only audit tool `tools/analyze_reference_3mf_style.py` parses material
roles, connected components, XY widths and Z spans without importing reference
geometry into the generator. All ten cohort-2 models contain one full-frame
black backing component. Major water and other structural negative space are
formed by omission from the upper grey/white layers, so the backing is exposed
as one continuous dark field rather than thousands of separately extruded
water fragments.

Every city contains two white relief populations: a quiet tier with median
component height between 0.196 and 0.328 mm, and a standard urban-mass tier
with a median height of approximately 0.8 mm. These are object-level component
medians, not proposed Z constants for our pipeline.

| City | White components | Weighted component width p50 (mm) | White height p50 tiers (mm) | Below 0.42 mm |
| --- | ---: | ---: | --- | ---: |
| Tokyo | 8,590 | 1.4439 | 0.1962 / 0.8000 | 0.117% |
| Xiamen | 2,091 | 1.9758 | 0.2219 / 0.8000 | 0.048% |
| Guangzhou | 3,408 | 2.3281 | 0.2089 / 0.8000 | 0.030% |
| Cairo | 4,682 | 1.9952 | 0.3281 / 0.8000 | 0.085% |
| Hangzhou | 5,527 | 1.7109 | 0.2142 / 0.8002 | 0.000% |
| Berlin | 8,622 | 1.5755 | 0.2846 / 0.8001 | 0.035% |
| Los Angeles | 10,414 | 1.3581 | 0.3090 / 0.8000 | 0.346% |
| Rome | 4,862 | 2.1027 | 0.2818 / 0.8000 | 0.021% |
| Moscow | 5,217 | 2.0955 | 0.3013 / 0.8000 | 0.000% |
| Athens | 4,636 | 1.9772 | 0.3090 / 0.8000 | 0.000% |

This near-total survival while thin-wall rescue is disabled is decisive. The
references deliberately merge and regularize ordinary footprints into compact,
printable masses before slicing. They then split those masses into quiet and
standard relief tiers, reserving additional vertical emphasis for a sparse
identity/core set. The visual fullness comes from many calm, printable bodies;
it does not come from keeping raw needles or making every road dark.

The initial implementation used **1.30--2.30 model mm** as a 25 km / 196 mm
urban-mass median-width prior. It is not a universal truth and must not become
a hard clamp. A direct compact-component audit of ten reference 3MFs found
city medians outside a single narrow band; scene density, component role and
the selected crop all move the useful distribution. The printer profile still
sets a hard survival floor, while the reference-width distribution is a soft
aggregation signal that may float and must be reported rather than silently
forced. Geographic granularity changes through scale: a larger crop makes
every printable millimetre represent more real metres, so the real merge
radius and minimum road seam grow, more nearby buildings aggregate, and
sub-print road intervals stop splitting independent blocks. A different
physical model span scales the visible prior by ``model_span_mm / 196 mm``.

Shape is a separate and more stable requirement. The same ten-reference audit
found city-level median solidity between **0.9904 and 1.0000**, median perimeter
excess essentially **1.0**, and a typical p75 of roughly **6--11 effective
outline turns**. The reference pieces therefore read as compact, calm
silhouettes rather than literal unions with parcel-sized bites. Anonymous
urban mass should aim for softened convex or near-convex polygons, low
perimeter excess and few meaningful turns. Strict convexification is not a
hard rule: a road, water, crop or protected-identity boundary may impose a
real concavity, and geometry must never cross that boundary merely to improve
an aesthetic score.

#### Shared grammar versus adaptive controls

The shared grammar is small:

- black/recessed water and a few structural cuts;
- neutral terrain/block substrate;
- white low urban texture;
- sparse height emphasis;
- printable, softened building masses instead of raw needle footprints.

The adaptive controls are scene-derived:

- whether Z belongs primarily to buildings or terrain;
- whether height emphasis is absent, one compact core, or several clusters;
- whether roads read as grid seams, radial structure, quiet irregular fabric,
  or only secondary context;
- whether water is a coast, a single river axis, a network of lakes/canals, an
  island field, or a confluence;
- how much low urban texture is needed to prevent an empty composition.

#### Scene-character vector required by code

Runtime selection should be deterministic and auditable. It should calculate
a continuous feature vector, not look up a city name and not ask an LLM to
author geometry.

**Water topology**

- total and largest-component water fraction;
- frame-edge contact pattern to distinguish coast from internal river/lake;
- component and island counts;
- shoreline density and complexity;
- river centreline sinuosity, width stability and opposite-bank evidence;
- network branching and confluence degree.

**Terrain**

- elevation `p95 - p05` normalized by crop span;
- slope and rugged-area fractions;
- buildable low-slope land fraction;
- correlation between building coverage and low-slope/shoreline bands.

**Road structure**

- orientation concentration and entropy;
- orthogonal grid score;
- ring closure candidates;
- radial convergence score;
- major-corridor density and spatial concentration.

**Buildings and height**

- footprint coverage, count density and spatial concentration;
- printable-width, compactness and aspect distributions at the selected map
  scale;
- trusted-height coverage and `p95 / p50` height contrast;
- height-mass concentration in the strongest connected cluster;
- number and separation of significant height clusters;
- named/semantic landmark evidence independent of raw height.

**Data confidence**

- local holes in otherwise urban cells;
- cross-source water/coast support when available;
- height-source provenance and negative-cache coverage;
- confidence must be recorded separately from scene character so poor source
  coverage is not mistaken for a naturally sparse city.

#### Deterministic policy resolver

The resolver should combine physical floors, city-relative ranks, and soft
archetype scores:

1. Printer profile establishes absolute minimum XY and Z survival floors.
2. Scene analysis produces continuous scores such as `coast`, `river_axis`,
   `water_network`, `island`, `confluence`, `grid`, `ring_axis`, `terrain`,
   `compact_core`, and `polycentric`.
3. The strongest compatible scores select or blend bounded profiles. For
   example, high water alone cannot make Hong Kong and Chicago equivalent:
   terrain relief and buildable-land fraction separate them.
4. Building roles are then resolved as `hero_identity`, `urban_mass`, or
   `quiet_texture`. Relative ranking may choose candidates, but physical width
   and provenance still gate whether a body is printable or trustworthy.
5. The resolved profile and every input metric are saved in DesignSpec. The
   renderer and mesh builders consume only bounded, versioned policy values.

The runtime path contains no subjective human switch and no generative-model
geometry control. Human/AI visual review remains an offline evaluation signal
for tuning policy versions across a fixed regression set.

#### Current implementation boundary

`scene-character-v6` now measures water/road/terrain identity plus printer-
scaled building width, aspect, compactness, area-weighted survival, cell
density, local distribution continuity, road/building contradictions, and a
transparent regularization-pressure score. `scene-policy-v6` resolves
`adaptive-local-printable-city-mass-v4`: hero identity, regularized urban
mass, and quiet density texture. High pressure lowers the budget for literal
independent bodies and raises the need for low-relief density.

The scene-policy output remains `audit_only`, but its bounded building-mass
consumer is now available to the formal preprocessing path. The implementation
does not enlarge each source footprint to force a target size. Instead it
coarsens topology first, then builds coherent anonymous mass inside those
final blocks. The evaluator in `tools/evaluate_building_mass_strategy.py`
consumes explicitly named trusted caches and renders repeatable A/B evidence.
Its contact sheet now numbers all six review stages: baseline top-down,
candidate top-down, candidate role routing, baseline height, candidate height,
and a reference-demo/effect scorecard. The sixth panel shows the 10-city 3MF
morphology cohort beside the current short-axis, solidity and outline evidence,
plus measured baseline-to-candidate coverage and fragmentation changes. It
labels the comparison `morphology_only`: no same-bbox reference image exists,
and metrics with different percentiles remain informational rather than being
promoted into aesthetic pass/fail gates. The same structured comparison is
persisted in the evaluator JSON as `reference_comparison`.

`building-mass-candidate-v6` separates geometry aggregation from relief-role
selection. Safe agglomeration still happens only between nearby compact source
groups inside one final road/water block. The complete-source route retains a
12% cap for shaping a source group and permits a separately recorded 35% cap
when joining two already compact groups. After all safe merges, a remaining
standard-relief component narrower than 60% of the scale-aware soft target or
outside the compact-silhouette policy is not widened or deleted: it is demoted
to low-relief `quiet_texture`. Evidence
records counts and area before/after this role filter, plus width, solidity,
perimeter excess and outline-turn distributions for each final semantic role.

The scale-aware topology and silhouette contract is:

1. Polygonize the complete selected road/water network into initial blocks.
2. Preserve motorway/trunk/primary roads, every road selected as visibly
   structural by the salience resolver, and every water boundary.
3. Measure the occupied usable short axis after the actual two-sided seam
   inset, in physical model millimetres.
4. Use the printer floor plus the provisional component-width prior to dissolve
   only existing low-order road cuts that create implausibly small cores. The
   prior is diagnostic and scene-adaptive, not an acceptance threshold. Each
   pass has a merge budget, so an urban grid cannot collapse into a few
   arbitrary slabs.
5. Derive both anonymous building aggregation and Block-base seams from the
   same final block boundaries. Removed low-order cuts therefore cannot remain
   as phantom Block-base grooves.
6. Build multiple source-seeded clusters inside eligible blocks rather than
   replacing the complete block. Recursively split transitive or U-shaped
   chains, then agglomerate only nearby small clusters whose final clipped
   polygon remains compact and within the bounded area-growth budget.
7. Regularize shallow anonymous concavities with bounded simplify, soften or
   convex-hull candidates. Accept a candidate only when it stays inside the
   road/water/hero-safe clip, keeps the printable core, respects the area
   budget, and improves the measured silhouette. Record genuine
   boundary-limited concavities instead of hiding them.
8. A block classified as cross-source-supported `neighborhood_mass` may use a
   lower OSM coverage gate for coherent block fill, but it must still contain
   at least six source footprints. Without that external urban evidence the
   stricter coverage gate remains. This separates incomplete building data
   from genuinely open or natural land without a city-name exception.

This is why a 25 km crop becomes geographically coarser than a 15 km crop at
the same 196 mm model span: more narrow real road intervals are sub-printable
and more anonymous footprints combine. Major/visible city identity and water
geometry remain unchanged. The evidence records before/after block counts,
occupied-core median, retained cut length, closing radius, final component
median, boundary clearance and every preserved invariant.

The consumer resolves three anonymous-building roles without a city-name
table:

- high-density blocks become standard-relief coherent urban mass;
- middle-density blocks become merged low-relief quiet texture;
- sparse blocks retain only compact independently printable bodies.

Printer pressure is measured from count- and area-weighted sub-nozzle
footprints. High pressure broadens the mass role and merge distance. Z values
are not accepted as inputs: quiet and standard relief are derived from printer
layer counts. Each body is inset from its topology block by at least half of
the printer profile's final two-extrusion road seam, and the measured boundary
clearance is recorded.

This experiment also records its abstraction cost. A filled city block contains
deterministic road-bounded area outside literal building footprints; that area
is labeled `block_abstraction`, not source geometry. A clean image therefore
cannot hide an unsupported fill ratio.

Full release acceptance still requires semantic reclassification of existing
`BL` bodies, multi-city visual A/B, a real slicer, and 3MF validation. Passing
the printer floor and reporting a plausible component-width distribution prove
only physical granularity, not beauty. Silhouette solidity, perimeter excess,
outline-turn distribution, abstraction share and boundary-limited counts are
independent review evidence.
The measured median uses an explicit 0.01 mm numerical tolerance; reports keep
the raw value and exact relation, so tolerance cannot masquerade as an exact
hit or justify progressively lowering source-support gates.

#### First fixed-cache building-mass A/B

The first candidate was evaluated on fixed crops and the same baseline layers.
It changed only the anonymous building presentation; water, road identity and
the formal exporter were unchanged.

| Scene | Baseline → candidate coverage | Raster components | Mid-density 8×8 cells | Small-component fraction | Review |
| --- | ---: | ---: | ---: | ---: | --- |
| West Lake 25 km | 27.27% → 4.40% | 1,437 → 924 | 34 → 30 | 32.71% → 3.90% | rerun: too sparse |
| Chicago 15 km | 41.17% → 18.41% | 7,918 → 3,306 | 15 → 55 | 1.18% → 0.82% | promising |
| Beijing 25 km | 31.69% → 16.90% | 3,539 → 2,142 | 37 → 63 | 3.00% → 3.64% | promising; tiny ink increase is negligible |
| Shanghai 25 km | 29.10% → 11.70% | 3,311 → 1,866 | 38 → 58 | 4.23% → 5.04% | promising; inspect sparse outskirts |

The result supports the reference design language: coherent mid-frequency
mass can reduce object noise while retaining urban fullness. It also rejects a
single global threshold. West Lake 25 km is a water-dominant mixed urban scene,
not a pure landscape scene: it still needs Block base and quiet urban mass
behind the West Lake/Qiantang River composition. The 4.40% candidate is a
resolver regression that removed too much city fabric, not evidence for
switching West Lake to the natural-landscape pipeline. The three denser cities
can use stronger mass regularization.

The current A/B deliberately preserves every existing `BL` body. Old caches
classify many merely large/printable buildings as `BL`, not only trustworthy
identity landmarks. The next consumer slice must split `BL` into a small
provenance-backed hero set and anonymous large bodies that are demoted into the
same neighbourhood mass grammar. Until then, the candidate height image still
overstates individual-building detail.

Post-shape seam measurement initially failed even though the configured inset
matched the printer profile: polygon simplification moved the final boundary
toward the road. The resolver now reserves one simplification tolerance before
shaping. Final measured two-sided clearances are 2.10009 nozzle widths (West
Lake), 2.16356 (Chicago), 2.10135 (Beijing), and 2.10237 (Shanghai), all above
the 2.10 hard floor. This is geometry evidence only; slicer survival remains a
separate activation gate.

The inferred block-fill share remains high (77.8–94.6% of candidate anonymous
mass). That is a deliberate, measured abstraction rather than literal building
coverage, and it is the main unresolved risk before promotion. A follow-up
consumer must bound that abstraction and compare it against the reference
style and real slicer output.

#### Fixed-cache scale-aware topology v2 A/B

The current implementation was then tested on two contrasting fixed caches
with the same 196 mm model span and no city-name parameter branch. West Lake
tests sparse/incomplete domestic buildings, water-dominant composition and an
irregular road field. Chicago tests exceptionally dense buildings, an
orthogonal grid and a coast. The stored West Lake bbox measures 27.59 km on
its longest projected axis despite its historical `25km` tag; Chicago measures
25.23 km. The resolver used those measured spans rather than the filenames.

| Metric | West Lake | Chicago |
| --- | ---: | ---: |
| Initial → final topology blocks | 33,310 → 25,913 | 60,021 → 21,231 |
| Retained road-cut length | 71.29% | 44.23% |
| Occupied usable-block p50 | 1.473 mm | 1.555 mm |
| Final urban-component p50 | 1.326 mm | 1.414 mm |
| Raster components | 1,437 → 1,052 | 14,515 → 4,977 |
| Land building coverage | 27.27% → 28.61% | 37.82% → 36.07% |
| Density-cell coefficient of variation | 0.812 → 0.786 | 0.299 → 0.162 |
| Candidate/baseline carrier area | 1.0489× | 1.0276× |
| Measured two-sided seam | 2.142 nozzles | 2.142 nozzles |
| Local warm-cache diagnostic time | 344.7 s | 840.2 s |

Both cases meet the 1.30--2.30 mm component distribution and the 2.10-nozzle
seam floor. The major/visible-road, water-boundary, source-geometry-only and
no-Z/no-Boolean invariants remained true. Chicago needed ten bounded merge
passes; a six-pass ceiling stopped while thousands of valid low-order merges
were still available and correctly triggered the baseline-area fallback.

This evidence is not formal print acceptance. The inferred candidate-area
share remains about 77.8% in both samples, so the abstraction-budget risk is
still open. The timings describe only this local Mac and these warm caches;
they are not a 16 GB Intel Mac, Windows worker, cloud-node or production SLA
conclusion. PNG review also cannot replace real 3MF, slicer and validator
evidence.

#### Source-seeded silhouette v4 cross-check

The follow-up v4 candidate removes complete-block replacement and measures
shape independently of width. A direct ten-reference 3MF audit supplied the
silhouette targets above. Fixed-cache West Lake and Chicago diagnostics then
used the same source-seeded split/agglomerate/regularize algorithm with no
city-name parameter branch.

| Metric | West Lake 25 km | Chicago 25 km |
| --- | ---: | ---: |
| Anonymous candidate components | 1,376 | 3,904 |
| Minimum-axis p50 | 0.811 mm | 0.674 mm |
| Solidity p50 after shaping | 0.9853 | 0.9913 |
| Perimeter-excess p50 | 1.0016 | 1.0013 |
| Effective outline turns p50 / p90 | 7 / 12 | 7 / 11 |
| Target-shape fraction after shaping | 68.24% | 82.35% |
| Inferred candidate-area share | 67.75% | 73.03% |
| Measured two-sided seam | 2.142 nozzles | 2.294 nozzles |
| Final composition | 1.0140×, composed | guarded baseline fallback |

This result separates two questions that the earlier implementation mixed.
The same silhouette grammar transfers well: both medians are reference-like,
the roads/water clearances pass, and the blocks are not replaced by arbitrary
slabs. The width prior is not met and must not be treated as a hard failure by
itself. Chicago nevertheless fails the stronger carrier test: the candidate
preserves too little of its accepted low-frequency urban mass, so the formal
composition correctly falls back to baseline. Solving that requires a
layered low-frequency carrier plus compact mid-frequency masses; relaxing the
convexity target or inflating every cluster would solve the wrong problem.

The versioned evaluator cache is also now explicit. On this Mac, West Lake's
full cache-hit diagnostic takes about 37 seconds. Chicago's first run rebuilt
21,231 topology blocks in 564.6 seconds and completed in 884.2 seconds; later
runs may reuse the derived cache. These numbers are local development evidence,
not a claim about another Mac, Windows, cloud workers or production latency.

### Print/physical profile

Add a first-class `restrained_three_value` visual profile that maps existing
semantic layers into black, neutral, and white roles without deleting their
source provenance. Keep semantic objects separate in 3MF when needed for
editing/validation, even when several share one material.

Candidate role mapping for an A/B test:

| Semantic evidence | Perceptual role | Constraint |
| --- | --- | --- |
| major sea/lake/river | black negative space | continuous surface; no minor-noise promotion |
| terrain + block base | neutral substrate | quiet variation; no patchwork edge fill |
| background buildings | white low relief | compressed height, printable footprint |
| exact/anchor landmarks | white focal relief | sparse, named, bounded exaggeration |
| primary structural roads | narrow dark/recessed cut | complete identity; minimum printable gap |
| secondary/local roads | low white/neutral texture | never equal in weight to primary |
| vegetation | neutral relief variant | do not introduce a fourth loud colour by default |

### City identity contract

DesignSpec should store a reviewed composition role in addition to raw counts:

- scene class: `urban`, `water_landscape`, `mixed`, or `landscape`;
- dominant divider/shape: river, coast, confluence, ring, radial axis, island,
  ridge, or none;
- visual focus and quiet field;
- landmark/core height policy;
- target material role profile;
- crop rationale and 15/25/30 km size.

These values select bounded algorithms and parameters. They do not allow an LLM
to author low-level geometry.

## Proposed A/B sequence

Do not replace the production profile in one jump.

1. **Material-only A/B** — render the same accepted geometry with the current
   diagnostic palette and the three-value palette. This isolates colour/value.
2. **Height hierarchy A/B** — keep footprints fixed; compare current height
   roles with compact-core + sparse-landmark + compressed-background roles.
3. **Road-role A/B** — compare raised dark ribbons against narrow structural
   cuts plus quiet secondary texture, using the same complete identities and
   printer profile.
4. **Cross-scene verification** — Chicago (grid/coast), Shanghai (broad river),
   Beijing (ring/axis), Suzhou (water network), and Hong Kong (terrain/harbour).

For every step record crop, source revision, DesignSpec, material count, height
distribution, road/water feature counts, minimum printable line/gap evidence,
validator result, and the exact render profile. Human review decides between
variants that pass the structural gates.

## Immediate conclusions

- The reference advantage is not mainly a nicer colour constant. It is the
  combination of a three-value material system, a dominant city silhouette,
  quiet mid-frequency density, selective height, and physical lighting.
- Our detailed data and cross-validation pipeline remain valuable. The design
  task is to subordinate that detail to perceptual roles rather than display
  every data class at equal strength.
- The safest next experiment is material-only Chicago/Shanghai/Beijing A/B.
  It is cheap, reversible, and will show how much of the gap is palette versus
  geometry before height or road geometry is changed.

## Standalone block-grammar observer

`tools/observe_reference_block_grammar.py` is a read-only diagnostic and is
deliberately outside the generation pipeline. It reads compact white-relief
components from reference 3MF packages and preserves the raw measurements in
per-city JSON before assigning a provisional morphology label. It does not
copy footprint geometry, change a model, or feed parameters back into mesh
generation.

The observer records six evidence families:

1. model-grid occupancy, projected white-relief coverage and spatial
   continuity;
2. component count and rotation-invariant short-axis distribution;
3. single-axis and orthogonal orientation coherence plus orientation entropy;
4. solidity, minimum-rectangle fill, perimeter excess, concavity and effective
   outline turns;
5. a clearly labelled nearest-neighbour gap proxy, which is not a slicer
   clearance measurement;
6. measured quiet/standard white-relief height tiers.

The first eight-city run used four learning examples (Beijing, Hangzhou,
Chicago and Suzhou) and four previously held-out examples (Nanjing, Chengdu,
Milan and Shenzhen). It completed locally in about 23 seconds with 240 sampled
silhouettes per white tier. The held-out results were not used to tune the
thresholds.

The observer separated the two visibly strongest orthogonal fields without a
city-name rule: Chicago reported orthogonal coherence `0.739` and Beijing
`0.606`; the six mixed-direction cities ranged from `0.011` to `0.374`.
Chicago also had the highest median minimum-rectangle fill (`0.918`), matching
its visibly calm rectilinear block language. All eight references remained
spatially continuous (`0.859`--`1.000` occupied-cell fraction) even though
their component populations varied from 2,412 to 9,928. This is direct
evidence that reference fullness is distribution plus quiet coverage, not a
single component-count target.

The rotation-invariant all-tier short-axis medians ranged from `0.966` to
`1.687` mm. That is lower and wider than the provisional `1.30`--`2.30` mm
prior in several cities. The result is important but not yet a policy change:
quiet and standard tiers are mixed in the all-tier statistic, and the reference
selection is a material-role proxy rather than guaranteed building semantics.
It does show that `1.30 mm` must not become a universal hard minimum or the
Chicago/Beijing grain will be over-coarsened.

Artifacts from the first run live under
`output/reference_block_grammar_observer_v1/`: `summary.json`, `summary.csv`,
`summary.md`, per-city observation JSON and per-city diagnostic PNG. These are
diagnostic evidence only; visual acceptance still requires human inspection,
and print acceptance still requires the normal DesignSpec/3MF/validator gates.

### Cross-check against project source data

Reference 3MF metrics alone cannot show whether the source data was already
good or whether the result was artistically processed. The project therefore
has a three-way read-only comparison:

```bash
.venv/bin/python tools/observe_project_block_grammar.py \
  --cases data/project_reference_block_grammar_cases.json \
  --output-dir output/project_reference_block_grammar_comparison_v1
```

It separates raw OSM buildings, existing current-pipeline candidate evidence,
and the reference demo. Raw source data is reported both as the full footprint
population and as a deterministic sample filtered to the reference observer's
compact-component size bounds. This avoids treating sub-nozzle source
footprints as if they were directly comparable to designed 3MF components.

The first Hangzhou/Beijing/Chicago/Suzhou run on 2026-08-30 supports these
provisional conclusions:

- reference median short axes are 3.18--4.49 times the source-comparable
  footprint medians;
- Beijing and Chicago reference components are more rectangular and have less
  excess perimeter, which is consistent with outline regularisation;
- Chicago reference components are about 37.7% of the extrapolated compatible
  source population, which is consistent with strong selection/aggregation;
- Suzhou has 2.49 times as many reference components as the source sample
  predicts above the same size bounds. The likely operation is not uniformly
  deleting buildings, but promoting sub-nozzle texture into printable blocks;
- direct faithful scaling of OSM footprints does not explain the reference
  demo's fullness and granularity in any of the four cities.

These are geometry-backed hypotheses, not proof of the reference author's
algorithm. Existing candidate JSON stores role-level percentiles but not final
candidate polygons, so candidate rectangularity and orientation remain marked
as unavailable instead of being inferred. See
`output/project_reference_block_grammar_comparison_v1/summary.md` and the
Chinese diagnostic PNGs for the complete evidence.

### Production consumer: measured evidence, not copied reference geometry

The reusable measurement definitions now live in
`aesthetic/block_grammar.py` and are called by `SceneCharacter`. The generation
pipeline does **not** read reference 3MF files or per-city reference results at
runtime. It only carries a versioned broad aggregate envelope as a soft visual
prior. The current-city source measurement is converted by `ScenePolicy` into
five bounded signals: aggregation, anonymous-outline regularisation,
orientation preservation, supported quiet-texture promotion, and selection.

Those signals are consumed by the existing BuildingMass policy resolver. They
may tune source-seeded merge/closing radii, compact-silhouette targets, the
soft component-width band and the urban-mass selection quantile; ScenePolicy
also records the resulting scene-level independent-body budget. They may not weaken
printer minima, cross road/water topology blocks, relax area-growth guards,
replace landmark geometry, control mesh vertices/global Z/boolean operations,
or invent an urban field where neither source footprints nor cross-source
urban evidence exists. The complete Stage contract and mapping table are in
`doc/current_generation_pipeline.md`.
