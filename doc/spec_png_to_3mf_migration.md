# Spec：PNG → 3MF 迁移（实现细节版）

> 这份 spec 给执行模型（claude/gpt/...）用。每节都精确到函数签名、参数、返回值、测试命令。审查模型需对照本 spec 比对实际改动。

---

## Part A：架构原则

### A.1 PNG / 3MF 本质差异

| 维度 | PNG | 3MF |
| --- | --- | --- |
| 遮挡 | z-order + α | **几何独占**（重叠 = 非流形）|
| 颜色 | RGBA | **每 sub-mesh 一种 filament**，无半透 |
| 优先级 | 顶层覆盖 | **物理高度堆叠** |
| 细节 | 像素级 | **≥ 51m**（0.4mm 喷嘴 ÷ scale） |
| 错误 | 视觉糊 | 打印失败 / 切片报错 |

**核心策略**：

1. 几何层面用 shapely 做 5 步布尔减法，**确保任意两 sub-mesh 在 polygon 层不重叠**
2. 按物理高度堆叠（z 替代 z-order）
3. 单层内部用 manifold3d.batch_boolean(Add) 合成 watertight solid
4. 每 sub-mesh 通过 EXTRUDER_MAP 分配 filament 槽位
5. 精度过滤：area < (51m × 1.5)² ≈ 4000m² 的 polygon 直接舍弃

### A.2 8 类 PNG → 6 sub-mesh 3MF 映射

| 3MF sub-mesh | 含 PNG 类 | Extruder | Filament | Z 高度 (mm，相对地形顶面) |
| --- | --- | --- | --- | --- |
| `terrain` | 地形 + 底盖 | E1 | 灰 #9A9A9A | base |
| `buildings` | BO（block_fill 街区填充）| E1 | 灰 同 terrain | **+3.0** 平顶 |
| `landmarks` | BL_size + BL_tag | **E5** | 暖砂 #F5E6C8 | **+2.8 ~ +4.0**（OSM 真高压缩）|
| `vegetation` | VL + VO（地标 + 普通） | E4 | 绿 #6B8E23 | **+0.10** (VO) / **+0.15** (VL) |
| `water` | WL + WO（地标水 + 小水）| E3 | 蓝/黑 #000000 | **−2.0** (WL) / **−1.5** (WO) |
| `roads` | RL + RO（普通路 + 桥梁）| E2 | 黑 #0A0A0A | **+0.51** (RO) / **+0.71** (RL 桥) |

**关键约束（必须遵守）**：

- BL 顶 (≥ 2.8) > BO 顶 (= 3.0)：等等矛盾——把 BO 改成 **2.5mm**，让 BL ≥ 2.8 永远高于 BO ✓
- VL 略高 VO（仅 0.05mm），视觉差异微弱但物理可区分
- WL 比 WO 凹更深（−2.0 vs −1.5），命名水更明显
- RL 桥比 RO 路高 0.2mm，物理浮起

### A.3 优先级总规则

```
BL > WL > VL > BO > VO > WO > RO > terrain
```

裁决任何未列冲突时按此优先级"上层减去下层覆盖区域"。

---

## Part B：实施计划（4 阶段）

### Phase 1：建立 polygon 预处理模块（独立可测）

**目标**：`_TEXTURE_STYLE_OF_DEEPSEEK/_layer_preprocess.py` —— 一个新模块，把 raw OSM gdf 转成"已减去高优先级区域 + 已精度过滤"的 6 类 polygon list。

#### B.1.1 模块文件位置

新建文件：`/Users/gangyu.zqw/Desktop/try_ops/map-generator-simple-main/_TEXTURE_STYLE_OF_DEEPSEEK/_layer_preprocess.py`

#### B.1.2 必要导入

