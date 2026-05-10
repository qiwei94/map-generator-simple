# 纯 osmium CLI 快速管线详解

> 高性能 OSM 数据获取方案：使用纯 osmium CLI 工具，无需 ogr2ogr/GDAL，速度提升 10-20 倍

## 管线架构图

```
原始 PBF (1GB+)                                              最终 GeoDataFrame
    │                                                             ↑
    ▼                                                             │
┌──────────────────────────────────────────────────────────────────┐
│  Step 1: osmium extract                                         │
│  ───────────────────                                            │
│  输入: zhejiang-latest.osm.pbf (浙江全省数据)                    │
│  操作: 按边界框裁剪 (25km×25km)                                  │
│  输出: westlake_area.pbf (~5MB)                                 │
│  时间: ~1秒                                                     │
│                                                                  │
│  注意: Windows 需加 --overwrite 避免文件冲突                      │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│  Step 2: osmium tags-filter                                     │
│  ──────────────────────                                         │
│  输入: westlake_area.pbf                                         │
│  操作: 按标签过滤（只保留水体相关）                              │
│  输出: westlake_water.pbf (~500KB)                              │
│  时间: ~0.5秒                                                   │
│                                                                  │
│  过滤表达式 (使用 nwr = node/way/relation):                       │
│  • nwr/natural=water   → 所有类型：湖泊                         │
│  • nwr/water=*         → 所有类型：水库、池塘                    │
│  • nwr/waterway=*      → 所有类型：河流、运河                   │
│  • nwr/landuse=reservoir → 所有类型：人工水库                    │
│                                                                  │
│  格式解释:                                                       │
│  • n = node (点)                                                 │
│  • w = way (线)                                                  │
│  • r = relation (关系)                                           │
│  • nwr = 全部类型                                                │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│  Step 3: osmium export -f geojson                               │
│  ───────────────────────────────                                │
│  输入: westlake_water.pbf                                        │
│  操作: PBF → GeoJSON 格式转换                                    │
│  输出: westlake_water.geojson                                   │
│  时间: ~0.1秒                                                   │
│                                                                  │
│  关键参数:                                                       │
│  • -f geojson    显式指定输出格式（必须）                        │
│  • --overwrite   Windows 必加，避免文件冲突                       │
│                                                                  │
│  输出内容:                                                       │
│  • FeatureCollection 格式                                        │
│  • 包含 Points（nodes）、LineStrings（ways）、                   │
│    Polygons/MultiPolygons（relations/areas）                     │
│  • OSM tags 作为 properties                                     │
└──────────────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│  Step 4: geopandas.read_file()                                  │
│  ────────────────────────                                       │
│  输入: westlake_water.geojson                                    │
│  输出: GeoDataFrame                                              │
│  时间: ~0.5秒                                                   │
│                                                                  │
│  结果示例:                                                       │
│  geometry   | name   | natural | waterway | ...                 │
│  ──────────┼───────┼─────────┼──────────┼────                 │
│  Polygon    | 湘湖   | water   | None     | ...                 │
│  MultiPoly  | 西湖   | water   | None     | ...                 │
│  LineString | 钱塘江 | None    | river    | ...                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## 具体命令示例（西湖 25km）

### Step 1: 区域裁剪

```bash
osmium extract ^
  -b 120.01,30.13,120.29,30.36 ^
  zhejiang-latest.osm.pbf ^
  -o westlake_area.pbf ^
  --overwrite
```

**参数解释:**

| 参数 | 说明 |
|------|------|
| `-b W,S,E,N` | 边界框（西、南、东、北） |
| `--overwrite` | **Windows 必加**，避免文件冲突 |

> **注意**: Windows 上必须加 `--overwrite`，否则会报 "系统无法把文件定位到不同的磁盘" 错误。

### Step 2: 标签过滤

```bash
osmium tags-filter ^
  westlake_area.pbf ^
  nwr/natural=water ^
  nwr/water=* ^
  nwr/waterway=* ^
  nwr/landuse=reservoir ^
  -o westlake_water.pbf ^
  --overwrite
