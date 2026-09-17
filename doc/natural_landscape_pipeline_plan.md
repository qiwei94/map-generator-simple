# Natural landscape pipeline plan

Status: full-lake canonical validation achieved; satellite/DEM material accents remain experimental

## 2026-09-17 阶段更新

完整青海湖已经完成 canonical S1–S10：约 138.36 × 123.77 km 的取景映射为最长边 196 mm，严格验证零错误零警告。它证明流水线可以处理远大于 25 km、建筑极少的完整内陆湖；结果尚未经过 Bambu Studio 切片或实体打印。细节见 `doc/qinghai_full_lake_20260914.md`。

泸沽湖、赛里木湖、太湖采用“每件最长边 196 mm、各自地理比例”的系列策略，不强迫不同尺度的湖泊使用统一比例。真实范围和换算规则见 `doc/natural_lake_series_scale_policy_20260916.md`。

自然山地的当前候选手法使用卫星影像确定雪、冰川、裸岩和植被的平面证据，使用 DEM 提供起伏、坡度和山脊，再生成有厚度变化的白色顶层。白色层目标厚度约 0.16–1.2 mm，用于表现雪线与积雪厚薄；不得用影像亮度直接生成地形高度。长白山、梅里雪山、博格达峰、玉龙雪山和祁连山／卓尔山均已有研究样片，但尚未完成正式切片和实物验收。

当前优先级暂时回到巴黎城市模型；自然产品线保留现有产物和结论，后续从切片验证继续。

## 2026-09-14 implementation update

Active landscape policies now bypass S5 block-first planning and B+C ground
microtexture. S6 removes synthetic block_base and aggregated BO mass from its
owned layer clone before freezing surfaces; source landmarks, roads, vegetation,
water, and the existing DEM terrain plan remain available. Urban and audit-only
policies retain their previous behavior. The structural terrain base is unchanged.

This is a bounded first activation, not completion of the geometry contract below.
Full-lake framing and sparse/empty urban-data behavior have now been exercised by
the complete Qinghai Lake run. DEM sampling retains its 1024 grid-resolution
ceiling for large frames. Geometry validation passed for that artifact, while
slicer and physical-print acceptance remain open.

Updated: 2026-08-27

## Phase 1 implementation status (2026-08-27)

`scene-character-v5` now embeds deterministic `landform-character-v1`
evidence derived from the exact DEM crop.  It measures isolated prominence,
iconic peak, ridge continuity, closed crater/basin evidence, open linear
valleys, repeated cones, and open plain evidence.  `scene-policy-v5` resolves
the initial landscape archetypes and records a separate landscape trade-off
order.  The formal generation pipeline writes these decisions to the existing
SceneCharacter, ScenePolicy, CompositionSpec, and DesignSpec audit chain.

This phase is intentionally `audit_only`: urban geometry is unchanged, and a
landscape decision cannot yet alter terrain vertices, Z mapping, water
booleans, materials, or Block-base generation.  Synthetic DEM tests cover an
isolated peak, elongated ridge, closed crater, open canyon, repeated cones,
and a nearly flat plain.  Real DEM cross-scene validation remains required
before any consumer is activated.

## Product decision

Travel memories divide naturally between built cities and natural landscapes.
The project therefore needs two first-class product lines:

- **Urban models**: city identity is carried by roads, water, buildings,
  landmarks, and selectively generated block-base structure.
- **Landscape reliefs**: place identity is carried by terrain prominence,
  ridges, valleys, crater rims, shorelines, water planes, snow/rock boundaries,
  and a deliberately chosen frame.

A landscape is not an urban scene with poor building coverage. Sparse roads
and buildings are expected evidence and must not trigger synthetic urban fill.

## Geometry contract

- DEM is the primary geometry source. Prefer cached Copernicus GLO-30; use
  cached SRTM as the compatible fallback.
- Urban `block_base` is disabled. The structural bottom plate remains mandatory
  and must form a closed printable solid with the terrain.
- Buildings are disabled by default. A small number of verified visitor
  buildings or monuments may be included only when they help scene identity.
- Ordinary roads are disabled by default. Scenic roads and trails may be kept
  as quiet context when they provide scale or narrative.
- Water is an independently materialized flat surface. Terrain must be carved
  or clipped to the same shoreline geometry so the two parts mate exactly.