```python
from __future__ import annotations
from typing import List, Tuple, Dict, Optional
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import (
    LineString, MultiLineString, MultiPolygon, Polygon, Point,
)
from shapely.ops import unary_union
from shapely.strtree import STRtree

from _TEXTURE_STYLE_OF_DEEPSEEK._landmark import (
    is_tag_landmark, is_vegetation_landmark, is_water_landmark, is_road_landmark,
    compute_top_percent_threshold, compute_hotspot_block_ids,
)
from _TEXTURE_STYLE_OF_DEEPSEEK.config import (
    INTERNAL_SPAN_MM, BUILDING_PRINT_LIMIT_M2, BUILDING_SIMPLIFY_TOL_M,
    BUILDING_V2_USE_LANDMARK_TAGS, BUILDING_V2_LANDMARK_TOP_PERCENT,
    BUILDING_V2_HOTSPOT_RELAX,                    # 新加 config 常量（见 B.1.7）
    BUILDING_V2_BLOCK_FILL_CONVEX,
    BUILDING_V2_MIN_BLOCK_COMPACTNESS,
    BUILDING_V2_MAX_BLOCK_AREA_M2,                 # 新加（见 B.1.7）
    BUILDING_V2_COUNT_THRESHOLD,
    BUILDING_V2_DENSITY_THRESHOLD,
    NOZZLE_DIAM_MM,
    MIN_PRINTABLE_AREA_M2,                          # 新加（见 B.1.7）
    WATERWAY_HALF_WIDTH,                             # 新加（见 B.1.7）
)
```

#### B.1.3 数据结构

```python
@dataclass
class LayerPolygons:
    """6 类 polygon 集合 + 精度元信息"""
    BL: List[Tuple[Polygon, float]]   # (polygon, height_mm) 高度差异化
    BO: List[Polygon]                  # 街区填充，统一 BUILDING_AGGREGATE_HEIGHT_MM
    VL: List[Polygon]                  # 植被地标
    VO: List[Polygon]                  # 普通植被（raw OSM polygon，不做 block_fill）
    WL: List[Polygon]                  # 水体地标
    WO: List[Polygon]                  # 小水体（< landmark 阈值）
    roads_lines: List[Tuple[LineString, str, bool]]  # (line, highway_type, is_bridge)
    nozzle_real_m: float
    min_area_m2: float
```

可以用 dict 也行，关键是**返回结构稳定**。

#### B.1.4 核心函数（按调用顺序）

```python
def preprocess_layers(
    buildings_gdf: gpd.GeoDataFrame,
    roads_gdf: gpd.GeoDataFrame,
    water_gdf: gpd.GeoDataFrame,
    vegetation_gdf: gpd.GeoDataFrame,
    bbox_local: Tuple[float, float, float, float],
    scale: float,
    *,
    enable_hotspot: bool = True,
    hotspot_relax: float = 10.0,        # top X% 热点
) -> LayerPolygons:
    """
    主入口。把 raw OSM gdf 转成 6 类 polygon + roads_lines。

    步骤：
      1. 计算 nozzle_real_m + min_area_m2
      2. 切 city blocks (路网 + 水网 polygonize)
      3. 提取 BL（建筑地标 + 高度），含 hotspot relax
      4. 计算 BO（block_fill 街区，已含地标参与 count/density）
      5. 提取 VL（含 protected_area）
      6. 提取 WL（含 LineString buffer + Tier 4 兜底）
      7. 5 步几何减法（保证独占）
      8. 精度过滤（< min_area_m2 舍弃）
      9. 返回 LayerPolygons
    """
```

每步对应一个 helper 函数：

