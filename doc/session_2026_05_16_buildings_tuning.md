# Session 2026-05-16 — Buildings 视觉调参专题

## 目标

让杭州 25km × 25km 模型的"建筑层"在视觉上接近 reference 锚点
`output/tune_buildings/_REFERENCE_HZ.png`（杭州 reference 3MF mesh 1+2 的 footprint
凸包近似），同时保持高级、低调、克制的美学。

## 方法论：图片调参 → 3MF 生成

3MF 完整生成耗时长（≥ 10min），不适合做密集参数实验。本次确立的工作流：

1. **图片调参**：抽出 buildings 聚合管道，单独写 CLI（`tools/tune_buildings_v2.py`），
   读 OSM 中间产物（`tmp/osmium_*.geojson`），直接渲染高分辨率 PNG（18 inch × 220 dpi
   ≈ 4000px）做参数对比，每组 ~5s。
2. **网格搜索**：每轮跑 22~36 组配置，肉眼比较，二分法收敛。
3. **锁定参数**：最佳一组的数值写回 `_TEXTURE_STYLE_OF_DEEPSEEK/config.py`。
4. **3MF 生成**：跑完整 pipeline 一次，验证视觉。

后续可推广到 `tune_water.py`、`tune_roads.py`、`tune_vegetation.py`、`tune_terrain.py`、
以及 `tune_compose.py`（叠加全图比对）。

## 算法演进

### v1：buffer-union（`tools/tune_buildings.py`）

```
≥ PRINT_LIMIT  → 个体保留
< PRINT_LIMIT  → buffer(+B) → unary_union → buffer(-B+slack) → simplify
```

**结果不及格** — 锯齿极多，边缘像棉花糖，没有 reference 的"街区方块感"。
原因：buffer-union 的边界由建筑分布决定，没有任何"街道"概念，跨马路/跨河
也会粘成一团。

### v2：road-network polygonize（`tools/tune_buildings_v2.py`，方案 c）

```
1. 道路 LineString + bbox 边界 → unary_union → polygonize → city blocks
2. 大建筑（≥ print_limit）个体保留
3. 小建筑按 centroid 分到 block
4. 块内三种聚合模式：
   - union          : unary_union(polys) ∩ block            （楼太散，碎片多）
   - buffered_union : unary_union(polys.buffer(B)) ∩ block  （默认，连片但保形状）
   - density_fill   : 楼总面积/block.area ≥ thr → 整个 block （最激进，纯街区块）
5. simplify 拉直边界
```

道路本身参与切分，天然解决"跨马路粘连"问题。

### v2.1：水体 + 路网（今日新增分支）

把 OSM 水体（湖、河）的 polygon 边界 + waterway LineString 也送进
polygonize 的 `lines` 列表，让钱塘江、西湖等天然边界也参与街区切分。

```python
# build_city_blocks(roads_gdf, ctx, road_tier, water_gdf=None)
for geom in water_gdf.geometry:
    if isinstance(geom, Polygon):
        lines.append(geom.exterior)
        for hole in geom.interiors: lines.append(hole)
    elif isinstance(geom, MultiPolygon): ...
    elif isinstance(geom, LineString): lines.append(geom)
```

CLI flag `--use-water`（默认关，保留旧分支）；网格 `--grid-n5` 跑 22 组带水
配置。

### v2.2：饱满形状 + 紧凑度过滤（今日继续推进）

用户复盘前一轮：`BU/T5/W/B=20/S=200` 已经有那味儿，但 union 形成的图形仍带
"指头 / 内凹 / 三角 sliver" — 这不是 simplify 能解决的，**simplify 只拉直边，
不能消除内凹**。新增三种"整形"模式 + 紧凑度过滤：

```
mode='convex_hull'   : union(buildings).convex_hull        ∩ block   — 楼簇凸包
mode='concave_hull'  : concave_hull(union, ratio=R)        ∩ block   — R∈[0,1]
mode='oriented_bbox' : union(buildings).min_rotated_rect   ∩ block   — 强制矩形
individual_shape ∈ {raw, convex, bbox}                                — 大楼也规整化
min_block_compactness=C : Polsby-Popper 4π·A/L² < C 则跳过该 block    — 去三角 sliver
```

紧凑度参考值：正方形 ≈ 0.785，正六边形 ≈ 0.907，长条 / 三角 sliver → 0。
建议 C ∈ [0.25, 0.40]。