```

**过滤表达式语法:**

| 表达式 | 含义 |
|--------|------|
| `nwr/natural=water` | 所有 `natural=water` 要素（湖泊） |
| `nwr/water=*` | 所有带 `water` 标签的要素 |
| `nwr/waterway=*` | 所有带 `waterway` 标签的要素（河流） |
| `nwr/landuse=reservoir` | 人工水库 |

**通配符说明:**

| 通配符 | 含义 | 示例 |
|--------|------|------|
| `*` | 匹配任意值 | `water=*` 表示有 water 标签即可 |
| 逗号 | 匹配多个值 | `nwr/water=lake,reservoir` |

### Step 3: 导出 GeoJSON

```bash
osmium export ^
  westlake_water.pbf ^
  -o westlake_water.geojson ^
  -f geojson ^
  --overwrite
```

**关键参数:**

| 参数 | 说明 |
|------|------|
| `-f geojson` | **必须指定**，显式输出 GeoJSON 格式 |
| `--overwrite` | **Windows 必加**，避免文件冲突 |

> **重要**: 如果不加 `-f geojson`，osmium 会输出 OSM JSON 格式，geopandas 无法读取。

### Step 4: Python 读取

```python
import geopandas as gpd

gdf = gpd.read_file("westlake_water.geojson")
print(gdf.columns)  # ['osm_id', 'name', 'natural', 'water', 'waterway', ...]
print(gdf.geometry.type.value_counts())
# MultiPolygon    29
# Polygon        185
# LineString     284
# Point          451
```

---

## 标签过滤器速查表

| 数据类型 | osmium tags-filter 表达式 |
|----------|--------------------------|
| **水体** | `nwr/natural=water nwr/water=* nwr/waterway=* nwr/landuse=reservoir` |
| **道路** | `nwr/highway=motorway,trunk,primary,secondary,tertiary,residential` |
| **建筑** | `nwr/building` |
| **植被** | `nwr/landuse=forest,grass,meadow nwr/natural=wood,grassland,scrub` |
| **公园** | `nwr/leisure=park,garden nwr/landuse=recreation_ground` |
| **湿地** | `nwr/natural=wetland,marsh,swamp` |

---

## 性能对比：为什么 CLI 更快？

```
┌─────────────────────────────────────────────────────────────────┐
│  纯 osmium CLI 方式 (推荐)                                       │
│                                                                  │
│  [PBF文件] → [磁盘流式读取] → [C++处理] → [磁盘写入]            │
│                                                                  │
│  特点:                                                           │
│  • 不需要加载全部数据到内存                                      │
│  • C++ 编译优化，CPU 效率高                                      │
│  • 直接操作二进制格式，无转换开销                                │
│  • 仅需 osmium-tool，无需 GDAL/ogr2ogr                           │
│  • 总耗时: 1-3秒                                                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Python pyosmium 方式                                            │
│                                                                  │
│  [PBF] → [Python解释器] → [对象创建] → [内存存储]               │
│        → [Shapely几何] → [GeoDataFrame构建]                      │
│                                                                  │
│  特点:                                                           │
│  • 每个对象需要 Python 解释器处理                                │
│  • Shapely 几何创建有开销                                        │
│  • Relation 组装需要额外逻辑                                     │
│  • 总耗时: 100-150秒                                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## 项目集成方案

`osmium_cli_fetcher.py` 已实现自动化管线，一行调用即可：

```python
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.osmium_cli_fetcher import fetch_from_cli

# 一行调用，自动执行完整管线（extract → tags-filter → export → GeoDataFrame）
gdf = fetch_from_cli(
    tag_type='water',
    south=30.13, west=120.01, north=30.36, east=120.29,
    pbf_file='pbf_cache/zhejiang-latest.osm.pbf'
)

# 输出的 GeoDataFrame 可直接用于后续处理：
from _TEXTURE_STYLE_OF_DEEPSEEK.water import build_deepseek_water
from _TEXTURE_STYLE_OF_DEEPSEEK.exporter import export_deepseek_3mf

water_mesh = build_deepseek_water(gdf, ...)
export_deepseek_3mf({'water': water_mesh}, 'output.3mf')
```

---

## 安装前置要求