```python
# B.1.4.1 path: 切 city blocks
def _build_city_blocks(roads_gdf, water_gdf, bbox_local, road_tier=5
                       ) -> List[Polygon]:
    """与 buildings.py:_build_city_blocks 完全相同的逻辑。
    可以直接 import 那个函数复用。"""

# B.1.4.2 path: 提取建筑地标 + 计算高度
def _extract_BL(
    buildings_gdf: gpd.GeoDataFrame,
    city_blocks: List[Polygon],
    enable_hotspot: bool,
    hotspot_relax: float,
) -> Tuple[List[Tuple[Polygon, float]], List[Polygon]]:
    """
    返回 (BL_with_heights, BO_input_smalls)
    - BL_with_heights: [(simplified_poly, height_mm)]
        - 标签命中 OR top X% 面积 OR hotspot 放宽命中 → BL
        - height_mm = _compress_height(OSM真实高度, area_m2) → 2.8-4.0
    - BO_input_smalls: [poly]
        - < print_limit 的小楼（待 block_fill）
    """
    # 对每个 row：
    #   1. simplify
    #   2. 计算 hotspot flag
    #   3. is_tag_landmark(row, area, hotspot=...) OR area >= top_thr → BL
    #   4. else → small
    # 注意：MultiPolygon 拆 sub poly 时把同 row 的 OSM tag 复用

# B.1.4.3 path: 计算 BO
def _compute_BO(
    smalls: List[Polygon],
    city_blocks: List[Polygon],
    BL_polys: List[Polygon],
    nozzle_real_m: float,
) -> Tuple[List[Polygon], set]:
    """
    返回 (BO_polys, filled_block_ids)
    - 用 aggregate_in_blocks 算法（block_fill mode）
    - landmark 参与 count/density 计算（不参与几何）
    - 输出做 _convex_quadrilateral 凸+≥4 顶点
    - 滤掉 area > BUILDING_V2_MAX_BLOCK_AREA_M2 的巨型 block
    """

# B.1.4.4 path: 提取植被
def _extract_VL_VO(
    vegetation_gdf: gpd.GeoDataFrame,
    bbox_local,
) -> Tuple[List[Polygon], List[Polygon]]:
    """
    返回 (VL_polys, VO_polys)
    - VL: is_vegetation_landmark(row, area_m2) 命中
    - VO: 其它所有 raw OSM 植被 polygon
    - 注意：vegetation gdf 应已含 protected_area 数据合并（pipeline.py Stage 2 fetcher）
    """

# B.1.4.5 path: 提取水体
def _extract_WL_WO(
    water_gdf: gpd.GeoDataFrame,
    nozzle_real_m: float,
) -> Tuple[List[Polygon], List[Polygon]]:
    """
    返回 (WL_polys, WO_polys)
    - Polygon/MultiPolygon: is_water_landmark → WL，否则 → WO（仅当 area >= 1000m²）
    - LineString/MultiLineString: is_water_landmark → buffer 到
        max(WATERWAY_HALF_WIDTH[wway], nozzle_real_m * 1.5)
        → 加入 WL（已 buffer 后的 polygon）
    - LineString 非地标 → 忽略（小溪流不要画）
    """

# B.1.4.6 path: 5 步减法 + 精度过滤
def _apply_subtraction_and_filter(
    BL: List[Tuple[Polygon, float]],
    BO: List[Polygon],
    VL: List[Polygon],
    VO: List[Polygon],
    WL: List[Polygon],
    WO: List[Polygon],
    BO_filled_ids: set,
    min_area_m2: float,
) -> Dict:
    """
    应用 5 步减法（与 PNG spec 一致）：
      WO_clean = WO − WL
      BO_clean = BO − all_landmarks − roads_buffered（路也减）
      VO_clean = VO − all_landmarks − BO_clean
      VL_clean = VL − BL
      RO_clean = ...（在 _extract_roads 里做）

    再做精度过滤：
      所有 polygon 过滤 area < min_area_m2 的
    """

# B.1.4.7 path: 提取道路（含桥梁分类）
def _extract_roads(
    roads_gdf: gpd.GeoDataFrame,
    BL_polys: List[Polygon],
) -> List[Tuple[LineString, str, bool]]:
    """
    返回 [(line, highway_type, is_bridge)]
    - 应用 ROAD_FILTER（如 large 城市仅取 motorway/trunk/primary/secondary）
    - 减去 BL footprint（路从大楼底下不画）
    - is_bridge = is_road_landmark(row)
    """
```

#### B.1.5 _subtract helper

```python
def _subtract(polys: List[Polygon], minus_geom) -> List[Polygon]:
    """从 polys 中扣掉 minus_geom，返回 polygon list。
    minus_geom 可为 None / Polygon / MultiPolygon / GeometryCollection。
    输出已展平 MultiPolygon。
    """
    if minus_geom is None or minus_geom.is_empty:
        return list(polys)
    out = []
    for p in polys:
        if not isinstance(p, Polygon) or p.is_empty: continue
        if not p.intersects(minus_geom):
            out.append(p); continue
        diff = p.difference(minus_geom)
        if diff.is_empty: continue
        if isinstance(diff, Polygon):
            out.append(diff)
        elif hasattr(diff, "geoms"):
            for g in diff.geoms:
                if isinstance(g, Polygon) and not g.is_empty:
                    out.append(g)
    return out
```

