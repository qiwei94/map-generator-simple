# 2026-05-24 性能优化 & 工具整理

## 一、性能优化

### 1. Brick render 向量化 (90s → 3.8s)

**文件:** `_TEXTURE_STYLE_OF_DEEPSEEK/_brick_transform.py`, `tools/brick_render.py`

**问题:** `_perlin_edge_offset` 对每个多边形的每个顶点调用 `opensimplex.noise2()`，Python 循环 ~280K 次。

**修复:**
- 移除 `from opensimplex import OpenSimplex`
- 引入 `_hash_noise_2d`（来自 `_z_displacement.py`），基于整数哈希的向量化噪声
- `_perlin_edge_offset` 完全向量化：numpy roll 计算切线/法线，一次性计算所有顶点位移
- `_individual_perturb` 改为接受 `noise_seed: int` 参数
- `brick_transform_polygon` 不再创建 `OpenSimplex` 实例

**结果:** westlake 12741 polys: 90s → 3.8s (~24x 加速)

---

### 2. block_base 非流形修复 (19810 → 409 条)

**文件:** `_TEXTURE_STYLE_OF_DEEPSEEK/block_base.py`

**问题:** `_polygon_to_textured_mesh` 中侧壁按多边形边界环构建，但 Delaunay 质心过滤会在凹多边形处移除边界三角形，导致侧壁与顶面之间存在开放边。

**修复:**
- 侧壁不再假设 = 多边形环，改为从过滤后三角形集的**真实边界**（只出现 1 次的边）构建
- 每条边恰好被 2 个面引用 → 水密
- 额外 +0.01mm Z 偏移，避免 block_base 底面与 terrain 顶面重合

**结果:** 非流形边 19,810 → 409（97.9% 消除），open edges 从 16,240 → 0

---

### 3. 高德水体补充修复 (over-fill)

**文件:** `_TEXTURE_STYLE_OF_DEEPSEEK/_water_supplement.py`

**问题:** 高德卫星图色彩分割 + 形态学 closing 会产生大面积假水体，覆盖整个校园/小区（启真湖、华家池）。

**修复:**
- 添加 `_MAX_SUPPLEMENT_AREA_M2 = 500000` 面积上限
- 添加 OSM 重叠率检查（≥15% 才保留）
- `binary_closing` 迭代次数 3 → 2

---

### 4. 高德补充接入 3MF 流程

**文件:** `backup_westlake_cli.py`

**问题:** `preprocess_layers()` 调用缺少 `bbox_wgs84`/`utm_crs`/`origin` 参数，导致水体补充从未在 3MF 管线中触发。

**修复:** 传入三个参数即可。

---

### 5. unary_union 优化（尝试后放弃）

**尝试方案:**
- 4×4 网格分区 + STRtree → 实际更慢，且丢失边界多边形
- 线段简化后再 noding → 快 10% 但丢失 23% blocks

**结论:** GEOS unary_union 对这个问题已经是接近最优的实现，无法在不丢失精度的前提下显著加速。Chicago 25km 的 68s 暂时接受。

---

## 二、工具索引

| 工具 | 用途 |
|------|------|
| `tools/tune_buildings_v2.py` | PNG 调参主力工具，支持 `--city` 切换城市、`--annotate` 标注、`--road-tier` 道路层级 |
| `tools/brick_render.py` | 砖石纹理独立渲染，用于调试 brick_transform 参数 |
| `tools/debug_water.py` | 可视化 OSM 水体 vs 高德补充 vs OSM-only（高德缺失），输出 debug_water_supplement.png |
| `tools/diagnose_block.py` | 分析 city_blocks 缺失原因，输出缺失区域热力图 |
| `tools/block_polygonize_viz.py` | 可视化 polygonize 的输入线段和输出多边形 |
| `tools/amap_sat_water.py` | 从高德卫星图提取水体轮廓（色彩分割 + 形态学） |
| `tools/amap_water_extract.py` | 高德水体 API 调用封装 |
| `tools/amap_align_test.py` | 高德 vs OSM 水体对齐测试（chamfer matching） |
| `tools/test_amap_water.py` | 高德水体 fetch 单元测试 |
| `tools/water_supplement_debug.py` | 水体补充细节调试 |
| `tools/water_threshold_visualizer.py` | 水体色彩阈值可视化调整 |
| `tools/visualize_water_types.py` | 水体分类（river/lake/canal）可视化 |
| `tools/texture_sampler.py` | Z-displacement 纹理采样预览 |
| `tools/bench_block_base.py` | block_base 性能基准测试 |
| `tools/bench_bridge_filter.py` | 桥梁过滤性能基准 |
| `tools/manage_dem.py` | DEM 高程数据管理（下载/缓存） |
| `tools/manage_pbf.py` | PBF 数据管理 |
| `tools/dem_server.py` | 本地 DEM 瓦片服务 |
| `tools/tune_buildings.py` | v1 建筑调参工具（已被 v2 取代） |

---

## 三、当前性能基准 (westlake 25km)

| 阶段 | 耗时 |
|------|------|
| Data fetch (osmium CLI) | ~47s |
| Terrain + water holes | 29.4s |
| Preprocess (block polygonize等) | 113s |
| └ city_blocks (unary_union) | 35.6s |
| └ _extract_BL | 33.0s |
| └ _compute_block_base | 30.6s |
| Buildings (landmarks only) | 0.4s |
| Roads | 1.5s |
| Block_base (brick + texture) | 30.4s |
| └ brick_transform | 3.8s |
| └ textured mesh | 25.3s |
| **Total** | **~261s** |

**瓶颈排序:** preprocess(113s) > block_base(30s) > terrain(29s) > data_fetch(47s)
