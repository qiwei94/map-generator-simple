# 水体保真度检查（Water Fidelity Check）

> 「生成之后跟标准地图比较」——数据缺口不可能一个个手动修，正确做法是生成后
> 自动与地理对齐的标准地图对比，批量检测缺失/断裂的水体要素。

## 1. 背景与动机

建筑浮雕地图的水体来自 OSM。OSM 源数据常有缺口（河流分段、运河箱涵段未绘制、
乡村池塘未标注等）。早期发现杭州**大运河中间断了 1.5km**，靠手写桥接多边形修复——
但这类缺口成百上千，**逐个手修不可扩展**。

因此建立自动化的「生成后 vs 标准地图」保真度检查，用**高德底图**作为地理对齐的
「标准答案」，自动找出 OSM 缺失的水体。

## 2. 两道关卡

| 关卡 | 实现 | 判据 | 依赖 | 适用 |
|---|---|---|---|---|
| **程序化几何检查** | `standard_map.check_water_fidelity()` | 高德水体 − OSM水体 的几何差集 | 仅需本地缓存，**离线、无需 API key** | 给确定性几何证据（位置/面积/圆形度） |
| **AI 视觉检查** | `agent/tools.check_against_standard_map()` | Qwen-VL 对比标准地图与渲染浮雕图 | 需 `DASHSCOPE_API_KEY` + 联网 | 凭视觉语境判读，能过滤几何噪声 |

两者**互补交叉验证**：几何检查给精确坐标但噪声多，AI 检查能凭视觉区分真水/噪声
但需 API key。理想流程是先跑几何检查拿候选清单，再用 AI 视觉复核。

## 3. 程序化检查 `check_water_fidelity()`

### 原理

```
缺失水体 = 高德水体(WGS84) − OSM水体(WGS84, 含buffer后的线)
        → 向内收缩 bbox 去边界伪影
        → 按面积筛选 → 按圆形度分类(紧凑=真水 / 细长=噪声)
```

### 用法

