# Pipeline 全流程、外部依赖与单步 Debug 指南

> 适用于：`_TEXTURE_STYLE_OF_DEEPSEEK/pipeline.py::run()` 与等价的 `generate_*.py` 入口
> 时间：2026-05-15（refactor 后）

---

## 一、总览

```
                ┌──────────────┐
                │ 用户：lat/lon │
                │ 矩形 bbox    │
                └──────┬───────┘
                       ▼
 ┌──── Stage 0 ────┐  pyproj          坐标系
 ├──── Stage 1 ────┤  SRTM HGT 瓦片   高程网格
 ├──── Stage 2 ────┤  osmium-tool     建筑/道路/水体/植被 GeoJSON
 ├──── Stage 3 ────┤  geopandas       投影到本地 UTM
 ├──── Stage 4 ────┤  numpy+manifold3d 对象4：地形+桥梁+水体镂空
 ├──── Stage 5 ────┤  trimesh         对象5：建筑
 ├──── Stage 6 ────┤  trimesh         （独立）道路 ribbon
 ├──── Stage 7 ────┤  manifold3d      对象3：底板+水体浮雕
 ├──── Stage 8 ────┤  manifold3d      对象2：植被+遮挡镂空
 ├──── Stage 9 ────┤  zipfile+ET      装配 + 写 3MF
 └──── Stage 10 ───┘  re              校验报告
                       ▼
                ┌────────────┐
                │ output/*.3mf│
                └────────────┘
```

10 个 Stage 全部在一个进程内串行；中间产物只在内存里流转，没有跨进程序列化。

---

## 二、各 Stage 详细分解

### Stage 0 — 坐标系与比例尺

**做什么**：把 WGS84 矩形换算到 UTM 局部坐标，决定模型 mm-per-meter 比例。

**关键调用**：`_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/processors/coords.py::bbox_to_utm`

**外部库**
| 库 | 用途 |
|----|------|
| `pyproj` (>=3.6) | UTM 投影变换器（自动按经度选 zone） |

**输出**：`bbox` dict — `utm_crs` / `utm_bbox` / `origin` / `width_m` / `height_m` / `area_km2`、`scale = 196 / max(width_m, height_m)`。

**Debug**
- 可疑：跨 UTM zone 的大区域 → 单 zone 投影会有变形。日志里看 `UTM zone:`。
- `python -c "from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.processors.coords import bbox_to_utm; print(bbox_to_utm(30.13,120.01,30.36,120.29))"`
- 想换比例尺：改 `config.py::INTERNAL_SPAN_MM`（默认 196.0）。

---

### Stage 1 — 高程数据

**做什么**：拉 SRTM HGT 瓦片，重采样成 `resolution × resolution` 网格（米）。

**关键调用**：`_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/elevation.py::fetch_elevation_grid`

**外部库 / CLI / 服务**
| 名称 | 作用 |
|------|------|
| `requests` | HTTP 下载 |
| AWS S3 镜像 `elevation-tiles-prod` | 主数据源（SRTM 1°×1° HGT） |
| `https://api.open-elevation.com` | 兜底（部分海岛/极区缺瓦片时） |
| `numpy` | HGT 二进制 → 网格 |
| `scipy.ndimage` | 高斯平滑（减少 SRTM 噪声） |

**缓存**：本地 `~/.cache` 或 `cache/srtm/`，单瓦片 ~7MB（1 弧秒）/ ~2MB（3 弧秒）。

**Debug**
- 网络断：日志 `Downloading SRTM tile: ...` 后 timeout → 走 Open Elevation 兜底。
- 海平面误差：HGT 在水面上是雷达噪声 → `terrain3d/processors/terrain.py::carve_terrain_for_water` 会按水体多边形把这部分压低。
- 直接看高程：
  ```python
  from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.elevation import fetch_elevation_grid
  g = fetch_elevation_grid(30.13, 120.01, 30.36, 120.29, resolution=256)
  print(g.shape, g.min(), g.max())
  import matplotlib.pyplot as plt; plt.imshow(g, cmap='terrain'); plt.colorbar(); plt.savefig('/tmp/elev.png')
  ```
