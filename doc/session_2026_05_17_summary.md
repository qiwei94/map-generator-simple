# Session 2026-05-17 — 地标 + 视觉中心架构

## 核心架构 pivot

> **"突出地标 / 视觉中心，其它风格化"**

```
Foreground（地标，独立标注，永远凸出）:
  ★ 建筑标签地标       绿色 #22aa55 + 标签
  ★ 建筑大体量地标     橙红 #e85a2c
  ★ 植被命名地标       深森林绿半透 #2d6e3a
  ★ 水体命名地标       深湖蓝 #0e74a8
  ★ 道路桥梁地标       仅文字标签

Background（兜底，统一风格化）:
  ░ 建筑街区填充 (BO)   浅蓝 block_fill
  ░ 街道 grid           灰线
  ░ 地形                灰底
  ░ 细碎水/植被         opt-in 半透
```

## 今日落地清单

### 1. 8×8 干扰矩阵 + geometry 减法（#55）

修复了"地标参与 block_fill count/density 计算"的 bug，让地标周围的 block 更易被认定为城市区域，避免地标"漂浮在空白里"。

**核心规则（5 步预处理减法）**:
```python
all_landmarks_geom = unary_union(BL ∪ VL ∪ WL)
WO_clean = WO − WL
BO_clean = BO − all_landmarks
VO_clean = VO − all_landmarks − BO_filled_blocks
VL_clean = VL − BL
```

确保高优先级永远不被低优先级覆盖。

### 2. 4 类地标识别（#50, #54, #56）

新增 `_TEXTURE_STYLE_OF_DEEPSEEK/_landmark.py`:

| 类别 | 函数 | 信号 |
| --- | --- | --- |
| 建筑 | `is_tag_landmark` | wikidata/historic/temple/name+...（已有，今日加 hotspot 参数）|
| 植被 | `is_vegetation_landmark` | boundary=national_park / leisure=nature_reserve / 命名+自然类标 |
| 水体 | `is_water_landmark` | wikidata / 命名水体 / **水关系兜底（不需 name）** |
| 道路 | `is_road_landmark` | bridge=yes + 命名 + 主干道 |

杭州 25km 命中：
- 建筑 3,403 + 121 大体量
- 植被 61（含西溪国家湿地、西湖风景区、五云景区...）
- 水体 90（西湖、湘湖、**钱塘江 36km²**、京杭运河）
- 道路 277（钱塘江大桥、复兴大桥...）

### 3. PNG 标注层（#56）

参考 `demo/杭州/01杭州模型5.jpg` 风格：

- 顶部城市英文（"Hangzhou"）
- 4 类地标 top N（默认 6）按 priority 评分排序
- 中英双行文字 + 引线（matplotlib annotate）
- 标签放图侧外（左右 margin），按 y 坐标均布
- 中文字体：PingFang HK / Heiti TC

CLI: `--annotate --annotate-top N --with-water-landmarks`

### 4. 热点区放宽建筑地标阈值（#57，spec 方案 E）

新增 `compute_hotspot_block_ids`：按"非空 block 中建筑面积 / block 面积"取 top X% 为热点。

热点区内放宽：
- Tier 3b 面积阈值 1500 → 800m²
- Tier 4（hotspot 专属）：commercial/retail/office/hotel + name → 算地标

CLI: `--hotspot-relax 10`

杭州 +43~150 个，重庆 +9，西区 OSM 数据相对完整，热点带来的边际增益不大。

### 5. 水体 LineString → buffered Polygon（钱塘江/京杭运河）

OSM 数据中钱塘江有两种形态：
- **MultiPolygon (relation)**：36 km² 主体——但 osmium export 后 **name 字段丢失**
- **LineString (waterway=river)**：中心线，分多段

修复两套：
- LineString 地标按 `WATERWAY_HALF_WIDTH` (river=90m / canal=25m) buffer 成多边形渲染
- MultiPolygon 加 **Tier 4 兜底**：`natural=water + water=river + ≥ 10 公顷` 即使 name 丢失也认地标

杭州 WL 13（仅 polygon）→ 75（+LineString）→ **90**（+Tier 4 钱塘江主体）

### 6. tune_buildings_v2.py 支持任意 bbox（#53）

加 `--city {westlake,chongqing,chicago}` + `--lat1/lon1/lat2/lon2`，自动调用 osmium fetcher 下数据。重庆 25km 跑通，Chicago 等 PBF。

### 7. block_fill 输出强制凸+≥4 顶点（#52）

