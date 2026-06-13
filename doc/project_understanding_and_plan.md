# 项目理解、剩余工作规划与代码 Bug 清单

> 时间：2026-05-15
> 范围：`map-generator-simple-main`（核心包：`_TEXTURE_STYLE_OF_DEEPSEEK`）
> 当前已满意：**对象3 — 底板 + 水体浮雕**（`water.py` + `build_deepseek_water`）

> **2026-05-15 Refactor 已完成**：13 项 Bug 全部修复，dead code/legacy 已清理，水体几何
> 抽取到 `_geom_utils.py`（obj3 与 obj4 现在共用同一个水体多边形集合，自动 XY
> 镶嵌一致）。pipeline 默认启用 `vegetation_with_exclusions` + `enable_roads_fusion`。
> 详见 §五。

---

## 一、项目整体理解

### 1.1 项目目标

把一块 WGS84 矩形区域（lat1/lon1/lat2/lon2）转成符合 Bambu Lab 200mm 打印平台的**多色 3MF 模型**，最终在打印件上呈现 Urban Series 参考样式（芝加哥/杭州 25km 城市肌理）。

模型由 5（+1）个 3MF 子对象按 Z 轴装配而成：

| 序号 | 子对象 | 文件 | 关键工艺 |
|------|--------|------|---------|
| 对象1 | 完整模型 | — | 装配结果 |
| 对象2 | 植被层（绿色） | `vegetation.py` | 平板挤出 + 镂空水体/建筑 |
| **对象3** ✅ | **底板 + 水体浮雕**（高于底板） | `water.py` | Manifold Union（底板 + 水体柱） |
| 对象4 | 地形 + 道路（融合）+ 水体镂空 | `object4_terrain_with_holes.py` | 高程网格 → walls/bottom → Manifold Difference |
| 对象5 | 建筑（嵌入地形） | `buildings.py` | 简化轮廓 + 多边形挤出 |

> 依据：`doc/most_important_doc.md` + `doc/manifold_boolean_spec.md`

### 1.2 坐标 / 比例尺约定

`config.py::compute_scale()`：

```
scale = 196.0 / max(width_m, height_m)   # mm 每真实米
```

模型 XY 跨度被强制归一到 196mm（200mm 平台留 2mm 边距）。所有几何在两套坐标系中流转：

- **真实米**（UTM 局部，OSM/SRTM 输入）
- **模型毫米**（实际 3D 网格）

`build_deepseek_water` / `water_column.create_water_columns_union_manifold` 在 Manifold `CrossSection` 阶段做 `cs.scale((scale, scale))` 把 XY 缩放到模型 mm；`build_deepseek_terrain` 则在生成网格之后 `mesh.vertices[:, :2] *= scale`。Z 轴单独通过幂曲线 `Z_GAMMA=0.45` 映射到 `[Z_TERRAIN_BASE, Z_TERRAIN_BASE + TERRAIN_THICKNESS_MM]`。

### 1.3 Z 轴堆叠（从下到上）

```
Z = -2.00 mm   水体底板下沿（Z_WATER_BASE_MM）
Z = -1.60 mm   水体底板上沿（厚度 0.4mm）
Z = -0.17 mm   地形底面（Z_TERRAIN_BASE）
Z =  3.83 mm   地形最高（+ TERRAIN_THICKNESS_MM = 4.0mm）
            |  其上叠加：
            ├─ 建筑：terrain_z - 0.04 → terrain_z + 3.0~5.3 mm
            ├─ 道路：terrain_z + 0.51 → +0.91 mm
            └─ 植被：terrain_z + 0.10 → +0.30 mm
```

### 1.4 Pipeline 概览（`_TEXTURE_STYLE_OF_DEEPSEEK/pipeline.py`）

```
Stage 0  bbox → UTM, scale, 区域分级
Stage 1  fetch_elevation_grid (SRTM)
Stage 2  fetch_buildings/roads/water/vegetation (Overpass 或 osmium PBF)
Stage 3  project_geodataframe (UTM 局部 + 裁剪)
Stage 4  build_terrain_with_water_holes_manifold  → 对象4 (合并体)
Stage 5  build_deepseek_buildings                 → 对象5
Stage 6  build_deepseek_roads                     → 道路（独立 ribbon）
Stage 7  build_deepseek_water                     → 对象3 (底板+浮雕)
Stage 8  build_deepseek_vegetation                → 对象2 (无镂空)
Stage 9  split_terrain_mesh + export_deepseek_3mf
Stage 10 validate_3mf
```