- 换数据源：在 `elevation.py:_SRTM_URLS` 加镜像；想用本地 GeoTIFF 走 `fetch_elevation_grid_from_file`（需 `rasterio`）。

---

### Stage 2 — OSM 数据

**做什么**：从本地 `.osm.pbf` 文件抽取建筑 / 道路 / 水体 / 植被四类要素，输出 `geopandas.GeoDataFrame`。

**关键调用**：`_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/osm.py::fetch_buildings/fetch_roads/fetch_water/fetch_vegetation` → `osmium_cli_fetcher.py` 真正执行 CLI。

**外部库 / CLI**
| 名称 | 作用 | 安装 |
|------|------|------|
| `osmium-tool` (CLI) | 主力：`extract` / `tags-filter` / `export` 三段管线，比 pyosmium 快 10-20× | `conda install -c conda-forge osmium-tool` 或 `brew install osmium-tool` |
| `ogr2ogr`（GDAL）| 水体 relation 的精确 bbox 裁剪 | `brew install gdal` |
| `geopandas` | 读取 osmium 输出的 GeoJSON | requirements.txt |
| `osmnx`（备用） | 走 Overpass API 在线兜底 | requirements.txt |

**输入数据**：`pbf_cache/*.osm.pbf` —— 用 `tools/manage_pbf.py download zhejiang` 一次性下完整省，后续都是离线。

**Debug**
- 没有 osmium：`generate_westlake_cli.py` 会直接 `sys.exit(1)`，提示安装。
- 中间产物：默认在 `tmp/osmium_cli_<tag>_<bbox>/`，每步都留 `.osm.pbf` / `.geojson`，QGIS 可直接打开。
- 设 `set_pbf_file_path()` 之后再调 `fetch_*`；否则会报"未指定 pbf"。
- 单独跑某一类：
  ```python
  from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water
  set_pbf_file_path("pbf_cache/zhejiang-latest.osm.pbf")
  gdf = fetch_water(30.13, 120.01, 30.36, 120.29, export_gpkg="/tmp/water.gpkg")
  ```
  生成 GeoPackage 用 QGIS 打开就能直观看见过滤前/后的水体轮廓。
- 建筑没高度：检查 gdf 是否有 `est_height` 列；CLI 路径在 `osmium_cli_fetcher.py::fetch_features` 末尾自动补；Overpass 路径在 `osm.py:576` 补。

---

### Stage 3 — 投影

**做什么**：把 WGS84 GeoDataFrame 投影到本地 UTM 米，再相对 `origin`（bbox 中心）平移到原点。最后用 `clip_bbox` 切掉超出区域的部分。

**关键调用**：`coords.py::project_geodataframe`

**外部库**
| 库 | 用途 |
|----|------|
| `geopandas` / `shapely` | `to_crs` 投影、`intersection` 裁剪、`buffer(0)` 修复无效几何 |

**Debug**
- 自相交几何：日志会安静走 `geom.buffer(0)` 修复路径；要看哪些几何无效：
  ```python
  bad = gdf[~gdf.is_valid]
  print(bad)
  ```
- 投影后坐标偏离原点：检查 `bbox['origin']` 是否合理（应在 UTM 数百千米数量级）。

---

### Stage 4 — 对象 4（地形+道路+水体镂空）

**做什么**：本项目最重的一步。三步布尔运算：

```
Step 1  build_deepseek_terrain     高程网格 → 加墙 + 底盖 → trimesh watertight
Step 2  filter_bridges_only        筛桥梁段 → ribbon → 布尔 ⨃ 进地形
Step 3  collect_water_polygons +   水体挤出柱 → 布尔 ⨃ → 一次 ⊖ 镂空地形
        batch_boolean(Add) +
        manifold3d.subtract
```

**关键调用**：`object4_terrain_with_holes.py::build_terrain_with_water_holes_manifold`

**外部库**
| 库 | 用途 |
|----|------|
| `numpy` | 高程网格、顶点/面数组 |
| `trimesh` | 网格容器、`merge_vertices`、`fix_normals` |
| `manifold3d` (>=3.4) | **真正的布尔运算引擎**，guaranteed-watertight |
| `mapbox-earcut` | 地形底盖三角化（避免扇形条纹） |