`_convex_quadrilateral` 把三角 block / 内凹 block 替换为 convex_hull（或 min_rotated_rectangle）。审美约束。

### 8. tune_buildings_v2 参数补全

加 `max_block_area_m2` 过滤山区/远郊巨型 polygonize cell（默认 500,000m²，杭州滤掉 54 个，重庆 58 个）。

## Spec 文档（设计先行）

今日按"先 spec 后实施"流程产出 3 份审批通过的 spec：

| 文档 | 内容 |
| --- | --- |
| `doc/spec_landmark_annotation.md` | PNG 4 类地标标注 + 8×8 干扰矩阵 + 5 步减法 + z-order |
| `doc/spec_extended_building_landmarks.md` | 5 种数据源对比（Wiki/Wikidata/POI/CSV/热点放宽），选了方案 E |
| （隐式 spec）block_fill 地标参与 count/density | 在对话内审批 |

## 关键代码改动

| 文件 | 增量 | 说明 |
| --- | --- | --- |
| `_TEXTURE_STYLE_OF_DEEPSEEK/_landmark.py` | +180 行 | is_water/is_road/landmark_priority/compute_hotspot_block_ids + Tier 4 兜底 |
| `tools/tune_buildings_v2.py` | +500 行 | bbox CLI + 4 类 records + 减法预处理 + 标注层 + hotspot 重分类 + 水体 LineString buffer |
| `_TEXTURE_STYLE_OF_DEEPSEEK/buildings.py` | +30 行 | landmark_polys 参数 + count/density 含地标 |
| `tools/diagnose_block.py` | +20 行 | LANDMARK_ONLY 类别 + 地标参与 |

## Verification 状态

| | 杭州 25km | 重庆 25km |
| --- | --- | --- |
| 建筑地标 | 3,524 | 867 |
| 植被地标 | 61 | 13 |
| 水体地标 polygon | 90 | 17 |
| 道路桥梁 records | 277 | （未统计）|
| block_fill 块 | 6,586 | 1,472 |
| 钱塘江 / 长江 显形 | ✅ | ✅ |
| 文字标注 | ✅ | ✅ |

PNG 输出：
- `output/tune_buildings_v2/BF_P1000000_T5_W_S60_N1_D0_westlake_25km.png`
- `output/tune_buildings_v2/BF_P1000000_T5_W_S60_N1_D0_chongqing_25km.png`

## Open items

| 优先级 | 项 | 说明 |
| --- | --- | --- |
| ★ | **照片 GPS 重点描述** | 用户上传照片，EXIF GPS 找最近 OSM 建筑，单独 highlight + 可选放大 |
| ★ | 把这套 PNG 渲染逻辑移到 3MF | buildings 已分 landmark/ambient sub-mesh，待加 water_landmark / road_landmark / vegetation_landmark sub-mesh + EXTRUDER_MAP 扩展 |
| ☆ | Wikipedia geosearch 接入（spec 方案 A） | 暂时不做，OSM + 热点放宽已够 |
| ☆ | VO 植被 block_fill 完整实现 | 当前 `--with-veg-fill` 是 no-op |
| ☆ | Chicago 跑一次（待 Illinois PBF）| 用户自下载 |

## 经验记录

### 数据洞察

- **OSM relation 导出常丢 name**：钱塘江 multipolygon relation 在 osmium export 后 `name=NaN`。靠 `natural=water + water=river + 大面积`兜底
- **杭州 OSM 数据已较完善**：标签命中率高，热点放宽边际收益有限
- **重庆 OSM 稀疏**：建筑数仅杭州 25%，主城区有效但郊区空洞
- **block_fill 的"hotspot"语义**：top 10% 非空 block，要排除空 block 否则 percentile 全 0

### 工程模式

- **图片调参 → 3MF**（之前定的 workflow）今日继续受益：4 张 spec PNG 反复迭代，参数全在工具层，主管道未动
- **几何减法 > z-order**：渲染层叠脆弱，预处理 difference 才是干净方案
- **缓存版本控制**：`tune_v2_cache.{city}.{full,sub}.pkl` 加 required 字段检查，缺字段自动重读

### 美学约定

- 地标永远不透明（α=1.0）
- 大面积 landmark（VL）必须半透（α=0.55），否则盖一切
- 标签居外 + 引线，不挡地图
- block_fill 凸+≥4 顶点（"无三角 / 无内凹"）

---

next session 入口：照片 GPS 重点描述 OR 移植到 3MF。