```python
from relief_studio.standard_map import check_water_fidelity

result = check_water_fidelity(
    bbox_wgs84=(120.01, 30.13, 120.29, 30.36),          # (min_lon, min_lat, max_lon, max_lat)
    osm_water_geojson="tmp/osmium_water_30.13...2900.geojson",
    viz_path="relief_studio/output/hz_fidelity.png",     # 可选诊断图
)
print(result["n_compact"], result["compact_total_m2"])   # 紧凑候选数 / 总面积(m²)
for g in result["compact_gaps"][:5]:                     # 按面积降序
    print(g["lat"], g["lon"], g["area_m2"], g["circularity"])
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `bbox_wgs84` | 必填 | `(min_lon, min_lat, max_lon, max_lat)` |
| `osm_water_geojson` | 必填 | OSM 水体 GeoJSON（Polygon + LineString） |
| `viz_path` | `None` | 输出诊断图 PNG（灰=OSM水，红=缺失水） |
| `min_area_m2` | `8000` | 只报告大于此面积的缺口 |
| `circularity_thresh` | `0.15` | 圆形度阈值（`4π·面积/周长²`），≥值为紧凑候选 |
| `inset_deg` | `0.02` | 分析区内缩度数（≈2km）去边界伪影 |
| `osm_buffer_m` | `15` | OSM 水体 union 外扩缓冲（吸收对齐误差） |
| `line_buffer_m` | `30` | OSM 线状水体 buffer 半宽 |

### 返回结构

```python
{
    "n_compact": 85,              # 紧凑候选水体数
    "n_noise": 81,                # 被过滤的细长噪声数
    "compact_total_m2": 9289387,  # 紧凑候选总面积
    "noise_total_m2": 4869270,    # 噪声总面积
    "compact_gaps": [{"lat", "lon", "area_m2", "circularity"}, ...],  # 按面积降序
    "noise_gaps":   [...],
    "viz_path": "...png" or None,
    # 高德缓存缺失时额外返回 "error"
}
```

### 四个关键陷阱（缺一即产生大量伪缺口）

1. **高德缓存已是 WGS84，勿重复转换**
   缓存由 `_water_supplement._vectorize_mask` 生成，其内部 `_merc_to_wgs84` 已调用
   `_gcj02_to_wgs84`。若再做一次 GCJ-02→WGS84，会把每块水平移 ~500m，
   造出数百个假缺口（杭州实测从 85 处暴涨到 382 处）。

2. **OSM 线状水体必须 buffer 后并入**
   OSM 常把河道绘成 `waterway=canal` 线（零面积）。若只用多边形做差集，
   线状河道会被误判为「缺失」。buffer `line_buffer_m` 后并入 OSM union 可避免。

3. **分析区向内收缩去边界伪影**
   高德瓦片范围比 OSM 裁剪范围略大，bbox 边界会产生大块碎屑。
   杭州实测最大的几块「缺口」全贴在 lon≈120.01/120.29 边界。内缩 `inset_deg` 去除。

4. **圆形度过滤分离真水与噪声**
   高德色彩分割掩膜会过度提取（路面阴影、暗色地物误判为水），呈细长碎屑。
   圆形度 `4π·面积/周长²` ≥ 阈值为紧凑真实水体（池塘/湖泊），< 阈值为噪声。
   杭州实测过滤掉 81 处 / 4.87 km² 噪声。

### 杭州实测基线

```
OSM 水: 208 多边形   高德水: 358 多边形
紧凑候选: 85 处, 9.29 km²    噪声(已滤): 81 处, 4.87 km²
Top3: 30.304/120.071 (95万m²) · 30.232/120.038 (74万m²) · 30.266/120.118 (74万m²)
```

## 4. AI 视觉检查 `check_against_standard_map()`

位于 `agent/tools.py`，已接入 LangChain agent 工具集。

- **输入**：生成的浮雕图路径
- **流程**：`get_standard_map()` 取标准地图 → Qwen-VL（`qwen-vl-max`）对比两图 →
  找出「标准地图有、浮雕图缺失/断裂」的水体
- **输出**（JSON）：
  ```json
  {"water_fidelity": 8, "missing_features": [{"type","location","problem"}],
   "note": "...", "standard_map_source": "amap_tiles", "standard_map_path": "..."}
  ```
- **依赖**：环境变量 `DASHSCOPE_API_KEY`（缺失时返回 error，此时应退回程序化检查）

## 5. 标准地图来源 `get_standard_map()`

```python
get_standard_map(bbox_wgs84, output_path, zoom=13) -> (path or None, source)
```

按优先级：
1. **`amap_tiles`** — 高德无标注底图瓦片拼接（`scl=2&style=7`），最完整，需联网
2. **`amap_water_cache`** — 渲染本地缓存水体矢量为蓝水白底图，离线可用

> 注：高德瓦片为 GCJ-02，与 WGS84 浮雕图有 ~500m 整体偏移，但对「某水体是否存在」
> 的视觉比对影响可忽略（相对 27km 边长 <2%）。

## 6. 能力边界（重要）

保真度检查**只能抓「OSM 缺、高德有」的水体**（大多数乡村池塘/水库/河段属此类）。

**抓不到「所有数据源都缺」的要素**——例如大运河箱涵段，OSM 与高德瓦片在
lat 30.2627~30.2765 之间**同时缺失**（两源误差 <10m），几何差集与 AI 对比都发现不了。
这类需靠**参考作品比对**（vs `city_demo` 成品）+ **人工抽查**兜底。

完整质量保障三层：
1. 保真度检查（vs 高德）—— 抓 OSM 缺失
2. 参考作品比对（vs 成品 demo）—— 抓所有源都缺的
3. 人工抽查 —— 最终兜底

## 7. 文件索引

| 文件 | 职责 |
|---|---|
| `standard_map.py` | `get_standard_map()` 标准地图获取 + `check_water_fidelity()` 程序化检查 |
| `agent/tools.py` | `check_against_standard_map()` AI 视觉检查工具 |
| `agent/relief_agent.py` | agent 工作流（第 4 步保真度检查 + 决策规则） |
| `run_hangzhou.py` | `_grand_canal_bridge_polygon()` 大运河手动桥接（数据源都缺的兜底案例） |