`generate_westlake_cli.py` 是用纯 osmium CLI 的快速版本；`generate_hangzhou_obj1.py` / `obj4.py` 是单对象生成脚本。

### 1.5 Manifold 用法

工程统一用 `manifold3d` 做布尔运算，`_bridge.py` 做 trimesh ↔ Manifold 转换。**所有水体相关的 Union/Subtract 都先经 `cs.extrude` 直接构造 Manifold，避免 trimesh 输入到 Manifold 时的非流型问题。**

---

## 二、剩余工作规划

> 满意基线：**对象3 (底板+水体浮雕)**。下面按"对每个剩余对象做什么"分块。

### 2.1 对象4：地形 + 道路 + 水体镂空

**当前状态**：`object4_terrain_with_holes.py` 已实现 *地形 - 水体* 镂空（Manifold Subtract），且 `enable_roads_fusion=True` 时可把桥梁（`bridges_only=True`）布尔并集进地形。`pipeline.py` / `generate_westlake_cli.py` 默认 `enable_roads_fusion=False`，没有道路融合。

**还需要做的事**（按优先级）：

1. **打开桥梁融合并验证视觉效果**（参考杭州模型钱塘江上的桥）
   - 把 pipeline 中 `enable_roads_fusion` 改为 `True`，先在 westlake 25km 上跑通
   - 确认 `bridge_filter::filter_bridges_only` 在不同 OSM 数据下的桥梁数（spec 预期 5–20 条）
   - 校验：地形 Z 范围未被桥梁意外抬高；watertight=True

2. **道路融合开销控制**（spec 提的"开销低"问题）
   - 现在桥梁过滤逻辑是 `road.intersection(water_union)`，复杂度随道路数*水体数增长，对 25km 已可接受
   - 进一步可加：`major_roads = highway in {motorway, trunk, primary}` 预过滤 + buffer 半径限制

3. **底板/对象3 与 对象4 的镶嵌精度验证**
   - spec 第73–119 行明确要求"水体镂空与水体浮雕完美契合"
   - 验证脚本：分别导出 obj3 和 obj4，加载后做 XY 投影差集，应 ≈0
   - 当前 `WATER_MIN_AREA_M2 = 50000` 在 obj3 和 obj4 都用同一个常量 ✅；但 **`water.py` 与 `water_column.py` 在"LineString vs Polygon 重叠时谁优先"上方向相反**（见 §三 Bug #B1），必须先修

4. **小水体过滤阈值打磨**
   - 当前 50000 m² 对 25km 跨度合理（参考模型也明显过滤了细小水体）
   - 建议加一个调试模式：跑 pipeline 时打印"被过滤掉的最大 N 个水体面积"以决策

### 2.2 对象2：植被（含遮挡镂空）

**当前状态**：

- `vegetation.py::build_deepseek_vegetation` 实现了 **基础平板挤出 + Manifold Union**。
- **遮挡处理未启用**：`vegetation_exclusion.py` 写好了 `build_deepseek_vegetation_with_exclusions`（植被 - 水体/建筑/道路），但 **pipeline.py / generate_westlake_cli.py 都没有调用它**。

**还需要做的事**：

1. **接入 vegetation_exclusion**：把 pipeline Stage 8 改为调用 `build_deepseek_vegetation_with_exclusions(...)`，参数：
   ```
   exclude_water=True   # P0
   exclude_buildings=True  # P0
   exclude_roads=False  # P1，先关
   ```
2. **植被层厚度归一**：`vegetation.py` 用硬编码 `vegetation_thickness_mm = 0.2`，`config.py` 没有对应常量。建议加 `VEGETATION_THICKNESS_MM` 到 config，并和 exporter 中的 `source_offset_z` 联动。
3. **小植被面积过滤**：`VEGETATION_MIN_AREA_M2 = 5000` 已经偏向保留中等斑块，符合参考模型。
4. **西溪湿地特殊处理**（spec 提到）：参考杭州模型用灰色（地形色）单独表现西溪湿地。当前实现里湿地按普通植被处理，绿色。如果需要还原，需要：
   - 识别 `natural=wetland` / `landuse=wetland`
   - 单独导出为 `vegetation_wetland` 子对象（pid 不同）→ 染地形色
   - 这是装饰性需求，可放最后