**Debug**
- 慢：默认 25km、resolution=1024 时 Step 3 ~30s。`object4_*.py` 每步都有 `⏱` 计时。
- watertight=False：先看 Step 1 日志（地形本身就漏？），再看 Step 3（水体柱 Z 没穿透地形？）。Z 穿透条件：`z_bottom < terrain.z_min < terrain.z_max < z_top`。
- 桥梁不见：`bridge_filter` 日志里"实际跨越水体的标记桥梁"为 0 → 检查 OSM 有没有 `bridge=yes` 标签或道路是否真的穿过水体多边形。
- 单独跑 obj4：`python generate_hangzhou_obj4.py`。
- 直接看中间网格：
  ```python
  res = build_terrain_with_water_holes_manifold(...)
  res['mesh'].export('/tmp/obj4.stl')
  ```
  再用 `meshlab /tmp/obj4.stl` 或 Bambu Studio 查。

---

### Stage 5 — 建筑

**做什么**：每个 footprint 独立挤出成简化方块，按 `terrain_z - Z_BUILDING_EMBED_MM` 嵌入地形。

**关键调用**：`buildings.py::build_deepseek_buildings`

**外部库**
| 库 | 用途 |
|----|------|
| `shapely` | `simplify`（Douglas-Peucker） |
| `numpy`+`trimesh` | 顶点 / 面拼接 |
| `scipy.spatial.cKDTree`（间接，通过 `sample_terrain_z`） | 在地形上采样建筑底面 Z |

**Debug**
- 全部建筑面积太小被过滤：日志 `No buildings generated`。降低 `config.py::BUILDING_MIN_AREA[area_class]`。
- 高度全是默认 10m：建筑 gdf 缺 `est_height` 列。检查 Stage 2。
- 看一栋楼：
  ```python
  m = build_deepseek_buildings(buildings_gdf.head(1), terrain_solid, area_km2, scale)
  m.export('/tmp/one_building.stl')
  ```

---

### Stage 6 — 道路（独立）

**做什么**：手工搭建道路 ribbon mesh（不与地形布尔合并；这是 obj_4 之外的"备选"独立道路对象）。

**关键调用**：`roads.py::build_deepseek_roads`

**外部库**
| 库 | 用途 |
|----|------|
| `shapely` | LineString 操作 |
| `numpy`+`trimesh` | ribbon 顶点/面 |
| `scipy.spatial.cKDTree` | 沿线采样地形 Z |

**Debug**
- 道路太细看不见：`config.py::ROAD_WIDTH_MULTIPLIER` 默认 2.5，可调。
- 大区域道路太多导致内存/速度问题：脚本里加 `roads_gdf = roads_gdf.sample(5000)` 限流。

---

### Stage 7 — 对象 3（底板+水体浮雕）

**做什么**：满 bbox 平板 ⨃ 水体多边形挤出柱（用 `_geom_utils.collect_water_polygons` 取得多边形集合，**和 Stage 4 用同一个**，保证 XY 镶嵌一致）。

**关键调用**：`water.py::build_deepseek_water`

**外部库**
| 库 | 用途 |
|----|------|
| `manifold3d` | `CrossSection.extrude` + `batch_boolean(Add)` |

**Debug**
- 浮雕和地形镂空对不上：之前 B1 bug。修复后两者共用 `collect_water_polygons`，理论上完美对齐。如还有偏差，检查 `WATER_MIN_AREA_M2`（必须两边一致）。
- 水体柱 Z 太矮：调 `config.py::WATER_HEIGHT_MODEL_MM`。

---

### Stage 8 — 对象 2（植被+遮挡）

**做什么**：植被多边形挤出 ⨃ → 减去（水体 ⨃ 建筑 ⨃ 道路*），得到"植被里挖空了水体/建筑"的 watertight 网格。`*` 道路默认关。

**关键调用**：`vegetation_exclusion.py::build_deepseek_vegetation_with_exclusions`

**外部库**：与 Stage 7 相同（`manifold3d`）。

**Debug**
- 植被压住了建筑顶：检查日志 `建筑排除柱体积`，应 >0。空说明 `buildings_gdf` 没传进来。
- 慢：建筑很多时 `create_building_exclusion_columns_manifold` 是 N 次 extrude + 1 次 batch_boolean。考虑只 union 大型建筑。

