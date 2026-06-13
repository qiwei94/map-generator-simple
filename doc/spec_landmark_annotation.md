# Spec：地标 + 风格化分层渲染（PNG 完整版）

## 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│ FOREGROUND（地标 / 视觉中心 / 突出）                              │
│ ─────────────────────────────────────────────────────────────── │
│ ★ 建筑标签地标       绿色实心 + 中英标签              已实现     │
│ ★ 建筑大体量地标     橙色实心 + 中英标签              已实现     │
│ ★ 植被命名地标       深森林绿半透 + 中英标签          已实现     │
│ ★ 水体命名地标       深湖蓝实心 + 中英标签            新增       │
│ ★ 道路桥梁地标       仅引线 + 中英标签                新增       │
│                                                                  │
│ BACKGROUND（细碎 / 风格化 / 城市底纹）                            │
│ ─────────────────────────────────────────────────────────────── │
│ ░ 细碎建筑（block_fill）    浅蓝/灰填充被路网切的 block  已实现   │
│ ░ 细碎植被（veg_block_fill）浅绿填充被路网切的 block     新增     │
│ ░ 细碎水体（small water）   半透明浅蓝直接绘制            新增   │
│ ░ 普通道路（road grid）     灰色细线（block 边界）        已实现 │
│ ░ 地形（terrain）            灰底                          已实现 │
└─────────────────────────────────────────────────────────────────┘
```

## Foreground：4 类地标识别

### 1. 建筑（buildings）— 已实现

`is_tag_landmark` 三层 + percentile + size 兜底，已定型。

### 2. 植被（vegetation）— 已实现

`is_vegetation_landmark`：wikidata / heritage / tourism / boundary=national_park / leisure=nature_reserve / 命名 + 自然类标 + ≥ 2 公顷。从 `osmium_vegetation_*.geojson` + `osmium_protected_area_*.geojson` 双源加载。

### 3. 水体（water）— **新增**

```python
def is_water_landmark(row, area_m2=None) -> bool:
    g = row.get
    # Tier 1
    if pd.notna(g("wikidata")) or pd.notna(g("wikipedia")):
        return True
    # Tier 2: 命名河流（任意大小，因为是线状）
    name = g("name")
    if pd.notna(name):
        wway = g("waterway")
        if pd.notna(wway) and wway in {"river", "canal"}:
            return True
        # Tier 3: 命名水体，≥ 5 公顷（过滤小区池塘/水景）
        if area_m2 is not None and area_m2 >= 50000.0:
            return True
    return False
```

**预期命中**：杭州 - 西湖 / 钱塘江 / 京杭运河 / 北里湖 / 外西湖；重庆 - 长江段 / 嘉陵江段 / 朝天门附近水域。

### 4. 道路桥梁（roads）— **新增**

普通道路全市数千上万条 name，不是地标，**仅取桥梁**：

```python
def is_road_landmark(row) -> bool:
    g = row.get
    if pd.notna(g("wikidata")) or pd.notna(g("wikipedia")):
        return True
    if pd.notna(g("bridge")) and g("bridge") not in ("no", "0"):
        if pd.notna(g("name")):
            return True
    return False
```

**预期命中**：钱塘江大桥 / 复兴大桥 / 之江大桥 / 重庆长江大桥 / 嘉陵江大桥...

## Background：风格化兜底

### A. 细碎建筑（block_fill）— **已实现**

```
路网+水网 polygonize → city blocks
每 block：count(non-landmark buildings) + density 双阈值
通过 → 整 block 浅蓝填充（凸+≥4 顶点）
不通过 → drop
+ 地标参与 count/density 但不参与几何（spec #55）
```

阈值：`count_thr=1`、`density_thr=0.005`、`compactness=0.0`、`max_block_area=500000`、`block_fill_convex=True`

### B. 细碎植被（veg_block_fill）— **新增**

**算法（与建筑 block_fill 同构）**：

```python
# 1. 加载所有非地标植被 polygons (vegetation_polys)
non_lm_veg = [p for p in vegetation_polygons if not is_vegetation_landmark(p)]

# 2. 分配到 city blocks（重用建筑的 blocks，避免再 polygonize）
veg_in_block = {ci: [poly_idx, ...]}    # via STRtree centroid query