#### B.1.6 验证（Phase 1 完成标志）

新建测试 `tests/test_layer_preprocess.py`：

```python
def test_preprocess_returns_disjoint_polygons():
    """5 步减法后任意两 layer 的 polygon 不重叠。"""
    polys = preprocess_layers(...)
    BL_geom = unary_union([p for p, _ in polys.BL])
    BO_geom = unary_union(polys.BO)
    VL_geom = unary_union(polys.VL)
    WL_geom = unary_union(polys.WL)

    # BO 不应与任何地标重叠
    assert BO_geom.intersection(BL_geom).area < 1.0    # < 1m² 容差
    assert BO_geom.intersection(VL_geom).area < 1.0
    assert BO_geom.intersection(WL_geom).area < 1.0
    # VL 不应与 BL 重叠
    assert VL_geom.intersection(BL_geom).area < 1.0


def test_preprocess_no_polygons_below_min_area():
    """精度过滤后无小于 4000m² 的 polygon。"""
    polys = preprocess_layers(...)
    nozzle = polys.nozzle_real_m
    min_area = (nozzle * 1.2) ** 2
    for p, _ in polys.BL:    assert p.area >= 1000   # BL 用 print_limit 阈值
    for p in polys.BO:        assert p.area >= min_area * 0.5   # BO 较宽松
    for p in polys.VL:        assert p.area >= min_area * 0.5
    for p in polys.WL:        assert p.area >= min_area * 0.5


def test_preprocess_landmark_recovery():
    """标志性地标必须保留：雷峰塔 / 灵隐寺 / 西湖 / 钱塘江"""
    polys = preprocess_layers(...)
    # ... 通过 OSM tag 反查
```

测试命令：
```bash
venv/bin/python -m pytest tests/test_layer_preprocess.py -v
```

#### B.1.7 config.py 新增常量（已存在的不重复）

```python
# 精度
NOZZLE_DIAM_MM = 0.4                            # 已存在
MIN_PRINTABLE_AREA_M2 = 4000.0                   # 新加，0.4mm × 1.5 nozzle 余量

# Building hotspot
BUILDING_V2_HOTSPOT_RELAX = 10.0                 # 新加，top X% 热点

# Block 大小过滤
BUILDING_V2_MAX_BLOCK_AREA_M2 = 500000.0         # 新加

# WATERWAY buffer
WATERWAY_HALF_WIDTH = {                          # 新加
    "river": 90.0,        # 钱塘江、长江
    "riverbank": 200.0,
    "canal": 25.0,         # 京杭运河
    "stream": 10.0,
    "drain": 6.0,
    "ditch": 4.0,
}
```

---

### Phase 2：改造各 layer builder

每个 layer builder 接收 preprocess_layers 输出的对应 polygon list，extrude 成 mesh。

#### B.2.1 改造 `_TEXTURE_STYLE_OF_DEEPSEEK/buildings.py`

```python
def build_deepseek_buildings_v3(
    BL_with_heights: List[Tuple[Polygon, float]],   # 来自 preprocess
    BO_polys: List[Polygon],                          # 来自 preprocess
    terrain_mesh: trimesh.Trimesh,
    scale: float,
) -> Dict[str, Optional[trimesh.Trimesh]]:
    """
    返回 {"landmarks": Trimesh, "buildings": Trimesh}.
    - landmarks: BL_with_heights 各自高度 extrude
    - buildings: BO 统一 BUILDING_AGGREGATE_HEIGHT_MM=2.5（注意修改）

    这是简化版本：geometry 已在 preprocess 阶段去重，buildings.py 只负责 extrude。
    旧的 _aggregate_in_blocks / _build_city_blocks / is_tag_landmark 调用全部移到
    preprocess_layers，保留 _build_mesh_from_items helper 用于 extrude+union。
    """

# helper 不变，复用现有的：
# def _build_mesh_from_items(items, terrain_mesh, scale, label) -> trimesh.Trimesh
# def _extrude_polygon_manifold(footprint, height_mm, terrain_z, scale) -> manifold
# def _compress_height(est_height_m, area_m2) -> float
```