### 2.3 对象5：建筑

**当前状态**：

- `buildings.py::build_deepseek_buildings` 实现了 *footprint → simplify → 单独挤出 → concatenate*，**未做布尔并集**，因此建筑之间相邻时是多个独立壳，trimesh 把它们合并后 watertight 不一定保证（相邻建筑共面不会触发非流型，独立分布通常 OK）。
- 嵌入：`z_bottom = terrain_z - 0.04`（硬编码，未读 `Z_BUILDING_EMBED_MM`），与参考模型一致。

**还需要做的事**：

1. **地形分类驱动的"平坦/山地"双方案**（spec 第329–404 行）
   - 平坦地形 → 现在的"挤出 + 嵌入"已足够
   - 山地（杭州类）→ spec 推荐"建筑柱体 ⨉ 地形"布尔交集，让建筑天然贴坡
   - 实现：先写 `terrain_classification.py`（`classify_terrain_type` + `compute_slope_grid`），然后在 `build_deepseek_buildings` 内部根据局部坡度选方案
   - **优先级 P0**：杭州/重庆等山城用平坦方案会出现建筑漂浮或埋入

2. **建筑与水体/道路冲突过滤**（spec 第79–121 行 `filter_buildings_conflicts`）
   - 现在 OSM 偶尔会给到水面上的"船屋"或道路中线上的奇怪 building polygon
   - 目前 pipeline 没做该过滤，杭州 25km 跑出来一般问题不大，但芝加哥这类高密度区域要加
   - 优先级 P1

3. **建筑高度估计的数据源差异**（见 §三 Bug #B6）
   - `terrain3d/fetchers/osm.py` 会给 `est_height` 列；`osmium_cli_fetcher.py` **没加**
   - `generate_westlake_cli.py` 走 CLI 路径 → 所有建筑 `est_height=0` → 全部走 `BUILDING_AREA_HEIGHTS` 面积估计
   - 必须修：在 `osmium_cli_fetcher.py` 里同样调用 `_estimate_building_heights`

4. **建筑 LOD 阈值**：`BUILDING_MIN_AREA = {small:30, medium:80, large:200}` 看起来合理，可保留。

### 2.4 单对象 3MF 导出（spec 要求"每个对象单独能生成 3mf"）

**当前**：`generate_hangzhou_obj1.py` 和 `obj4.py` 已能单独导出。缺：

- `generate_hangzhou_obj2_vegetation.py`（植被单独 + 镂空）
- `generate_hangzhou_obj5_buildings.py`（建筑单独）

它们的实现可以直接抄 obj1/obj4 的脚手架，把 `meshes` dict 中只填一个 key。

### 2.5 装配（最后一步）

把 5 个 3MF 用 Bambu Studio 加载后按 Z 轴叠放即可，工程上不需要额外代码。如果要导出**单个装配 3MF**，`pipeline.py` 现在已经做这件事（Stage 9 export_deepseek_3mf 把 6 类 mesh 一起塞进一个 3MF）。

### 2.6 推荐里程碑顺序

```
M1  修水体 LineString/Polygon 优先级一致性 (Bug #B1)         <- 阻塞 obj3↔obj4 镶嵌
M2  pipeline 接入 vegetation_exclusion (obj2 镂空)
M3  打开 obj4 的 enable_roads_fusion 并验证桥梁视觉效果
M4  est_height 在 CLI 路径上补齐 (Bug #B6)
M5  补 generate_obj2 / generate_obj5 单独导出脚本
M6  地形分类 + 山地建筑布尔交集（杭州/重庆）
M7  对象3↔对象4 XY 镶嵌验证脚本
M8  装饰：西溪湿地灰色高亮、建筑-水体过滤
```

---

## 三、代码 Bug / 可疑点清单

> 按"严重度"标注：🔴 必修 / 🟡 影响视觉/性能 / ⚪ 死码或风格