# 3. 每 block：count + density 触发
for ci, vis in veg_in_block.items():
    block = blocks[ci]
    veg_count = len(vis)
    veg_area = sum(non_lm_veg[i].area for i in vis)
    veg_density = veg_area / max(block.area, 1.0)
    if veg_count >= veg_count_thr AND veg_density >= veg_density_thr:
        veg_blocks_filled.append(_convex_quadrilateral(block))
```

**阈值（待调）**：
- `veg_count_thr = 2` （单棵孤树不算，避免噪声）
- `veg_density_thr = 0.10`（10%，植被通常需更高密度才说明"绿化片区"）
- `max_block_area = 500000`（同建筑）
- `block_fill_convex = True`（同建筑）

**渲染色**：浅橄榄绿 `#bdd5a3`（中性，不抢地标深绿的视线）

### C. 细碎水体（small water）— **新增**

水体形状不规则、零散小池塘 < 5 公顷。**不做 block_fill 聚合**（水体不是按街区分布）。直接画原 polygon：

```python
non_lm_water = [p for p in water_polygons
                 if not is_water_landmark(p) and p.area >= 1000.0]
ax.add_collection(_polys_to_collection(
    non_lm_water, facecolor="#a8c8e0", edgecolor="none", alpha=0.7))
```

**渲染色**：浅湖蓝 `#a8c8e0` 半透。

### D. 普通道路 — **已实现**

`block` 外框 polyline `#aaaaaa` 线宽 0.15。维持现状。

## Foreground 标注（reference 风格）

参考 `demo/杭州/01杭州模型5.jpg`：

```
                Hangzhou
   ┌─────────────────────────────────────┐
西溪湿地 ──→ ▓▓░░  ▓▓                ←── 钱塘江
              ░░▓▓                        Qiantang River
   西湖 ──→  ░░░░                     ←── 市民中心
   └─────────────────────────────────────┘
```

### 优先级评分

```python
def landmark_priority(row, area_m2):
    score = 0
    if pd.notna(row.get("wikidata")):       score += 100
    if pd.notna(row.get("wikipedia")):      score += 50
    if pd.notna(row.get("heritage")):       score += 30
    if pd.notna(row.get("tourism")):        score += 20
    if pd.notna(row.get("historic")):       score += 20
    score += min(area_m2 / 10000, 50)
    return score
```

每类 top N（默认 6），按 score 倒排取头部。

### 标签布局（简单版）

- 按 centroid x 分左右两组
- 每组按 y 坐标排序，垂直均布在地图侧外
- 用 `ax.annotate(name, xy=center, xytext=label_pos, arrowprops=...)` 自带细引线
- 中英双行：第一行 `name`（OSM `name` 字段），第二行 `name:en`（缺时仅显示中文）

### 中文字体

```python
plt.rcParams['font.sans-serif'] = ['PingFang HK', 'Heiti TC', 'STHeiti', 'sans-serif']
plt.rcParams['axes.unicode_minus'] = False
```

### 顶部城市英文名

```python
CITY_NAMES = {
    "westlake":  "Hangzhou",
    "chongqing": "Chongqing",
    "chicago":   "Chicago",
}
ax.text(0.5, 0.995, CITY_NAMES.get(city, city.title()),
        transform=ax.transAxes, fontsize=22, ha='center', va='top',
        family='monospace', weight='bold')
```

## CLI 参数（新增）

```python
ap.add_argument("--annotate", action="store_true",
                help="加文字标签 + 引线（reference 风格）")
ap.add_argument("--annotate-top", type=int, default=6,
                help="每类地标显示前 N 个")
ap.add_argument("--with-veg-fill", action="store_true",
                help="把细碎植被也做 block_fill 风格化（默认关）")
ap.add_argument("--with-small-water", action="store_true",
                help="画细碎水体（默认关）")
ap.add_argument("--veg-count-thr", type=int, default=2,
                help="植被 block_fill 的 count 阈值")
ap.add_argument("--veg-density-thr", type=float, default=0.10,
                help="植被 block_fill 的 density 阈值")
```

## 冲突解决（核心）—— 8 类两两干扰矩阵

### 8 个分类（4 元素 × 2 子类）