## 关键发现（参数敏感度二分法实验）

### Round 1（25km, 36 组, mode=BU/DF）

候选样本：`BU_P1000_T5_B25_S5`、`BU_P3000_T5_B25_S5`、`BU_P8000_T5_B25_S5` 三张
"勉强还可以"。共同点：tier=5（最精细路网）、buffer=25、simplify=5。问题：仍然
碎、散、锯齿。

### Round 2（极端二分）

| 维度 | 区间 | 末端结果 |
| --- | --- | --- |
| simplify | 5 → 20 → 50 → 80 | blocks 5821 → 4050（**主要变量**）|
| buffer   | 25 → 60 → 120     | 仅 5% 变化（被 block 边界裁掉）|

**结论**：simplify 是主导参数；buffer 在 ≥ 25m 后失效。

### Round 3（N5：水体进 polygonize + density_fill 高 simplify）

跑了 22 组 25km 配置（产物在 `output/tune_buildings_v2/*_W_*.png`，文件名仍带
`_sub` 是当时的 substring bug，已修复）：

- 水体加入后 polygonize 在 25km 上输出 ~32k city blocks（仅路网时 ~30k）。
  钱塘江和西湖的湖岸线确实把环湖建筑切成了独立 block。
- density_fill 系列：
  - thr=0.15 → 大量 block 整块米白填充，城市像"满铺"
  - thr=0.25 → 平衡点，密集区块铺满 + 稀疏区显楼
  - thr=0.35 → 太严格，多数 block 走 fallback buffered_union
  - simplify=80 + thr=0.25 + tier=5：blocks ≈ 7500，边界明显更直
- buffered_union 对比组（P=3000, B=15/30, S=40/80）：仍是单元更小、保形状的风格。

22 张图待用户**肉眼挑选**，没有客观指标能跨过审美这关。

## 文件清单

### 改动 / 新增

- `tools/tune_buildings_v2.py`：
  - `load_data` 增加 water 加载（兼容老缓存自动失效）
  - `build_city_blocks(...water_gdf=None)`：水体边界进 noding
  - `run_one(...water_gdf, use_water)` 透传
  - `--use-water` CLI flag
  - `--grid-n5` 22 组水体网格
  - 文件名 `_W` tag 标识水体启用
  - 修复 `"5km" in "25km full"` 子串 bug（改用 `startswith`）

### 关键路径

- 输入数据：`tmp/osmium_{building,road,water}_30.13_120.01_30.36_120.29.geojson`
- 缓存：`tmp/tune_v2_cache.{full,sub}.pkl`（pickle 含 polys/roads/water/ctx）
- 输出：`output/tune_buildings_v2/{BU,DF,UN}_P*_T*[_W]_*.png`
- Reference 锚点：`output/tune_buildings/_REFERENCE_HZ.png`

### 当前 buildings.py 配置（`_TEXTURE_STYLE_OF_DEEPSEEK/config.py`）

```python
BUILDING_PRINT_LIMIT_M2 = 3500.0          # 0.4mm 喷嘴 × scale 物理下限 + 35% 余量
BUILDING_AGGREGATE_BUFFER_M = 20.0
BUILDING_AGGREGATE_SHRINK_SLACK_M = 5.0
BUILDING_AGGREGATE_SIMPLIFY_M = 15.0
BUILDING_AGGREGATE_HEIGHT_MM = 3.0
BUILDING_SIMPLIFY_TOL_M = 25.0
BUILDING_HEIGHT_MIN_MM = 2.8
BUILDING_HEIGHT_MAX_MM = 4.0
```

注意：当前的 `buildings.py` 还在用 v1 的 buffer-union 算法。v2（路网 polygonize）
的代码只在 `tools/tune_buildings_v2.py` 调试工具里，**还没有合并回主管道**。

## 进度状态

| 阶段 | 状态 |
| --- | --- |
| 项目代码精简 + 成熟库迁移（manifold3d/shapely/osmium） | done |
| 5 个 reference 3MF 结构分析 | done |
| 底板 + 水体（obj_4 manifold 挖洞） | done，用户已满意 |
| 道路 / 桥梁 / 植被 主管道 | done |
| 3MF 导出（OPC rels + sub-mesh + Bambu UUID） | done |
| Buildings 单阈值聚合主管道 | v1（buffer-union）已落 |
| **Buildings v2（路网+水体 polygonize）调参** | **在 PNG 阶段，未合并** |
| Buildings v2 → 写回 config + 主管道 | **TODO** |
| 全 pipeline 跑一次 25km 杭州 验证视觉 | TODO |