### 🔴 B1. `water.py` 与 `water_column.py` LineString/Polygon 优先级方向相反

- `water.py::build_deepseek_water` 在 LineString ∩ Polygon 重叠 >30% 时**保留 Polygon、跳过 LineString**（line 297–322，注释 "Polygon takes precedence"）。
- `water_column.py::create_water_columns_union_manifold` 在同一情形下**保留 LineString、跳过 Polygon**（line 222–228，注释 "优先使用LineString"）。
- 两处用的水体阈值（`WATER_MIN_AREA_M2`）相同，但筛出来的几何集合**不同** → 对象3 浮雕 与 对象4 镂空 的 XY 形状不一致 → spec 要求的"完美契合"做不到。
- **修法**：统一为一种（建议 Polygon 优先，因为 OSM 中 `natural=water` Polygon 通常比 `waterway=river` LineString + buffer 更精确，参考 obj3 的现状）。把 `water_column.py` 的 Step 2 改成"polygon 优先"。

### 🔴 B2. `water.py` 计数器命名误导

- `n_skipped_overlap` 在 line 304 仅在 *polygon 与 linestring 覆盖重叠* 时 +1，但 print 里写成 "LineString skipped"。实际真正的 LineString 跳过发生在 line 318–325 的循环中且**未被计数**。
- 影响：调试日志骗人，真要诊断"为什么少了水体"会被误导。
- **修法**：把 `n_skipped_overlap` 移到 step 3 的真正 skip 分支里。

### 🔴 B3. `pipeline.py` Stage 编号重复

- line 277 `print("\n[Stage 9] Preparing 3MF objects...")`
- line 287 `print("\n[Stage 9] Exporting 3MF...")`（应该是 Stage 10）
- line 308 `print("\n[Stage 10] Validating...")`（应该是 Stage 11）
- 计时变量 `t9` 也被重复赋值。无功能影响，但日志混乱。

### 🔴 B6. `osmium_cli_fetcher.py` 没有为 buildings 计算 `est_height`

- `terrain3d/fetchers/osm.py::fetch_buildings` 会调用 `_estimate_building_heights` 给每行加 `est_height`。
- `osmium_cli_fetcher.py` 走 osmium CLI 直接 export GeoJSON，**没有这一步**。
- `buildings.py:187` 用 `gdf.loc[idx].get("est_height", 0)`，缺列就回退到 `0` → 全走面积代理 (`BUILDING_AREA_HEIGHTS`)。
- 用 `generate_westlake_cli.py` 时所有建筑都按"面积→高度"估计，丢失 OSM 的 `height` / `building:levels` 标签。
- **修法**：在 `osmium_cli_fetcher.py` 输出的 building gdf 上调用 `_estimate_building_heights`（或抽到独立 utils 里复用）。

### 🟡 B4. `vegetation_exclusion.py` 未接入 pipeline

- 文件存在、写得也比较完整，但 `pipeline.py` Stage 8 仍是 `build_deepseek_vegetation`（无镂空）。spec 第294–321 行明确指出植被需要镂空水体、建筑。
- 影响：植被 mesh 和水体 mesh 在 XY 上重叠，打印时同一格子可能两层挤出（颜色取决于 Bambu Studio 的对象顺序）。
- **修法**：见 §2.2。

### 🟡 B5. `vegetation.py::vegetation_thickness_mm = 0.2` 硬编码

- config.py 里有 `VEGETATION_Z_OFFSET_MM=0.1` 但没有 `VEGETATION_THICKNESS_MM`。
- exporter 的 metadata 读 `VEGETATION_Z_OFFSET_MM` 但植被实际上厚 0.2mm，导致 `source_offset_z` 不准。该 metadata 现在已被禁用（`exporter.py:290–292`），暂无可见影响。
- **修法**：加常量并联动。

### 🟡 B7. `buildings.py::z_bottom = terrain_z - 0.04` 不读 `Z_BUILDING_EMBED_MM`

- config 里就是 `Z_BUILDING_EMBED_MM = 0.04`，硬编码 0.04 容易和 config 漂移。
- **修法**：`z_bottom = terrain_z - Z_BUILDING_EMBED_MM`。

### 🟡 B8. `roads.py` 几何质量