| 缩写 | 元素 | 子类 | 当前渲染 | 几何来源 |
| --- | --- | --- | --- | --- |
| **BL** | 建筑 | 地标 | 实色 + 标签 | OSM building landmark |
| **BO** | 建筑 | 普通 (block_fill) | 浅蓝填充 block | 路网+水网 polygonize 后选块 |
| **VL** | 植被 | 地标 | 半透深绿 + 标签 | OSM vegetation/protected_area landmark |
| **VO** | 植被 | 普通 | 浅橄榄填充 block （新提议） | 同 polygonize block，按密度筛 |
| **WL** | 水体 | 地标（西湖/钱塘江/...） | 实色深蓝 + 标签（新） | OSM 命名水体 |
| **WO** | 水体 | 普通（小池塘/水景） | 半透浅蓝（新） | OSM 水体 polygon - WL |
| **RL** | 道路 | 地标桥梁 | 仅标签（新） | OSM bridge=yes + name |
| **RO** | 道路 | 普通 | 灰色 block 边线 | 路网 polyline |

### 8 × 8 干扰矩阵

行 = "下层"（先画/被覆盖方），列 = "上层"（后画/覆盖方）。
- ✅ = 正确叠加，无需处理
- ⚠ = 需要 geometry 减法或 z-order 强约束
- ─ = 几何上不会重叠 / 不相关
- "BL 上" = BL 永远在上，作为基准

|         | **BL**  | **BO**  | **VL**  | **VO**  | **WL**  | **WO**  | **RL**  | **RO**  |
| ---     | ---    | ---    | ---    | ---    | ---    | ---    | ---    | ---    |
| **BL** (基准) | —      | ⚠ #1   | ⚠ #2   | ⚠ #3   | ⚠ #4   | ⚠ #5   | ✅     | ─      |
| **BO**  | BL 上   | —      | ⚠ #6   | ⚠ #7   | ⚠ #8   | ⚠ #9   | ✅     | ✅     |
| **VL**  | BL 上   | BO 上 ⚠ #6 | —    | ⚠ #10  | ⚠ #11  | ⚠ #12  | ✅     | ✅     |
| **VO**  | BL 上   | BO 上 ⚠ #7 | VL 上 | —      | ⚠ #13  | ⚠ #14  | ✅     | ✅     |
| **WL**  | BL 上 ⚠#4 | BO 上 ⚠ #8 | VL 上 ⚠ #11 | VO 上 ⚠ #13 | —      | ⚠ #15  | ✅     | ✅     |
| **WO**  | BL 上   | BO 上 ⚠ #9 | VL 上 ⚠ #12 | VO 上 ⚠ #14 | WL 上 ⚠ #15 | —      | ✅     | ✅     |
| **RL**  | BL 上   | ─      | ─      | ─      | ─      | ─      | —      | ─      |
| **RO**  | BL 上   | BO 上   | VL 上   | VO 上   | WL 上   | WO 上   | ✅     | —      |

### 15 个具体冲突的处理

| #  | 场景 | 几何现实 | 处理 |
| -- | --- | --- | --- |
| 1  | 雷峰塔（BL）站在 block_fill 街区（BO）里 | BL 完全在 BO 内 | **BO = BO − BL**（block_fill 几何减去地标 footprint，让 BO "环绕"地标） |
| 2  | 灵隐寺（BL）在灵隐景区（VL）内 | BL 完全在 VL 内 | VL α=0.55 半透 + z-order BL 在上 + **VL = VL − all_BL**（地标顶上不画绿底） |
| 3  | 一栋小区（BL）在小绿地（VO）里 | BL 完全在 VO 内 | **VO = VO − all_BL**（同 #2） |
| 4  | 雷峰塔（BL）在西湖（WL）边 | BL 不与 WL 内重叠（建筑不在水里） | 实际不重叠，z-order 兜底 |
| 5  | 一栋楼（BL）跨过小池塘（WO）边缘 | BL 与 WO 重叠少量边缘 | **WO = WO − all_BL**（楼不被水盖） |
| 6  | block_fill 街区（BO）覆盖了西湖风景名胜区（VL）的城市部分 | BO 与 VL 边界部分重叠 | **BO = BO − VL**（避免大片绿色被蓝色街区盖） |
| 7  | block_fill 街区（BO）压过零散绿地（VO） | 同 block 同时触发 | **建筑优先**：先算 BO，已 fill 的 block 不再算 VO |
| 8  | block_fill 街区（BO）压过西湖（WL） | 一般不会，因为 WL 边界参与 polygonize 切 | 兜底：**BO = BO − WL** |
| 9  | block_fill 街区（BO）压过小池塘（WO） | 偶尔（block 内含小水） | **BO = BO − WO** |
| 10 | 灵隐景区（VL）压过零散绿地（VO） | VO 完全在 VL 内 | **VL 在上**（不动 VO，让它被覆盖；实际 VL α=0.55 还能透出） |
| 11 | 西湖风景名胜区（VL）包含西湖（WL） | WL 完全在 VL 内 | **WL 在上**（深蓝盖在半透绿上 → 看到边界仍是 VL，水面是 WL）|
| 12 | 灵隐景区（VL）内的小池塘（WO） | WO 在 VL 内 | **WO 在上**（小水点缀在景区里）|
| 13 | 小绿地（VO）压过西湖（WL） | 一般不会 | 兜底：VO 优先级低，**VO = VO − WL** |
| 14 | 小绿地（VO）压过小池塘（WO） | 极少；通常先 polygonize 切开 | 兜底：**VO = VO − WO** |
| 15 | 小池塘（WO）压过西湖（WL） | OSM 数据里 WO 通常不在 WL 内（已被命名为 WL） | **WO = WO − WL**（如果存在） |