```bash
# Anaconda 环境（仅需 osmium-tool，无需 GDAL）
conda install -c conda-forge osmium-tool
```

**Windows 注意事项:**
- osmium 安装在 `anaconda3\Library\bin\` 目录
- 所有 osmium 命令必须加 `--overwrite` 避免文件冲突
- stderr 含中文时需设置 `errors='replace'` 解码

---

## 完整管线到 3MF 示例脚本

项目已提供 `generate_westlake_cli.py`，演示完整管线：

```bash
python generate_westlake_cli.py
```

**管线执行过程:**
```
[Step 1/3] osmium extract (clipping area)...
           Done in 1.2s, output: 6496.0 KB
[Step 2/3] osmium tags-filter (filtering water features)...
           Done in 0.4s, output: 587.7 KB
[Step 3/3] osmium export (converting to GeoJSON)...
           Done in 0.1s, output: 4894.8 KB
[CLI Pipeline] Complete: 6190 features extracted
```

输出：
- `output/westlake_cli/water_plate_westlake_cli.3mf`
- 耗时对比：纯 osmium CLI ~2秒 vs Python pyosmium ~127秒

---

## Windows 常见问题

### 1. "系统无法把文件定位到不同的磁盘"

**原因**: osmium 拒绝覆盖已存在的文件

**解决**: 所有命令加 `--overwrite` 参数

### 2. osmium CLI 未找到

**原因**: osmium 在 `anaconda3\Library\bin\` 而非 `Scripts\`

**解决**: 代码已自动搜索 `Library\bin` 路径，确保使用 Anaconda Prompt 运行

### 3. GeoJSON 文件无法被 geopandas 读取

**原因**: `osmium export` 默认输出 OSM JSON 格式

**解决**: 必须加 `-f geojson` 参数

### 4. 中文错误乱码

**原因**: Windows stderr 使用 GBK 编码

**解决**: 代码已使用 `errors='replace'` 解码

---

## 河流 relation 提取最佳实践

从 QGIS 属性表可见，钱塘江等河流在 OSM 中以 **multipolygon relation** 形式存储：

```
relation = 6227206    ← OSM Relation ID（父级 multipolygon）
natural = water
water = river
waterway = river
source = PGS          ← 数据来源（Perry-Castañeda 地图库）
```

### 正确的提取顺序：先过滤，后裁剪

```bash
# Step 1: 从完整 PBF 过滤河流 relation（保持完整结构）
osmium tags-filter zhejiang.osm.pbf r/natural=water r/water=river -o all_rivers.pbf --overwrite

# Step 2: 导出为 GeoJSON
osmium export all_rivers.pbf -f geojson -o all_rivers.geojson --overwrite

# Step 3: 用 ogr2ogr 精确裁剪到目标范围
ogr2ogr -f GeoJSON xihu_rivers_25km.geojson all_rivers.geojson -clipsrc 120.038 30.138 120.262 30.362
```

### 为什么这个顺序正确？

| 步骤 | 工具 | 作用 | 原因 |
|------|------|------|------|
| **过滤** | `osmium tags-filter` | 提取河流 relation | 使用 `r/` 前缀保留完整 relation |
| **导出** | `osmium export` | PBF → GeoJSON | 必须加 `-f geojson` |
| **裁剪** | `ogr2ogr -clipsrc` | 裁剪到 bbox | 比 osmium extract 更可靠 |

### 常见错误

| 错误做法 | 问题 |
|----------|------|
| 先用 `osmium extract` 裁剪 | relation 数据丢失，多段河流被丢弃 |
| 使用 `nwr/` 前缀过滤 | 可能匹配不到 relation 本身 |
| 用 `--with-dependencies` | 不是有效参数 |
| 忘记 `-f geojson` | 导出格式错误，geopandas 无法读取 |

### 关键要点

- **`r/` 前缀**：只提取 relation 类型，保留完整的 multipolygon 结构
- **先过滤后裁剪**：osmium extract 对 relation 处理不友好，用 ogr2ogr 做最后的 bbox 裁剪
- **ogr2ogr `-clipsrc`**：精确裁剪几何体到 bbox 范围内，不丢失数据