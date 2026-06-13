# 数据依赖、性能与缓存策略

> 时间：2026-05-15
> 适用：`_TEXTURE_STYLE_OF_DEEPSEEK/pipeline.py` 与 `generate_*.py` 入口

---

## 一、数据依赖：必须 vs 可选

### 1.1 真正必须依赖外部的"数据"

只有两类，都是地理实体数据（**这是物理世界的事实，不可能本地造**）：

| 数据 | 来源 | 入口 file:line | 缓存路径 |
|------|------|----------------|----------|
| **DEM 高程** | SRTM HGT 1°×1° 瓦片，AWS S3 镜像 | `terrain3d/fetchers/elevation.py:40-43` | `cache/srtm/*.hgt` |
| **OSM 矢量** | Geofabrik PBF（一次性下载） | `tools/manage_pbf.py:13-49` | `pbf_cache/*.osm.pbf` |

### 1.2 声明为外部源、但实际**非必须**的兜底

| 来源 | 文件 | 现实 |
|------|------|------|
| `api.open-elevation.com` | `elevation.py:36` | SRTM 没覆盖才走，城市级 3D 打印基本永不触发 |
| Overpass API（osmnx 提供） | requirements.txt:10 | 全项目 0 调用，**死依赖** |
| `aria2c` / `wget` 子进程 | `tools/manage_pbf.py:104-124` | 仅初次下 PBF；可换 `requests` |

### 1.3 外部 Python 库使用情况

| 库 | 实际用途 | 状态 |
|----|----------|------|
| `numpy` `scipy` `pandas` | 数值/几何 | 关键路径 |
| `trimesh` `manifold3d` | 网格 + 布尔运算 | 关键路径 |
| `shapely` `geopandas` `pyproj` | 矢量几何 + 投影 | 关键路径 |
| `mapbox-earcut` | 三角化 | 关键路径 |
| `requests` | HTTP（仅 SRTM 下载） | 关键路径 |
| **`osmnx`** | **零调用** | **可删** |
| **`rich`** | 一处 fallback 进度条 | **可删** |
| `rasterio` | 注释掉的死路径（升级 GeoTIFF 后才用） | 升级后启用 |

### 1.4 外部 CLI

| 工具 | 用途 | 状态 |
|------|------|------|
| **`osmium-tool`** | PBF → GeoJSON，比 pyosmium 快 10-20× | 必须，主路径 |
| `ogr2ogr`（GDAL） | water relation 后裁剪 | **可去** — `osm.py:OSMPipeline.step3_clip_bbox` 已经做了同样的事 |
| `aria2c` / `wget` | PBF 下载加速 | 一次性，可换 `requests` |

### 1.5 减少依赖的具体路径

按收益排：

1. **HIGH** 删除 `osmnx`（`requirements.txt:10`）— 顺带砍掉 networkx + scikit-learn ~80MB
2. **HIGH** `_run_relation_first_pipeline` 的 `ogr2ogr` 步替换为 `shutil.copy2 + step3_clip_bbox`（`osmium_cli_fetcher.py:548-575`），**整个 GDAL 工具链就不再需要**
3. **MEDIUM** 删除 `rich` (`requirements.txt:11`)，进度条用 `print(..., end="\r")` 代替
4. **MEDIUM** 删除从未触发的 `rasterio` 旧路径（`elevation.py:291-331`）— 注意：升级 Copernicus GeoTIFF 后会重新需要 `rasterio`，看 §三 P3
5. **MEDIUM** 删除 Windows-only 死代码（`osmium_cli_fetcher.py:75-99`）
6. **LOW** 砍 Open Elevation 兜底 + 把 `requests` 换成 stdlib `urllib.request`

---

## 二、性能瓶颈与优化方案

### 2.1 各 Stage 成本估计（25km × 25km，1024×1024 grid）

| Stage | 文件 | 复杂度 | 是否热点 |
|-------|------|--------|----------|
| 0 setup | `pipeline.py:90-105` | O(1) | 否 |
| 1 elevation | `elevation.py:fetch_elevation_grid` | O(grid)，瓦片下载串行 | 一般 |
| 2 OSM fetch | `osmium_cli_fetcher.py:367-429` | I/O，4 类串行 | **热点** |
| 3 project | `pipeline.py:152-163` | O(N polys) | 否 |
| 4 terrain + holes | `object4_terrain_with_holes.py` | O(grid) + O(W) Manifold | 中 |
| 5 buildings | `buildings.py:99-176` | O(N bldg) Python loop | 中 |
| 6 roads | `roads.py:173-238` | O(R lines × pts) Python loop | 中 |
| 7 water | `water.py:67-136` | O(W) batch_boolean | 已最优 |
| 8 vegetation + exclusions | `vegetation_exclusion.py` | O(N feat) per-feature Manifold | **热点** |
| 9 export 3MF | `exporter.py:43-93` | ET.SubElement × 3M | **热点** |
| 10 validate | `validator.py:35-50` | regex 重扫整个 XML | 中 |