### 实现：纯 geometry 减法 + 单遍 z-order

**预处理阶段**（一次性减法，O(N) STRtree 查询）：

```python
# 1) 把所有"地标"拢到一起（高优先级集合）
all_landmarks_geom = unary_union([*BL_polys, *VL_polys, *WL_polys])  # RL 是 LineString 不算

# 2) 普通水体减地标水体
WO_clean = subtract(WO_polys, WL_polys)

# 3) 建筑 block_fill 减所有地标 (BL/VL/WL)，形状变成"环绕地标"
BO_clean = subtract(BO_polys, all_landmarks_geom)

# 4) 植被 block_fill 减所有地标 + 建筑 block_fill 已占的 block
VO_clean = subtract(VO_polys, unary_union([all_landmarks_geom, *BO_filled_block_geoms]))

# 5) 植被地标减建筑地标（让 BL 顶上不画绿底）
VL_clean = subtract(VL_polys, BL_polys)
```

`subtract(A_list, B_geom)` 实现：

```python
def subtract(polys: List[Polygon], minus_geom) -> List[Polygon]:
    if minus_geom is None or minus_geom.is_empty: return polys
    out = []
    for p in polys:
        if not p.intersects(minus_geom):
            out.append(p); continue
        diff = p.difference(minus_geom)
        if diff.is_empty: continue
        if isinstance(diff, Polygon):
            out.append(diff)
        elif hasattr(diff, "geoms"):
            out.extend(g for g in diff.geoms if isinstance(g, Polygon) and not g.is_empty)
    return out
```

**渲染阶段**（一次性 z-order，无需特殊处理）：

```
1.  terrain                    full opaque
2.  RO (road grid lines)       opaque (only thin lines, doesn't really cover anything)
3.  WO_clean                   α=0.65 light blue
4.  VO_clean                   α=0.55 light olive
5.  BO_clean                   α=0.85 light blue
6.  VL_clean                   α=0.55 dark forest green (transparent so BL inside still visible)
7.  WL                         α=1.00 deep blue (opaque, identity)
8.  BL                         α=1.00 (size: orange / tag: green)
9.  RL                         labels only
10. annotations                over everything
11. city title                 top
```

### 优先级总规则（用作裁决任何未列冲突）

```
建筑地标 (BL) ＞ 水体地标 (WL) ＞ 植被地标 (VL，半透) ＞
建筑普通 (BO) ＞ 植被普通 (VO) ＞ 普通水体 (WO) ＞ 路网 (RO) ＞ 地形
```

道理：
- **BL 最尖锐易丢**（小目标），永远在最上
- **WL 第二**（深蓝大色块，标识城市的"水"性格）
- **VL 半透**（大面积如不半透会盖一切，半透才能既看到边界又看到内部）
- **BO 高于 VO**（城市纹理本应是"建筑感"为主）
- **WO 低于 BO**（被路网切的零散水滴是次要）
- **RO 极低层**（仅 polyline 几乎不挡视线，但要画出街道结构）