旧 `build_deepseek_buildings` 函数标 deprecated，保留作向后兼容兜底（无 preprocess 时退回 v1 buffer-union）。

修改 config：

```python
BUILDING_AGGREGATE_HEIGHT_MM = 2.5    # 旧 3.0 → 改 2.5，确保 ≤ BL 最低 2.8
```

#### B.2.2 改造 `_TEXTURE_STYLE_OF_DEEPSEEK/water.py`

```python
def build_deepseek_water_v3(
    WL_polys: List[Polygon],    # 来自 preprocess（含 LineString buffered）
    WO_polys: List[Polygon],
    bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max,
    scale: float,
) -> Optional[trimesh.Trimesh]:
    """
    返回 water sub-mesh。两层凹陷高度：
      WL: -2.0 mm
      WO: -1.5 mm

    base plate (W_BASE_PLATE_MM = 0.5mm) 仍由 obj4_terrain_with_holes 处理。
    本函数只产 water 凸出 mesh（实际是凹下去的 negative 体）。
    """
```

注意：当前 `water.py` 与 `object4_terrain_with_holes.py` 协作复杂——water 本身在 terrain 里挖洞（subtract）。本 spec 不改 obj4 的挖洞逻辑，只在 water sub-mesh 上区分 WL/WO 的不同凹陷深度。

#### B.2.3 改造 `_TEXTURE_STYLE_OF_DEEPSEEK/vegetation_exclusion.py`

```python
def build_deepseek_vegetation_v3(
    VL_polys: List[Polygon],    # 已减去 BL
    VO_polys: List[Polygon],    # 已减去 all_landmarks + BO
    terrain_mesh: trimesh.Trimesh,
    scale: float,
) -> Optional[trimesh.Trimesh]:
    """
    返回 vegetation sub-mesh：
      VL extrude 0.15 mm（地标，略高）
      VO extrude 0.10 mm（普通）

    高度差异 0.05 mm = 0.4mm 喷嘴的 1/8——视觉接近平面但物理可区分。
    """
```

#### B.2.4 改造 `_TEXTURE_STYLE_OF_DEEPSEEK/roads.py`

```python
def build_deepseek_roads_v3(
    roads_lines: List[Tuple[LineString, str, bool]],
    terrain_mesh: trimesh.Trimesh,
    scale: float,
) -> Optional[trimesh.Trimesh]:
    """
    返回 roads sub-mesh：
      普通路 (RO): 0.51 mm
      桥梁段 (RL, is_bridge=True): 0.71 mm（高 0.20mm 浮起）

    宽度仍按 ROAD_WIDTHS × ROAD_WIDTH_MULTIPLIER。
    精度兜底由 slicer 处理（不在我们这里 buffer）。
    """
```

#### B.2.5 验证（Phase 2 完成标志）

每个 layer builder 单独 unit test：

```python
def test_buildings_v3_separates_landmark_and_ambient():
    """两个 sub-mesh 都 watertight，face 数合理"""
    result = build_deepseek_buildings_v3(...)
    assert result["landmarks"].is_watertight
    assert result["buildings"].is_watertight
    assert len(result["landmarks"].faces) > 0
    assert len(result["buildings"].faces) > 0


def test_buildings_v3_height_separation():
    """BL 顶 ≥ 2.8mm，BO 顶 ≤ 2.5mm，永远不重叠"""
    bl = result["landmarks"]
    bo = result["buildings"]
    assert bl.bounds[5] - bl.bounds[2] >= 2.8 - 0.1   # tolerance
    assert bo.bounds[5] - bo.bounds[2] <= 2.5 + 0.1
```

测试命令：
```bash
venv/bin/python -m pytest tests/test_layer_builders.py -v
```

---

### Phase 3：pipeline.py / generate_westlake_cli.py 集成

#### B.3.1 改造 `pipeline.py`