---

### Stage 9 — 装配 + 3MF 导出

**做什么**：
1. `split_terrain_mesh` 把对象 4 按面法线 Z>0.1 拆成 `terrain_surface` 与 `terrain_walls`（每面分别上色）。
2. `export_deepseek_3mf` 用 `xml.etree.ElementTree` 拼 3dmodel.model XML，`zipfile` 打包成 .3mf。

**关键调用**：`exporter.py::export_deepseek_3mf` / `split_terrain_mesh`

**外部库 / 标准库**
| 名称 | 用途 |
|------|------|
| `xml.etree.ElementTree` | 3dmodel.model 序列化 |
| `zipfile` | 3MF 是带 ContentTypes 的 zip |

**Debug**
- 3MF 在 Bambu Studio 显示成全部一个颜色：检查 XML 里每个 `<object>` 是否带 `pid="1" pindex="N"`，N 应取 0..5。
- 缺对象：`<basematerials>` 永远写 6 个 `<base>`（含 vegetation），但 `<object>` 仅写 mesh.faces > 0 的对象，避免 Bambu 警告 volume=0。
- 直接看 XML：
  ```python
  import zipfile
  with zipfile.ZipFile('output/.../foo.3mf') as zf:
      print(zf.read('3D/3dmodel.model').decode()[:2000])
  ```

---

### Stage 10 — 校验

**做什么**：再读一遍刚写的 3MF，跑 V1–V12 共 12 条规则。错误项让 pipeline 报红，警告项不影响 Overall=PASSED。

**关键调用**：`validator.py::validate_3mf` + `print_validation_report`

**外部库**
| 库 | 用途 |
|----|------|
| `zipfile` + `re` | 解 3MF / 抽 `<vertex>` `<triangle>` |
| `numpy` | 法线计算（用于 V6 V9 V12） |

**12 条规则**

| ID | 含义 | 失败常见原因 |
|----|------|----|
| V1 | XY 跨度 = 196 ± 2mm | scale 计算错或后期被改 |
| V2 | terrain_surface + terrain_walls 都存在 | split_terrain_mesh 找不到水平面 |
| V3 | 地形厚度 4.0mm ± 15% | Z_GAMMA / TERRAIN_THICKNESS_MM 没起作用 |
| V4/V5 | 建筑嵌入合理 | Z_BUILDING_EMBED_MM 太大 |
| V6/V7 | 道路朝上 + Z 范围合理 | ribbon 法线翻转 |
| V8/V9 | 水体厚度 + 有侧壁 | 水体只剩底板（无水体多边形） |
| V10 | 每对象 extruder = EXTRUDER_MAP | 一般不会失败（已硬绑定） |
| V11/V12 | 植被有厚度 + 面平整 | V12 现在通常会 warn — 因为 Manifold extrude 自带侧壁，可忽略 |

**Debug**
- V2 FAIL：之前 B 阶段有 metadata 不写问题，refactor 后 validator 改读 XML name 属性。
- V8 Z span ≈ 0：水体 mesh 实际为空（GeoDataFrame 全部小于 `WATER_MIN_AREA_M2`）。

---

## 三、运行入口

| 入口 | 用途 | 备注 |
|------|------|------|
| `python -m _TEXTURE_STYLE_OF_DEEPSEEK.pipeline --lat1 ... --lat2 ...` | 完整 pipeline，OSM 走 osmnx Overpass | 默认行为 |
| `python generate_westlake_cli.py` | 完整 pipeline，OSM 走 osmium CLI（快） | 需 osmium-tool |
| `python generate_hangzhou_obj1.py` | 仅生成对象 3（底板+水体） | 调试 obj3 |
| `python generate_hangzhou_obj4.py` | 仅生成对象 4（地形+水体镂空+桥梁） | 调试 obj4 |
| `python generate_hangzhou_west_lake.py` | 4 个 obj 分别独立导出 + 装配 | 看 §四 |
| `tools/manage_pbf.py` | 下载 / 列出 / 清理 .osm.pbf | 一次性 |
| `tools/visualize_water_types.py` / `water_threshold_visualizer.py` | 调阈值 | 仅辅助 |

---