### 解决原则

#### 原则 1：z-order 严格（小且独特 → 上层）

```
─── 重画 z-order ────────────────────────
1. terrain                  完全不透 #888
2. veg block_fill           α=0.55  浅橄榄 #bdd5a3   (BG: 大块铺底)
3. small water              α=0.65  浅湖蓝 #a8c8e0   (BG: 散点)
4. road grid lines          完全不透 #aaaaaa 0.15px  (BG: 街道骨架)
5. building block_fill      α=0.85  浅蓝   #a8c8e8   (BG: 街区填充)
6. veg landmarks            α=0.55  深森林绿 #2d6e3a (FG: 大片)
7. water landmarks          α=1.00  深湖蓝 #0e74a8   (FG: 西湖类)
8. building landmarks size  α=1.00  橙红   #e85a2c   (FG: 大体量)
9. building landmarks tag   α=1.00  翠绿   #22aa55   (FG: 小但精)
10. annotations + leader    α=1.00  黑文字 + 灰引线  (顶层)
11. city name title         α=1.00  深字大字          (顶顶层)
```

**关键点**：
- **Foreground 全部 α=1.0 不透明**：地标永远不会被透过看见底层
- **Background 全部 α<1.0 半透**：底层之间能透出地形，避免完全密不透风
- **小且精的层在最上**：building landmarks（最易丢的小目标）排第 8/9 高于 water landmark
- **veg landmark α=0.55**：因为面积大（西溪 11km²、西湖风景区 59km²），不透会铺一片深绿盖掉里面所有建筑地标。半透才能既看到边界又能透出里面的雷峰塔等

#### 原则 2：geometry 减法（关键预防 #1）

**block_fill 的几何在合并前减去所有地标 footprint**：

```python
# 渲染前预处理
def subtract_landmarks_from_block_fill(block_fill_polys, all_landmark_polys):
    """从 block_fill 输出 polygon 中扣掉地标 footprint，留下"地标周围的城市纹理"。"""
    if not all_landmark_polys:
        return block_fill_polys
    union_lm = unary_union(all_landmark_polys)
    out = []
    for bp in block_fill_polys:
        diff = bp.difference(union_lm)
        if diff.is_empty: continue
        if isinstance(diff, Polygon):
            out.append(diff)
        elif hasattr(diff, "geoms"):
            out.extend(g for g in diff.geoms if isinstance(g, Polygon) and not g.is_empty)
    return out
```

效果：block_fill 真的"环绕"地标，不会盖在地标上。**z-order 不再需要小心翼翼**。

类似减法用于：
- veg block_fill 也减去 vegetation landmark 的 footprint
- small water 减去 water landmark
- 建筑地标 footprint 也从 veg block_fill 中扣（防止地标顶上画绿色）

#### 原则 3：building vs vegetation block_fill 优先级

同一 block 同时触发建筑 + 植被填充时，**建筑优先**（城市纹理本应是建筑感）：

```python
# 第一遍：算 building block_fill
filled_block_ids = set()
for block in blocks:
    if building_count >= ... and density >= ...:
        building_filled_blocks.append(block)
        filled_block_ids.add(block_id)

# 第二遍：算 veg block_fill，但跳过 building 已占的 block
for block in blocks:
    if block_id in filled_block_ids:
        continue  # 让位给建筑
    if veg_count >= ... and veg_density >= ...:
        veg_filled_blocks.append(block)
```

#### 原则 4：annotations 强制居外 + 引线避让

**布局规则**：

```
┌──── annotation margin（左外）────┐    ┌──── annotation margin（右外）────┐
│                                  │    │                                  │
│ 西湖 ─────────────→  ●           │    │              ●  ←───── 钱塘江   │
│ West Lake          (centroid)    │    │           (centroid) Qiantang R. │
│                                  │    │                                  │
│ 雷峰塔 ──────────→  ●            │    │              ●  ←──── 灵隐寺    │
│ Leifeng Pagoda     (centroid)    │    │           (centroid) Lingyin     │
└──────────────────────────────────┘    └──────────────────────────────────┘
                  ↑                                       ↑
            map area starts here                   map area ends here
```