## 待办（按优先级）

1. **挑选最优 N5 配置**（22 张图肉眼选，需要用户决策）。
2. **把 v2 算法（路网+水体 polygonize）合并回 `_TEXTURE_STYLE_OF_DEEPSEEK/buildings.py`**：
   - 新增 `BUILDING_USE_ROAD_BLOCKS` / `BUILDING_USE_WATER_BLOCKS` 开关
   - 把 roads_gdf / water_gdf 透传进 `build_deepseek_buildings`
   - 在 pipeline.py Stage 5 加传参
3. **跑一次完整 25km 杭州 3MF**，肉眼对比 reference。
4. 推广 tune-via-PNG 到其它部分：
   - `tune_water.py`：水体颜色/边界 simplify/最小面积
   - `tune_roads.py`：道路宽度倍数 / 等级过滤
   - `tune_vegetation.py`：最小面积 / simplify 容差
   - `tune_terrain.py`：高程 gamma / smoothing sigma
   - `tune_compose.py`：把所有层合成全图，跟卫星图/参考叠加比对

## 经验记录

- **simplify 是主导，buffer 是次要**（被 block 边界裁掉）
- **substring 类型校验有坑**：`"5km" in "25km full"` 是 True
- **PNG 调参非常划算**：单组 5s vs 完整 pipeline 10min
- **水体不能只用主管道的 area filter**：必须把 boundary 送进 polygonize
  才能让"湖岸"成为切分线
- **density_fill thr 在 0.20~0.30 是甜蜜区**，> 0.35 大量 fallback 反而失去效果
- **重要洞察（2026-05-17）**：reference demo（杭州/武汉/重庆）的建筑层并非按真实 OSM
  建筑形状渲染，而是"路网+水网切 block + 阈值通过则整块填充"。这导致：
  - convex_hull / concave_hull / oriented_bbox / buffered_union 都是过度设计
  - 真正需要的算法只是 `block_fill`：count + density 双阈值通过 → 整 block 填充，
    不通过 → 直接丢弃（无 fallback）
  - `min_block_compactness` 把三角 sliver 滤掉，是必备过滤
- **N8 锁定参数（已写回 config.py）**：
  ```
  BUILDING_V2_MODE = "block_fill"
  BUILDING_V2_ROAD_TIER = 5
  BUILDING_V2_USE_WATER_BLOCKS = True
  BUILDING_V2_COUNT_THRESHOLD = 1
  BUILDING_V2_DENSITY_THRESHOLD = 0.05
  BUILDING_V2_MIN_BLOCK_COMPACTNESS = 0.30
  BUILDING_V2_AGGREGATE_SIMPLIFY_M = 60.0
  BUILDING_PRINT_LIMIT_M2 = 2500.0
  ```
  对应文件 `output/tune_buildings_v2/BF_P2500_T5_W_S60_C30_N1_D5_25km.png`。

## 渲染配色（调参 PNG，仅工具用）

| 图层 | 含义 | 颜色 | 备注 |
| --- | --- | --- | --- |
| 灰底 `#dadada` | 原 OSM 全量轮廓 | 浅灰 | 50% 透明，参考底图 |
| 街道线 | block 边界 | `#aaaaaa` 0.15px | tier=5 时密如蛛网 |
| 街区聚合 | 小楼 union 后的街区 | `#a8c8e8` 浅蓝 + 黑边 | 浅蓝 = "聚合"区 |
| **大楼个体** | **≥ PRINT_LIMIT 没被过滤** | **`#e85a2c` 橙红** + 0.4px 黑边 | **一眼定位地标分布** |

之前个体用米白 `#f0e4cc`，跟浅蓝街区对比度不足，25km 全图缩到屏幕大小后大楼几乎
看不见。改成橙红后地标层在街区底图上"点状"凸出，能直接判断 PRINT_LIMIT 阈值是否
合理（橙点过密 = 阈值太低，过疏 = 阈值太高，分布与城市真实地标分布是否吻合）。

注意：这是**调参工具**的可视化配色，**不影响 3MF 输出**。3MF 里 buildings 仍用
extruder 1（白 PLA），跟 reference 一致。