新增 Stage 4.5：在 Stage 4（terrain）之后、Stage 5（buildings）之前：

```python
# Stage 4.5: 5 步预处理（geometry 减法 + 精度过滤）
print("\n[Stage 4.5] Preprocessing layers (subtraction + precision filter)...")
t45 = time.time()
from _TEXTURE_STYLE_OF_DEEPSEEK._layer_preprocess import preprocess_layers
bbox_local = (utm_bbox[0]-origin[0], utm_bbox[1]-origin[1],
               utm_bbox[2]-origin[0], utm_bbox[3]-origin[1])
layers = preprocess_layers(
    buildings_gdf=buildings_gdf,
    roads_gdf=roads_gdf,
    water_gdf=water_gdf,
    vegetation_gdf=vegetation_gdf,
    bbox_local=bbox_local,
    scale=scale,
    enable_hotspot=True,
    hotspot_relax=BUILDING_V2_HOTSPOT_RELAX,
)
print(f"  BL={len(layers.BL)} BO={len(layers.BO)} VL={len(layers.VL)} "
      f"VO={len(layers.VO)} WL={len(layers.WL)} WO={len(layers.WO)} "
      f"roads={len(layers.roads_lines)}")
print(f"  Time: {time.time() - t45:.1f}s")
```

替换 Stage 5/6/7/8 调用（用 v3 builder 接 layers 直接）：

```python
# Stage 5: Buildings
b_result = build_deepseek_buildings_v3(layers.BL, layers.BO, terrain_solid, scale)
landmarks_mesh = b_result["landmarks"]
buildings_mesh = b_result["buildings"]

# Stage 6: Roads
roads_mesh = build_deepseek_roads_v3(layers.roads_lines, terrain_solid, scale)

# Stage 7: Water
water_mesh = build_deepseek_water_v3(
    layers.WL, layers.WO,
    bbox_x_min, bbox_y_min, bbox_x_max, bbox_y_max, scale)

# Stage 8: Vegetation
vegetation_mesh = build_deepseek_vegetation_v3(
    layers.VL, layers.VO, terrain_solid, scale)
```

#### B.3.2 generate_westlake_cli.py 同步

CLI 用同一套 v3 builders。改动跟 pipeline.py 平行。

#### B.3.3 验证（Phase 3 完成标志）

```bash
venv/bin/python generate_westlake_cli.py
```

期望：
- 跑完无错误
- 输出文件大小 < 50 MB
- 各 sub-mesh face 数：terrain ~300k / buildings ~100k / landmarks ~50k / water ~5k / vegetation ~10k / roads ~50k
- 总耗时 < 15 分钟

---

### Phase 4：Bambu Studio 加载验证 + 视觉一致性

#### B.4.1 加载测试

```bash
# 用 Bambu Studio 打开 output/.../westlake_cli_deepseek.3mf
# 确认：
#   - 无 "对象体积为零" 错误
#   - 无 "非流形" 警告
#   - 6 个 sub-mesh 全部显示
```

#### B.4.2 视觉一致性

跟 PNG 对比（手动）：
- 西湖凹陷可见
- 钱塘江凹陷可见
- 雷峰塔 / 灵隐寺 / 浙博等地标作为暖砂石高 bumps
- 街区填充与地标 + 地标周围有"城市文脉"
- 西溪国家湿地公园作为 vegetation 大块

#### B.4.3 切片测试

```
Bambu Studio:
  - 加载 3MF
  - "切片" 检查路径合理
  - 总打印时间预估
  - 滤芯切换次数
```

---

## Part C：Edge Cases 详细处理

### C.1 8×8 干扰矩阵在 3D 的表现

每对组合 + 3D 处理：