### 2.2 Top 5 热点

| # | 位置 | 卡点 | 占比估计 |
|---|------|------|----------|
| **H1** | `exporter.py:76-87` `for v in m.vertices: ET.SubElement(...)` | 1M 面 → ~3M 个 Element 对象 | **20-35%** |
| **H2** | `vegetation_exclusion.py:94-134, 173-213` 每个建筑/道路单独 `cs.extrude` | 1000+ 次 Python 单线程 trip | **15-25%** |
| **H3** | `pipeline.py:123-137` 4 类 OSM 串行 | osmium 子进程互不依赖 | **15-25%** |
| **H4** | `roads.py:31-49 + 79-103` Python 求切线 / 法向 | 10k 路段 × 50 顶点 = 500k Python iter | **10-15%** |
| **H5** | `validator.py:36-50` regex 全文扫 100MB+ XML | 重复解析自己刚写的 | **5-10%** |

加分项隐形成本：
- `terrain3d/processors/terrain.py:207-224` `sample_terrain_z` **每次调用都重建 cKDTree** —— buildings/roads/vegetation 反复调
- `terrain3d/processors/terrain.py:76-91` `_generate_grid_faces` 1024² 上是 2M 次 Python 双 for
- `terrain3d/processors/terrain.py:60-70` `simplify_quadric_decimation` 静默吞异常 → 1024² 网格其实**根本没被裁掉**

### 2.3 提速 Roadmap

**1 小时一个、叠加 ~2× —— 先做：**

| ID | 改什么 | 文件 | 预期 |
|----|--------|------|------|
| Q1 | Stage 2 OSM 4 路并行（ThreadPoolExecutor） | `pipeline.py:123-137` | Stage 2 ~4× |
| Q2 | exporter 改 f-string + `"\n".join` 直拼 XML | `exporter.py:76-87` | Stage 9 ~5-10× |
| Q3 | terrain mesh 上挂 cKDTree 缓存 | `terrain3d/processors/terrain.py:207-224` | 隐形 5/6/8 全摊平 |
| Q4 | 向量化 `_generate_grid_faces` (numpy meshgrid) | `terrain3d/processors/terrain.py:76-91` | 去掉 2M Python iter |

**1 天的功夫、再叠 4-10×：**

| ID | 改什么 | 文件 |
|----|--------|------|
| Q5 | vegetation exclusion 先 `unary_union` 再单次 extrude | `vegetation_exclusion.py:72-226` |
| Q6 | roads 用 `shapely.segmentize` + numpy 切线向量化 | `roads.py:31-170` |
| Q7 | 加 `validate_meshes(mesh_dict)` 同进程内存校验 | `validator.py` + `pipeline.py:314` |
| Q8 | 修地形 decimation（`pyfqmr` 或 stride 降采样） | `terrain3d/processors/terrain.py:60-70` |

**已经做对的事**（参考 / 不要破坏）：

- `manifold3d.Manifold.batch_boolean` 在 `water.py:120` `water_column.py:77` `vegetation_exclusion.py:144,224,324` 等处已经走对，内部 TBB 并行
- `_geom_utils.collect_water_polygons` 让 obj3 / obj4 共用同一组水体多边形，水体浮雕和地形镂空 XY 完美对齐

---

## 三、数据缓存策略：PBF + DEM 双轨离线

### 3.1 设计原则

**所有外部地理数据都按 "下载一次，长期复用"**，运行时零网络。  
PBF 已经按这个模式跑通；DEM 现在补上。

### 3.2 PBF 现状（参考模板）

```
pbf_cache/
├── zhejiang-latest.osm.pbf        # 来源: download.geofabrik.de
├── chongqing-260508.osm.pbf
├── hainan-260508.osm.pbf
└── ...
```

工作流：
1. `python tools/manage_pbf.py download zhejiang` （aria2c / wget）
2. 脚本里 `set_pbf_file_path("pbf_cache/zhejiang-latest.osm.pbf")`
3. `fetch_water/buildings/roads/vegetation` 全部走本地 osmium CLI
4. **运行时不联网**