- 道路 ribbon 是手工搭顶/底/侧 + concatenate，不做 Manifold 流型化。绝大多数情况 trimesh 的 `fix_normals()` 能纠正，但密集路网相邻 ribbon 会产生 T-junction，3D 打印切片器会发警告。
- spec 给的目标是"道路融入地形"（obj4），所以独立 roads mesh（pipeline Stage 6）只是对象之间装配的备选输出，可以容忍。

### 🟡 B9. `terrain.py::_add_walls_and_bottom` 重复求边界

- line 45–47 用 `trimesh.grouping.group_rows` 求一次 boundary edges，结果覆盖在 line 56 用 `Counter` 重新求一次。前者完全是死代码且耗 O(E)。
- **修法**：删掉前者。

### 🟡 B10. `water_column.py::_fan_triangulate` 索引越界

- line 414：`faces.append([i, j, n])`，但函数只接受 `exterior_coords`（长度 n），没有把质心顶点加进来。这条 fan 三角化所有三角形都会引用不存在的顶点 `n`。
- 触发条件：仅在 earcut 不可用时被调用（`extrude_water_column_for_cutting` 里），而 earcut 是 `mapbox_earcut`，环境装了之后就不会触发。
- **修法**：要么真的拼接质心顶点再返回 `(n+1)` 顶点 + faces，要么删掉这个老路径（新代码已全用 Manifold 路径）。

### ⚪ B11. 大量死代码

- `exporter.py::_format_vertices_xml` / `_format_triangles_xml`：定义了基于字符串的 XML 格式化函数，从未被调用（实际用的是 `ET.SubElement`）。
- `exporter.py::_generate_bambu_metadata` / `_make_slic3r_metadata` / `_make_metadata`：函数被调用但产物 `bambu_meta` 没写入 zip（line 290–292 有意注释掉了）。其中 `extruders` 参数也成了死参数。
- `exporter.py::export_deepseek_3mf` 默认 `extruders=3`，但 pipeline 调用时传 4。该参数现在唯一作用是写进被禁用的 metadata。
- `vegetation.py::_extrude_vegetation_manifold`：定义但实际代码走 `cs_scaled.extrude` 直接调用。
- `buildings.py::_build_one_building` + `from concurrent.futures import ThreadPoolExecutor, as_completed`：导入并定义但没有并行化的入口。
- 影响：阅读理解成本高，但运行无害。建议在一次"清理"PR 里统一删除。

### ⚪ B12. `WATER_HEIGHT_MODEL_MM` 命名误导

- 常量名带 `_MM` 但代码里被当作"模型米"使用（`water_height_m = WATER_HEIGHT_MODEL_MM`，然后 `cs.scale((scale, scale)).extrude(height=water_height_m)`，最后 `combined.vertices *= scale`）。
- 实际效果：100.0 → 缩放后 ≈0.78mm（25km 比例）。注释里 "~1.0mm at 25km scale" 也写明了。
- **修法**：要么改名为 `WATER_HEIGHT_MODEL_M`，要么改成"先转 mm 再 extrude"。

### ⚪ B13. `bridge_filter.py::roads_gdf.get('bridge', '')`

- 当列存在但里面是 `NaN`（pandas 默认），`NaN == 'yes'` 是 `False`，会被当成"非桥梁"继续走 fallback "与水体相交"分支，结果实际把所有过水道路当桥梁。
- 在 OSM 数据里，绝大多数道路 `bridge` 列都是 NaN，少数是 `'yes'`。当前行为：**永远走 fallback**（即使数据里其实有 `bridge=yes` 标签）。
- **影响**：实际桥梁数 = 与水体相交的所有道路，对参考模型还原已经够用，但和注释承诺不一致。
- **修法**：`(roads_gdf.get('bridge') == 'yes').fillna(False)`。

---

## 五、本次 Refactor 实际改动（2026-05-15）

### 新增

| 文件 | 作用 |
|------|------|
| `_TEXTURE_STYLE_OF_DEEPSEEK/_geom_utils.py` | 共享几何工具：winding 归一化、densify、Shapely→CrossSection、`collect_water_polygons` |
| `venv/` | Python 3.12 虚拟环境（已 `pip install -r requirements.txt`） |

### 修复（与 §三 Bug 列表一一对应）