| # | 场景 | 几何处理 | 高度处理 | 视觉效果 |
| -- | --- | --- | --- | --- |
| 1 | BL 在 BO 内 | BO − BL（preprocess B.1.4.6 第 2 步）| BL 2.8-4.0 高 / BO 2.5 平 | BL 顶突出 BO 顶 0.3-1.5mm |
| 2 | BL 在 VL 内（灵隐寺）| VL − BL | BL 2.8 / VL 0.15（薄铺面）| BL 完全凸出 VL，VL 像草坪 |
| 3 | BL 在 VO 内 | VO − BL | 同 #2 | 同 #2 |
| 6 | BO 跨 VL（西湖风景区）| BO − VL | BO 2.5 / VL 0.15 | VL 薄绿底，BO 在外侧凸 |
| 7 | BO + VO 同 block | 建筑优先（preprocess B.1.4.6 第 3 步） | BO 2.5 / VO 0 | 该 block 给建筑，VO 让位 |
| 11 | 西湖风景区(VL)含西湖(WL) | 不减（VL 0.15 高，WL -2.0 凹）| VL 抬起 / WL 下沉 | VL 是绿底，WL 是蓝凹槽，自然分离 |
| **bridge** | 钱塘江大桥 | road LineString 标 is_bridge=True | RL 0.71 / WL -2.0 | 桥从 0 起到 +0.71，水从 0 凹到 -2.0；桥跨水面 |
| 14 | VO 跨 WO | VO − WO | 都很矮 | 不重要（视觉权重小）|

### C.2 MultiPolygon 处理

5 步减法可能产生 MultiPolygon：

```python
# 例：BO − BL，如果 BL 在 BO 中间，BO_clean 变 ring（中间空 hole）
# extrude 时：
shapely_poly = ...   # MultiPolygon
for p in shapely_poly.geoms:
    extrude(p, ...)
# 结果是多个独立 manifold，再 batch_boolean(Add) 合并
```

#### B.4.4 性能预算

| 步骤 | 预算 | 实测（杭州 25km）|
| --- | --- | --- |
| preprocess (5 步减法) | < 30s | TBD |
| Build buildings | < 60s | 当前 ~50s |
| Build roads | < 30s | 当前 ~20s |
| Build water | < 20s | 当前 ~15s |
| Build vegetation | < 30s | 当前 ~20s |
| Export 3MF | < 30s | 当前 ~10s |
| **总计** | **< 200s** (3 分钟) | TBD |

如果 preprocess 超 30s，需优化（重用 STRtree、避免重复计算）。

---

## Part D：测试矩阵

### D.1 必须通过的 unit test

```bash
venv/bin/python -m pytest tests/test_layer_preprocess.py -v
venv/bin/python -m pytest tests/test_layer_builders.py -v
```

### D.2 端到端集成测试

```bash
# 西湖
venv/bin/python generate_westlake_cli.py
# 期望：成功生成 output/westlake_cli/.../westlake_cli_deepseek.3mf，< 50MB

# 重庆（验证非杭州 bbox 也能跑）
venv/bin/python generate_chongqing_cli.py
# 期望：同上
```

### D.3 视觉验证（手动）

PNG 输出（用 tune_buildings_v2 参考，纯视觉对照）：
```bash
venv/bin/python tools/tune_buildings_v2.py --city westlake \
  --road-tier 5 --print-limit 1000000 --simplify 60 \
  --mode block_fill --use-water --count-thr 1 --density-thr 0.005 \
  --min-compactness 0.0 --use-landmarks --landmark-top-percent 1.0 \
  --max-block-area 500000 --hotspot-relax 10 \
  --annotate --with-water-landmarks
```

3MF 输出（Bambu Studio 打开）：
- 西湖凹陷、钱塘江凹陷
- 暖砂石的 BL（雷峰塔 / 灵隐寺）凸起
- 街区灰色 BO 平整在地标周围

切片成功 + 估时 < 30 hours。

---

## Part E：失败 / 回退策略

### E.1 单 sub-mesh 失败

如果某 layer 失败（如 vegetation 数据为空），返回 None，pipeline 跳过该 sub-mesh。3MF 仍能生成（缺失 layer）。

### E.2 整体失败

```
若 preprocess_layers 抛错：
  → log + fallback 到旧 build_deepseek_buildings (v1 buffer-union)
  → 3MF 仍能跑通，但失去几何独占性

若 batch_boolean 失败：
  → 单 part 输出（无 union），多 sub-mesh
  → Bambu Studio 兼容
```

### E.3 Bambu Studio 报错