## 四、典型 Debug 工作流

### 1. 先看是不是数据问题

```bash
# OSM 数据（中间产物在 tmp/）
python -c "
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osm import set_pbf_file_path, fetch_water
set_pbf_file_path('pbf_cache/zhejiang-latest.osm.pbf')
gdf = fetch_water(30.13, 120.01, 30.36, 120.29, export_gpkg='/tmp/water.gpkg')
print(gdf.geometry.type.value_counts())
"
qgis /tmp/water.gpkg
```

### 2. 单独跑某一个对象

```bash
python generate_hangzhou_obj1.py   # 看 obj3
python generate_hangzhou_obj4.py   # 看 obj4
```

每个会写 `output/.../obj*.3mf`，用 Bambu Studio 打开。

### 3. 用合成数据验证逻辑

`venv/bin/python doc/_smoke_e2e.py`（你想保留这个测试，可以从我之前在 `/tmp/_e2e.3mf` 跑过的脚本拷一份）。

### 4. 静态检查

```bash
venv/bin/python -m py_compile _TEXTURE_STYLE_OF_DEEPSEEK/*.py
venv/bin/python -c "import _TEXTURE_STYLE_OF_DEEPSEEK.pipeline"
```

### 5. 看 3MF 内部

```bash
unzip -l output/.../foo.3mf
unzip -p output/.../foo.3mf 3D/3dmodel.model | head -200
```

或在 Bambu Studio 里 "Object → Split" 看每个对象。

---

## 五、所有外部依赖一览

### Python 库（`requirements.txt`）

| 库 | 用在哪 |
|----|------|
| `numpy` | 几乎每一步 |
| `trimesh` | 网格容器、`split_terrain_mesh` 的法线分组 |
| `shapely` (>=2.0) | 多边形/线 几何运算 |
| `geopandas` (>=0.14) | OSM 数据容器 + `to_crs` |
| `pandas` | 列操作（`est_height`） |
| `osmnx` (>=2.0) | Overpass API（备用） |
| `pyproj` (>=3.6) | UTM 投影 |
| `requests` | SRTM HGT 下载 |
| `scipy` | KDTree（地形 Z 采样）+ ndimage（高斯平滑） |
| `rich` | 日志（terrain3d 内部用） |
| `mapbox-earcut` | 地形底盖三角化 |
| `manifold3d` (>=3.4) | **核心**：所有布尔并/差/交 |

### 外部 CLI / 服务

| 名称 | 必需 | 装法 |
|------|------|------|
| `osmium-tool` | 用 `generate_westlake_cli.py` 必需；用 `pipeline.run` 不必需（走 osmnx） | `conda install -c conda-forge osmium-tool` |
| `ogr2ogr` (GDAL) | 与 osmium 配合，水体 relation 精确裁剪 | 跟随 GDAL 安装 |
| AWS S3 `elevation-tiles-prod` | SRTM HGT 镜像 | 自动 |
| Open Elevation API | SRTM 兜底 | 自动 |
| Overpass API | OSM 在线兜底（osmnx） | 自动 |

### 标准库

| 模块 | 用途 |
|------|------|
| `xml.etree.ElementTree` | 3MF XML |
| `zipfile` | 3MF 打包 |
| `re` | 校验 3MF |
| `concurrent.futures` | 不再使用（已删） |

---

## 六、记忆点：每一步去哪儿看日志

```
Stage 0  →  "UTM zone:" / "Scale:"
Stage 1  →  "Grid shape:" + "Elevation range:"
Stage 2  →  "[CLI Pipeline] Complete: N features"
Stage 3  →  "Time: Xs"（无显著输出）
Stage 4  →  "[Step 1] 地形重建..." → "[Step 3] 水体布尔差集镂空..." → "Watertight: True"
Stage 5  →  "Building faces:"
Stage 6  →  "Road faces:"
Stage 7  →  "Water features: N extruded from M polygons"
Stage 8  →  "[植被遮挡处理]" 多步
Stage 9  →  "File size:" + "Time:"
Stage 10 →  "Validation Report: ... Overall: PASSED"
```

任何一步出问题，先在屏幕里搜对应行；多数情况下 Watertight=False / faces=0 / Z range=0 都能直接说明根因。