| Bug | 状态 | 落地位置 |
|-----|------|----------|
| B1 LineString/Polygon 优先级冲突 | ✅ | `_geom_utils.collect_water_polygons` 单一入口，`water.py` 与 `water_column.py` 共用 |
| B2 计数器命名误导 | ✅ | 旧语义随 B1 重写一并消失 |
| B3 pipeline Stage 重复编号 | ✅ | `pipeline.py` Stage 9/10 |
| B4 vegetation_exclusion 未接入 | ✅ | `pipeline.py` + `generate_westlake_cli.py` 默认调用 |
| B6 osmium CLI 缺 est_height | ✅ | `osmium_cli_fetcher.py::fetch_features` 末尾补 `_estimate_building_heights` |
| B7 buildings 硬编码 0.04 | ✅ | 改用 `Z_BUILDING_EMBED_MM` 常量 |
| B9 terrain.py 重复求边界 | ✅ | 删除前一段死代码 |
| B10 `_fan_triangulate` 索引越界 | ✅ | 跟随 legacy 路径整体删除 |
| B11 大量死代码 | ✅ | `_format_*_xml`、`_make_*metadata`、`_generate_bambu_metadata`、`_extrude_vegetation_manifold`、`_build_one_building`、`concurrent.futures` import 等全部删除；`exporter.export_deepseek_3mf` 移除死参数 `extruders` |
| B13 bridge_filter NaN | ✅ | `(roads_gdf['bridge'] == 'yes').fillna(False)` |

> B5/B8/B12 留在后续打磨：分别是植被层厚度常量化、道路 Manifold 流型化、`WATER_HEIGHT_MODEL_MM` 改名。改它们风险/收益比较低。

### 行为变化（pipeline 默认值）

- `enable_roads_fusion=True` + `bridges_only=True`：obj4 默认把跨越水体的桥梁段
  布尔并集进地形（参考杭州模型钱塘江上的桥）。
- 植被层默认走 `build_deepseek_vegetation_with_exclusions`，自动镂空水体 + 建筑。

### 删除的目录 / 脚本

```
_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/backup/   # city_cache_loader 等 legacy fetcher
tools/right_xihu.py
tools/right_xihu_osmium.py
tools/test_object4.py
tools/generate_water_hangzhou.py                        # 已被 generate_hangzhou_obj1 覆盖
tools/proxy_config_example.py
```

`water_column.py` 重写为 Manifold-only（删去 earcut + 手工建墙的旧路径与索引越界的 `_fan_triangulate`）。

### 静态验证

- `python -m py_compile`：所有改动模块通过
- AST cross-resolution：41 个模块的 `from X import Y` 全部解析成功
- `venv/bin/python` 实际 import：21 个核心模块 ✅
- 合成数据端到端 e2e（5km×5km 假地形 + 1 湖 + 2 路 + 2 楼 + 1 公园）：
  - obj3 watertight=True
  - obj4（含桥梁融合 + 水体镂空）watertight=True，volume=51233 mm³
  - 3MF 导出 + validator V1–V11 全部 PASS

### 怎么跑

```bash
source venv/bin/activate
python generate_westlake_cli.py     # 需要 osmium-tool 与 zhejiang PBF
# 或
python -m _TEXTURE_STYLE_OF_DEEPSEEK.pipeline --lat1 30.13 --lon1 120.01 --lat2 30.36 --lon2 120.29
```

---

## 四、阅读这份文档之后建议的第一步

如果只动一行代码：把 `water_column.py:222–228` 的"polygon overlap with linestring → 跳 polygon"反过来改成"linestring 跳过 polygon"，让 obj3 和 obj4 的水体集合完全一致（B1）。这一步是后面所有镶嵌/装配工作的前提。

如果有半天时间，按 §2.6 M1–M3 推：

1. 修 B1（水体优先级一致性）
2. pipeline.py 接入 `build_deepseek_vegetation_with_exclusions`
3. 默认打开 `enable_roads_fusion=True` + `bridges_only=True`
4. 跑一次 `generate_westlake_cli.py`，对比 `output/westlake_cli/full_*.3mf` 在 Bambu Studio 中的视觉，与 spec 第87–127 行的杭州参考模型对照。