| 错误 | 原因 | 修复 |
| --- | --- | --- |
| 对象体积为零 | sub-mesh 太小被打回 | 检查 face count > 0；几何 simplify 太狠 |
| 非流形 | extrude 后未 batch_boolean Add | 走完整 manifold 流程 |
| 颜色丢失 | EXTRUDER_MAP 未生效 | 检查 _SUB_MESH_DEFS 的 oid / extruder 映射 |

---

## Part F：审批节点

请确认：

1. **Phase 1 / 2 / 3 / 4 拆分** OK 吗？（一次性 vs 分阶段）
2. **`_layer_preprocess.py` 作为新模块** OK？还是要嵌入 buildings.py？
3. **6 个 sub-mesh / 5 个 extruder** OK？
4. **BUILDING_AGGREGATE_HEIGHT_MM 从 3.0 改 2.5** OK？（保持 BL ≥ BO 永真）
5. **VL 比 VO 高 0.05mm** OK？还是想完全平面？
6. **桥梁高 0.20mm** OK？还是更夸张（0.4mm = 1 nozzle）？
7. **道路精度方案 b**（保留 ROAD_FILTER + slicer 兜底）OK？
8. **MIN_PRINTABLE_AREA_M2 = 4000m²** OK？（可调）
9. **VO 仍 raw 渲染**（不做 block_fill）OK？

OK 后开干。这份 spec 给执行模型用，每段都精确到函数 / 参数 / 测试命令。

---

## Part G：实施 checklist（执行模型用）

### Phase 1: preprocess
- [ ] 新建 `_TEXTURE_STYLE_OF_DEEPSEEK/_layer_preprocess.py`
- [ ] 加 config 常量（B.1.7）
- [ ] 实现 `LayerPolygons` dataclass
- [ ] 实现 7 个 helper 函数（B.1.4.x）
- [ ] 实现 `preprocess_layers` 主入口
- [ ] 写 `tests/test_layer_preprocess.py`
- [ ] 单测全过

### Phase 2: layer builders
- [ ] 改 `buildings.py`：加 `build_deepseek_buildings_v3`，旧 `build_deepseek_buildings` 加 `@deprecated`
- [ ] 改 `water.py`：加 `build_deepseek_water_v3`
- [ ] 改 `vegetation_exclusion.py`：加 `build_deepseek_vegetation_v3`
- [ ] 改 `roads.py`：加 `build_deepseek_roads_v3`
- [ ] 改 `config.py`：BUILDING_AGGREGATE_HEIGHT_MM = 2.5
- [ ] 写 `tests/test_layer_builders.py`
- [ ] 单测全过

### Phase 3: pipeline 集成
- [ ] 改 `pipeline.py`：加 Stage 4.5，替换 Stage 5/6/7/8 用 v3
- [ ] 改 `generate_westlake_cli.py`：同步
- [ ] 改 `generate_chongqing_cli.py`：同步
- [ ] 端到端跑通 westlake + chongqing

### Phase 4: 验证
- [ ] Bambu Studio 加载无错
- [ ] 视觉对比 PNG OK
- [ ] 切片成功 + 时间合理

### 改动文件清单 final

```
新建：
  _TEXTURE_STYLE_OF_DEEPSEEK/_layer_preprocess.py    (~250 行)
  tests/test_layer_preprocess.py                      (~150 行)
  tests/test_layer_builders.py                        (~100 行)

改：
  _TEXTURE_STYLE_OF_DEEPSEEK/buildings.py             (~50 行)
  _TEXTURE_STYLE_OF_DEEPSEEK/water.py                  (~50 行)
  _TEXTURE_STYLE_OF_DEEPSEEK/vegetation_exclusion.py  (~50 行)
  _TEXTURE_STYLE_OF_DEEPSEEK/roads.py                  (~30 行)
  _TEXTURE_STYLE_OF_DEEPSEEK/pipeline.py               (~60 行)
  _TEXTURE_STYLE_OF_DEEPSEEK/config.py                 (~15 行)
  generate_westlake_cli.py                              (~30 行)
  generate_chongqing_cli.py                             (~30 行)
```

总：~815 行。