### 3.3 DEM 新建（对称）

```
dem_cache/
├── srtm/                          # 老路径，HGT 1°×1°，向后兼容
│   ├── N30/N30E120.hgt
│   └── ...
└── cop30/                         # 新路径，Copernicus GLO-30 GeoTIFF
    └── Copernicus_DSM_COG_10_N30_00_E120_00_DEM/
        └── *.tif
```

工作流（与 PBF 完全对称）：
1. `python tools/manage_dem.py download zhejiang --source cop30`
2. `fetch_elevation_grid()` 在 cop30 命中本地 → rasterio 读
3. fallback：cop30 没有 → 本地 SRTM HGT → **AWS 东京镜像**（国内稳定）→ us-east-1 兜底
4. 运行时只在首次访问陌生区域时联网

### 3.4 数据源稳定性矩阵（国内访问）

| 源 | 速度 | 是否要登录 | 建议 |
|----|------|------------|------|
| **gscloud.cn**（中科院） | ⭐⭐⭐ 满速 | 邮箱注册一次 | 手动批量下，最稳 |
| AWS 东京 `s3://elevation-tiles-prod`（SRTM HGT） | ⭐⭐⭐ 60ms | 否 | 改 `_SRTM_URLS` 一行就上 |
| AWS 东京 `s3://copernicus-dem-30m` | ⭐⭐⭐ | 否 | `aws s3 sync --no-sign-request --region ap-northeast-1` |
| 国家青藏高原科学数据中心 `data.tpdc.ac.cn` | ⭐⭐ | 注册 | NASADEM 优选 |
| OpenDataLab `opendatalab.com` | ⭐⭐ | 注册 | 子集，速度可 |
| `hf-mirror.com`（HuggingFace 国内镜像） | ⭐⭐ | 否 | 部分社区切片 |
| 原 `s3://elevation-tiles-prod`（us-east-1） | ⭐ ~250ms 经常超时 | 否 | **不推荐** |
| OpenTopography (US) | ⭐ 不稳 | API key | **不推荐** |
| NASA Earthdata 直连 | ✗ 经常超时 | 登录 | **不推荐** |

### 3.5 体积参考

| 范围 | SRTM 30m HGT | Copernicus GLO-30 | Copernicus GLO-90 |
|------|--------------|-------------------|-------------------|
| 1°×1° 瓦片 | 25 MB | 30 MB | 3 MB |
| 一个省 | 1-3 GB | 1-3 GB | 100-300 MB |
| 中国全境 | ~45 GB | ~45 GB | ~5 GB |
| 单个洲 | 30-100 GB | 30-100 GB | 3-10 GB |
| 全球 | ~360 GB | ~600 GB | ~60 GB |

PBF 的 `zhejiang-latest.osm.pbf` 是 88MB；同区域的 SRTM 大约 1-2GB，**还在同一个量级**。

### 3.6 更新节奏

| 数据 | 实际更新频率 | 建议 |
|------|--------------|------|
| OSM PBF | Geofabrik 每周更新 | 每月或重要场景前手动拉一次 |
| SRTM 1" | 2014 公开后基本不变 | 永久缓存 |
| Copernicus DEM GLO-30 | 2019 起小幅迭代 | 每年检查一次 |
| NASADEM | 2020 一次性发布 | 永久缓存 |

DEM 比 OSM 安静得多，**没必要日更**。

### 3.7 三段式实施计划

| 阶段 | 时间 | 改动 | 收益 |
|------|------|------|------|
| **P1 零代码改动** | 5 分钟 | `_SRTM_URLS` 把 ap-northeast-1 放第一位 | 国内立即稳定 |
| **P2 一次性下载脚本** | 半天 | `tools/manage_dem.py` 仿 `manage_pbf.py` | 离线优先 |
| **P3 质量升级** | 1-2 天 | `rasterio` + Copernicus GeoTIFF 主路径 | 数据质量 ⭐ |
| **P4 完全离线发布** | 按需 | 把目标地区 GLO-90 打包成项目附属包 | 团队同步 |

---

## 四、引用

具体 file:line 与 patch 见：

- `doc/project_understanding_and_plan.md` — Bug 列表与修复
- `doc/pipeline_walkthrough.md` — Stage 0–10 详解
- `doc/manifold_boolean_spec.md` — 布尔运算契约
- `doc/most_important_doc.md` — 参考模型与对象结构