- Relief mapping is deterministic and derived from measured elevation range,
  local prominence, slope, and print constraints. LLM output must not set
  vertices, global Z, or boolean parameters.
- Global smoothing must not erase protected peak silhouettes, ridge lines,
  crater rims, or sharp shoreline transitions.

## Initial scene modes

| Mode | Example | Primary identity |
|---|---|---|
| `isolated_monolith` | Uluru | isolated prominence above a local base plane |
| `volcanic_lake` | Changbai Tianchi | continuous crater rim plus planar lake |
| `lake_shore` | Qinghai Lake, Sayram Lake | shoreline curvature plus adjacent relief |
| `iconic_peak` | Matterhorn | recognizable summit, shoulders, and valleys |
| `mountain_range` | Alps, Tianshan | peak hierarchy, ridges, valleys, and lakes |
| `volcanic_field` | Inner Mongolia volcano groups | repeated cones and spatial rhythm |
| `desert_relief` | selected Xinjiang/Inner Mongolia frames | dunes, escarpments, or basin edges only when DEM supports them |

## Framing and scale

- Support 15, 25, and 30 km square products before considering larger formats.
- Keep the printable XY span at 196 mm; recompute real-world print thresholds
  from the resulting scale.
- A 30 km frame maps 0.4 mm to about 61 m and the default 0.63 mm colored strip
  to about 96 m. Fine roads, buildings, streams, and terrain noise must not be
  carried forward merely because they exist in source data.
- Large lakes must use a characteristic shoreline segment rather than placing
  mostly open water in the frame. Mountain ranges require a main peak/valley
  hierarchy rather than an arbitrary square of uniformly busy relief.

## 3MF part and material policy

Landscape 3MF files should normally contain fewer semantic parts than urban
models:

1. structural base and main terrain;
2. characteristic rock, summit, snow, or other verified terrain accent;
3. water;
4. optional large vegetation region;
5. optional scenic route or verified human landmark.

The first three-material studies should use restrained scene-specific palettes:

- lake/mountain: warm stone, ivory summit/snow, ink-blue water;
- arid monolith: pale sand, muted terracotta rock, dark brown/graphite base;
- alpine peak: stone grey, warm off-white snow, deep blue water.

Display colors in the 3MF must match actual extruder roles. Bright map-blue,
map-green, and literal satellite colors are not the default product aesthetic.

## Data preservation

Persist on the Windows data vault, with source and license metadata:

- raw DEM tiles and checksums;
- water/coastline vectors and supplementary masks;
- derived prominence, ridge, crater, shoreline, and crop evidence;
- resolved landscape DesignSpec;
- PNG/GLB/3MF artifacts and slicer/validator reports.

Derived caches must be invalidated when their source fingerprint or geometry
policy changes. They do not replace the raw-source archive.

## Acceptance matrix

Implement and validate scenes in this order:

1. Matterhorn 15 km: iconic peak and ridge preservation.
2. Uluru 25 km: isolated-prominence baseline and arid palette.
3. Changbai Tianchi 15/25 km: crater continuity and water/terrain mating.
4. Qinghai Lake 25 km: characteristic shoreline composition.
5. Alps 30 km: peak-valley-lake hierarchy at the new scale.
6. Selected Xinjiang and Inner Mongolia frames: arid, volcanic, and sparse
   terrain behavior.

Each accepted result requires:

- non-empty, source-backed landform and water evidence where expected;
- closed/manifold finite geometry within the 196 mm footprint;
- adequate structural bottom and wall thickness;
- planar water with no shoreline gaps or floating surfaces;
- slicer evidence for slopes, isolated layers, thin islands, and material
  boundaries;
- 0 validator errors and 0 warnings;
- a DesignSpec containing crop, DEM source/resolution, relief mapping,
  materials, object counts, and artifact checksums;
- human composition review confirming that the named landform is recognizable.

## Explicit non-goals

- Do not fill naturally empty regions with urban block base.
- Do not infer terrain height directly from satellite-image brightness.
- Do not fabricate fine sand dunes when the DEM cannot resolve them.
- Do not let an LLM directly manipulate terrain mesh, global Z, or booleans.
- Do not accept a generic attractive heightfield when the named landform is not
  recognizable.