实现：
- figure 总尺寸 22 × 18 inch（左右各加 2 inch margin 给标签）
- map 占中间 18 × 18
- 每个标签放在 ax.transAxes 坐标系，x ∈ {-0.10, 1.10}（外边界外）
- y 坐标按 centroid y 分组排序，避免内部交叉
- 引线 `arrowstyle='-'` 细线，深灰 `#444` 0.6 px

**简单避撞**：每侧 top N 按 y 排好后，强制 y 间距 ≥ `0.10 * map_height`。如果挤不下就跳过末尾的（top N 自然减）。

#### 原则 5：标签引线本身不画在地标几何上

引线起点在地标 centroid，终点在 margin。引线**穿越其它地标的概率小**（margin 在外，引线大致水平进入），即便穿过也是细线不显眼。**不做避撞**——成本高。

#### 原则 6：alpha 不叠加成深色

matplotlib 多层 α=0.55 半透叠加会变深。**预防**：
- veg landmark + building block_fill 重叠：building 在上 α=0.85，veg 半透在下，叠加后 ~95% 接近不透——可控
- veg landmark + veg block_fill 重叠：geometry 减法已避免（原则 2）
- small water + water landmark：geometry 减法（原则 2）

### 实现总结

| 层 | 关键预防机制 |
| --- | --- |
| building block_fill | 减地标 / 减 building 自身地标 |
| veg block_fill | 跳过已被 building 占的 block / 减地标 |
| small water | 减 water landmark |
| veg landmark | α 半透（避免完全盖掉建筑地标） |
| 其它 landmark | α=1.0 不透、z-order 在上 |
| annotations | 强制居外 + y 间距 |

## 改动文件清单

| 文件 | 改动 | 行数估计 |
| --- | --- | --- |
| `_TEXTURE_STYLE_OF_DEEPSEEK/_landmark.py` | 加 `is_water_landmark` / `is_road_landmark` / `landmark_priority` | ~70 |
| `tools/tune_buildings_v2.py` | load_data fetch water/road landmarks; `aggregate_in_blocks` 加 `vegetation_polys` 参数 + veg_block_fill 分支; render 加多层 + 标注层 | ~250 |
| `_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/osmium_cli_fetcher.py` | 已有 water/road，无需改 | 0 |

不动：`buildings.py` 主管道、`config.py`、`exporter.py`、`pipeline.py`、`generate_*_cli.py`。

## 验证用例

| # | 命令 | 预期 |
| --- | --- | --- |
| 1 | `--city westlake --annotate` | 顶部 "Hangzhou" + 4 类地标各 ≤6 个标注 |
| 2 | `--city westlake --annotate --with-veg-fill --with-small-water` | 多层背景：浅绿小绿地 / 浅蓝小池塘 + 4 类地标标注 |
| 3 | `--city chongqing --annotate` | 朝天门 / 长江 / 嘉陵江 / 解放碑 / 嘉陵江大桥... |
| 4 | 不加任何 flag | 跟当前一样，无改动 |

## 性能

- 水体 / 桥梁地标识别：< 100ms
- 植被 block_fill：和建筑 block_fill 同量级，~1s
- 细碎水体直绘：~0.5s
- 标注（matplotlib annotate ~30 个）：~1s

总增量 < 3s。

## 不在范围（明确说清）

- ❌ 不动 `_TEXTURE_STYLE_OF_DEEPSEEK/buildings.py`、`pipeline.py`、`exporter.py`
- ❌ 不输出 3MF（图片满意后另起任务）
- ❌ 不引入用户照片 GPS 高亮
- ❌ 不做 label 自动避撞（只做简单左右分组 + y 均布）
- ❌ 不改既有渲染色（建筑/植被/水体 landmark 色不变）

## 文档持久化

完成后：
- 更新 `doc/session_2026_05_16_buildings_tuning.md` 记录 4 类 landmark 检测规则汇总 + 兜底风格化算法

---

## 审批节点

请确认：

1. **Foreground 4 类**地标识别都要做（buildings 已 / vegetation 已 / **water 新** / **road bridge 新**）？
2. **Background 风格化**新增范围（**veg_block_fill 新** / **small water 新** / 原有维持）？
3. **CLI flags**：`--annotate`、`--with-veg-fill`、`--with-small-water` 都默认关，显式开启？
4. **不动 3MF** / 不动主管道 OK？

OK 后我开干。
