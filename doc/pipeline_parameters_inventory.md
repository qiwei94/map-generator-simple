# Pipeline 全参数清单

> 代码中所有写死的值、含义、来源、是否可自动化

---

## 1. 物理尺寸 & 缩放（config.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| INTERNAL_SPAN_MM | 196.0 | 模型 XY 跨度（留 2mm 边距适配 200mm 热床） | 固定（硬件） |
| BUILD_PLATE_MM | 200.0 | Bambu Lab 热床尺寸 | 固定（硬件） |
| NOZZLE_DIAM_MM | 0.4 | 喷嘴直径 | 固定（硬件，换喷嘴时改） |
| MIN_PRINTABLE_AREA_M2 | 4000.0 | 精度过滤阈值（0.4mm×1.5 余量 ≈ 51m²） | 随喷嘴自动算 |

---

## 2. Z 堆叠（config.py）— 模型空间 mm

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| Z_WATER_BASE_MM | -2.00 | 水层底面（贴热床） | 固定 |
| Z_TERRAIN_BASE | -1.60 | 地形底面 = 水层顶面 | 固定（= Z_WATER + WATER_BASE_THICK） |
| Z_BUILDING_EMBED_MM | 0.04 | 建筑嵌入地形深度 | 固定 |
| Z_ROAD_ABOVE_TERRAIN_MM | 0.51 | 道路顶面高于地形 | 固定 |
| VEGETATION_Z_OFFSET_MM | 0.1 | 植被高于地形 | 固定 |
| VL_Z_OFFSET_MM | 0.15 | 植被-地标类 Z 偏移 | 固定 |
| VO_Z_OFFSET_MM | 0.10 | 植被-普通类 Z 偏移 | 固定 |

---

## 3. 层厚度（config.py + 各 builder）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| TERRAIN_THICKNESS_MM | 4.0 | 地形实体厚度 | ⭐ 可按 elevation_range 自适应 |
| WATER_THICKNESS_MM | 0.5 | 水平板厚度（config） | 固定 |
| WATER_BASE_THICKNESS_MM | 0.4 | 水底板厚度 | 固定 |
| ROAD_THICKNESS_MM | 0.4 | 道路薄带厚度 | 固定 |
| RO_THICKNESS (roads.py) | 0.51 | 普通道路挤出高度 | 固定 |
| RL_THICKNESS (roads.py) | 0.71 | 桥梁道路挤出高度（+0.20mm） | 固定 |
| VEGETATION_THICKNESS_MM | 0.2 | 植被层厚度 | 固定 |
| BLOCK_BASE_THICKNESS_MM | 0.5 | block_base 层厚度 | 固定 |
| BUILDING_AGGREGATE_HEIGHT_MM | 0.625 | 街区聚合体超薄高度 | 固定 |

---

## 4. 地形参数（config.py + terrain3d/config.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| Z_GAMMA | 0.45 | 高程幂曲线指数（<1 boost 低起伏） | ⭐ 可按 elevation_range 自适应 |
| ELEVATION_SMOOTHING_SIGMA | 2.5 / 1.0 | 高程高斯平滑（grid cell 数） | ⭐ 可按 max_slope 自适应 |
| TERRAIN_GRID["small"] | 512 | 小区域网格分辨率 | 已实现（按面积） |
| TERRAIN_GRID["medium"] | 768 | 中区域网格分辨率 | 已实现 |
| TERRAIN_GRID["large"] | 1024 | 大区域网格分辨率 | 已实现 |
| DECIMATION_TARGETS["small"] | None | 不做 mesh 简化 | 已实现 |
| DECIMATION_TARGETS["medium"] | 450,000 | 中等 LOD 面数上限 | 已实现 |
| DECIMATION_TARGETS["large"] | 280,000 | 大区域面数上限 | 已实现 |
| AREA_SMALL_THRESHOLD | 5.0 km² | 小/中分界 | 固定 |
| AREA_LARGE_THRESHOLD | 50.0 km² | 中/大分界 | 固定 |

---

## 5. 建筑参数（config.py）

### 5.1 高度估算

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| BUILDING_DEFAULT_HEIGHT_M | 10.0 | 无数据时默认楼高 | 固定 |
| BUILDING_LEVEL_HEIGHT_M | 3.5 | 每层楼高 | 固定 |
| BUILDING_HEIGHT_OSM_MIN_M | 5.0 | OSM 压缩下限 | 固定 |
| BUILDING_HEIGHT_OSM_MAX_M | 150.0 | OSM 压缩上限 | 固定 |
| BUILDING_AREA_HEIGHTS | {100:8, 200:10, 500:15, 1000:25, 2000:40} | 面积→高度代理映射 | 固定 |
| HEIGHT_QUALITY_COVERAGE_THRESHOLD | 0.30 | 高度标签覆盖率<30% → flat mode | 已实现 |

### 5.2 模型高度（mm）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| BUILDING_HEIGHT_MM | 4.0 | 默认凸起高度 | 固定 |
| BUILDING_HEIGHT_MIN_MM | 2.8 | 最低凸起 | 固定 |
| BUILDING_HEIGHT_MAX_MM | 4.0 | 最高凸起 | 固定 |
| BUILDING_FLAT_HEIGHT_LOW_MM | 0.8 | flat mode: 小地标 | 固定 |
| BUILDING_FLAT_HEIGHT_MID_MM | 1.0 | flat mode: 中地标 | 固定 |
| BUILDING_FLAT_HEIGHT_HIGH_MM | 1.3 | flat mode: 大地标 | 固定 |
| BUILDING_FLAT_AREA_MID_M2 | 5000.0 | flat 中/大分界 | 固定 |
| BUILDING_FLAT_AREA_HIGH_M2 | 20000.0 | flat 大/超大分界 | 固定 |

### 5.3 过滤 & 聚合

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| BUILDING_MIN_AREA["small/med/large"] | 30/80/100 | 噪声过滤最小面积（m²） | 已实现（按面积） |
| BUILDING_PRINT_LIMIT_M2 | 2500.0 | 单体保留阈值（m²） | ⭐ 可按 building_density 自适应 |
| BUILDING_AGGREGATE_BUFFER_M | 20.0 | 邻近 40m 内合并 | 固定 |
| BUILDING_AGGREGATE_SHRINK_SLACK_M | 5.0 | 收缩少收 5m 留"块感" | 固定 |
| BUILDING_AGGREGATE_SIMPLIFY_M | 15.0 | 聚合后简化容差 | 固定 |
| BUILDING_SIMPLIFY_TOL_M | 25.0 | 大楼 footprint D-P 简化 | 固定 |

### 5.4 V2 路网聚合策略

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| BUILDING_V2_ENABLED | True | 启用 v2 路网切 block | 固定 |
| BUILDING_V2_MODE | "oriented_bbox" | block fill 模式 | 固定 |
| BUILDING_V2_ROAD_TIER | 5 | 道路等级 1-5 | ⭐ 可按 road_density 自适应 |
| BUILDING_V2_USE_WATER_BLOCKS | True | 水体参与切分 | 固定 |
| BUILDING_V2_BLOCK_BUFFER_M | 20.0 | buffered_union buffer 半径 | 固定 |
| BUILDING_V2_DENSITY_THRESHOLD | 0.005 | block fill 密度阈值 (≥0.5%) | ⭐ 可按 building_density 自适应 |
| BUILDING_V2_COUNT_THRESHOLD | 1 | block 内最少建筑数 | 固定 |
| BUILDING_V2_CONCAVE_RATIO | 0.70 | concave_hull ratio | 固定 |
| BUILDING_V2_MIN_BLOCK_COMPACTNESS | 0.0 | 紧凑度过滤（关闭） | 固定 |
| BUILDING_V2_MIN_BLOCK_AREA_M2 | 0.0 | block 最小面积（关闭） | 固定 |
| BUILDING_V2_MAX_BLOCK_AREA_M2 | 500,000 | 过大 block 过滤 | 固定 |
| BUILDING_V2_INDIVIDUAL_SHAPE | "raw" | 大楼外形保持 | 固定 |
| BUILDING_V2_AGGREGATE_SIMPLIFY_M | 60.0 | v2 聚合后 simplify 容差 | 固定 |
| BUILDING_V2_MIN_BUILDINGS_PER_BLOCK | 0 | block 内最少建筑（关闭） | 固定 |
| BUILDING_V2_USE_LANDMARK_TAGS | True | 启用 OSM 标签识别地标 | 固定 |
| BUILDING_V2_LANDMARK_TOP_PERCENT | 1.0 | top X% 面积也算地标 | 固定 |
| BUILDING_V2_BLOCK_FILL_CONVEX | True | block fill 强制凸 | 固定 |
| BUILDING_V2_HOTSPOT_RELAX | 10.0 | 热点 block 放宽 landmark 阈值 | 固定 |

### 5.5 细长建筑（CLI arg）

| 参数 | 默认值 | 含义 | 可自动化? |
|------|--------|------|----------|
| narrow-threshold | 6.0 | aspect ratio 阈值 | 固定 |
| narrow-penalty | 0.5 | 细长建筑高度缩放 | 固定 |

---

## 6. 道路参数（config.py + roads.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| ROAD_WIDTH_MULTIPLIER | 5.0 | 视觉宽度放大倍数 | ⭐ 可按 motorway_ratio 自适应 |
| ROAD_FACE_NORMAL_Z_RATIO | 0.90 | 顶面法向检测阈值 | 固定 |
| ROAD_DENSIFY_MAX_M | 10.0 | 中心线加密最大段长 | 固定 |
| ROAD_WIDTHS | {motorway:16, trunk:14, ...} | 各级别实际宽度(m) | 固定 |
| ROAD_FILTER["large"] | {motorway~secondary} | 大区域只保留主干 | 已实现 |

---

## 7. 水体参数（config.py + _water_supplement.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| WATER_MIN_AREA_M2 | 50,000 | 水体最小保留面积（m²） | ⭐ 可按 water_ratio 自适应 |
| WATER_HEIGHT_MODEL_MM | 100.0 | 水特征高度（model meters） | 固定 |
| WATER_MAX_EDGE_M | 100.0 | 三角化前最大边长 | 固定 |
| WATERWAY_HALF_WIDTH | {river:90, canal:25, stream:10, ...} | 线性水体 buffer 半宽(m) | ⭐ 待集成 adaptive |
| WATERWAY_WIDTHS | {river:500, canal:30, stream:12, ...} | 水体渲染宽度 | 固定 |
| WATER_HIGH_DETAIL | False | 高精度水体（三潭印月等） | ⭐ 可按 has_major_lake 自适应 |

### 7.1 高德水体补全（_water_supplement.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| _AMAP_ZOOM | 14 | 瓦片缩放级别 | 固定 |
| _AMAP_TILE_PX | 512 | 瓦片像素 (scl=2) | 固定 |
| _MIN_SEGMENT_LEN | 200 | 忽略短于此长度的未覆盖段(m) | 固定 |
| _MIN_POLYGON_AREA_M2 | 50,000 | 高德 polygon 最小面积 | 固定 |
| _MAX_SUPPLEMENT_AREA_M2 | 500,000 | 高德 polygon 最大面积（防误检） | 固定 |
| _ADAPTIVE_MIN_HW | 120 | adaptive buffer 最小半宽(m) | ⭐ 可按 waterway_type 自适应 |
| _ADAPTIVE_MAX_HW | 450 | adaptive buffer 最大半宽(m) | ⭐ |
| _ADAPTIVE_DECAY_DIST | 15,000 | 衰减距离(m) | 固定 |

---

## 8. 植被参数（config.py + vegetation.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| VEGETATION_MIN_AREA_M2 | 5,000 | 最小植被面积（m²） | ⭐ 可按 GEOS 超时风险自适应 |
| VEGETATION_MAX_EDGE_M | 20.0 | 边界加密最大段长 | 固定 |
| VEGETATION_SIMPLIFY_TOL_M | 5.0 | D-P 简化容差 | 固定 |

---

## 9. Block Base 参数（config.py + block_base.py）

| 参数 | 值 | 含义 | 可自动化? |
|------|-----|------|----------|
| BLOCK_BASE_THICKNESS_MM | 0.5 | 底层厚度 | 固定 |
| BLOCK_BASE_MIN_AREA_M2 | 1,000 | 最小 block 面积 | 固定 |
| _MAX_GRID_POINTS (block_base.py) | 10,000 | Z-displacement 网格上限 | 固定 |
| grid_step_mm | 0.5 | Z-displacement 采样步长 | 固定 |
| amp_scale | 2.0 | Z-displacement 振幅系数 | 固定 |

---

## 10. 砖石纹理参数（_brick_transform.py 函数签名默认值）

| 参数 | 默认值 | 含义 | 可自动化? |
|------|--------|------|----------|
| corner_r_m | 8.0 | 圆角半径(m) | ⭐ 可按 scale 自适应 |
| rot_deg | 10.0 | 微旋转最大角度(°) | 固定 |
| shift_m | 8.0 | 微平移最大距离(m) | ⭐ 可按 avg_block_area 自适应 |
| perlin_amp | 4.0 | Perlin 噪声振幅(m) | ⭐ 可按 avg_block_area 自适应 |
| perlin_freq | 0.15 | Perlin 噪声频率 | 固定 |
| resample_m | 12.0 | 边缘重采样间距(m) | 固定 |
| noise_seed | 2026 | 随机种子 | 固定 |

---

## 11. 颜色 / Extruder 映射（config.py）

| 参数 | 值 | 含义 |
|------|-----|------|
| EXTRUDER_MAP | terrain→E2, buildings→E1, roads→E2, water→E3, vegetation→E2, block_base→E1 | 3 料槽分配 |
| FILAMENT_COLOURS | [白, 灰, 黑] | 料色定义 |
| TERRAIN_COLOR | #9A9A9A | 灰 (E2) |
| BUILDING_COLOR | #FFFFFF | 白 (E1) |
| ROAD_COLOR | #9A9A9A | 灰 (E2) |
| WATER_COLOR | #000000 | 黑 (E3) |
| BLOCK_BASE_COLOR | #FFFFFF | 白 (E1) |

---

## 12. OSM 标签过滤

| 参数 | 值 | 含义 |
|------|-----|------|
| WATER_POLYGON_TAGS | {natural:water, landuse:reservoir} | 水 polygon 筛选 |
| WATER_LINE_TAGS | {waterway: True} | 水 line 筛选 |
| VEGETATION_TAGS | {landuse:[forest,grass,...], natural:[wood,...]} | 植被筛选 |
| PARKS_TAGS | {leisure:[park,garden,...]} | 公园筛选 |

---

## 13. terrain3d/config.py 独有参数（部分与 config.py 重复）

| 参数 | 值 | 含义 |
|------|-----|------|
| CACHE_TTL_SECONDS | -1 | 缓存永不过期 |
| CACHE_MIN_FREE_SPACE_GB | 1.0 | 缓存路径最低剩余空间 |
| NDSM_MIN_HEIGHT_M | 3.0 | nDSM 噪声下限 |
| NDSM_MAX_HEIGHT_M | 300.0 | nDSM 异常上限 |
| NDSM_SAMPLE_PERCENTILE | 90 | footprint 内取 P90 |
| WATER_PRINT_VERTEX_SPACING_M | 50.0 | 水 mesh 边界顶点最大间距 |
| ROAD_DENSIFY_MAX_SEGMENT_M | 10.0 (low=25) | 路段加密最大段长 |
| WATER_POLYGON_SIMPLIFY_MAX_POINTS | 500 | 水 polygon 简化上限 |
| PRINT_BASE_THICKNESS_M | 50.0 | 打印底座厚度(model m) |
| PRINT_LAYER_HEIGHT_MM | 0.2 | 各层分离高度 |
| MESH_WORKERS | 0 (auto) | 并行构建线程数 |
| PIN_DBSCAN_EPS_M | 120.0 | 热点 DBSCAN epsilon |
| PIN_DBSCAN_MIN_SAMPLES | 2 | 热点最少样本数 |
| PIN_CYLINDER_SEGMENTS | 16 | 针脚圆柱段数 |
| PARKS_MIN_AREA_M2 | 10.0 | 公园最小面积 |
| WETLANDS_MIN_AREA_M2 | 15.0 | 湿地最小面积 |

---

## 14. CLI 开关（generate_city.py）

| 开关 | 默认 | 含义 |
|------|------|------|
| --narrow-threshold | 6.0 | 细长建筑 aspect ratio |
| --narrow-penalty | 0.5 | 细长建筑高度缩放 |
| --no-vegetation | False | 跳过植被层 |
| --no-block-base | False | 跳过 block_base 层 |
| --merge-layers | False | 合并 block_base+BO |
| --use-ndsm | False | 使用 nDSM 推算楼高 |
| --elevation-file | None | 本地 DEM 文件 |

---

## 汇总统计

| 分类 | 参数总数 | 已实现自适应 | 可自动化（待实现） | 硬件/固定 |
|------|---------|-------------|-------------------|----------|
| 物理/硬件 | 4 | 0 | 1 | 3 |
| Z 堆叠 | 7 | 0 | 0 | 7 |
| 层厚度 | 9 | 0 | 1 | 8 |
| 地形 | 10 | 5 | 2 | 3 |
| 建筑高度 | 14 | 1 | 0 | 13 |
| 建筑过滤/聚合 | 24 | 1 | 3 | 20 |
| 道路 | 5 | 1 | 1 | 3 |
| 水体 | 12 | 0 | 4 | 8 |
| 植被 | 3 | 0 | 1 | 2 |
| Block Base | 5 | 0 | 0 | 5 |
| 砖石纹理 | 7 | 0 | 3 | 4 |
| 颜色/Extruder | 7 | 0 | 0 | 7 |
| OSM 标签 | 4 | 0 | 0 | 4 |
| terrain3d 独有 | 15 | 0 | 0 | 15 |
| CLI 开关 | 7 | 0 | 0 | 7 |
| **总计** | **133** | **8** | **16** | **109** |

---

## 自动化优先级排序（影响×可行性）

| 优先级 | 参数 | 影响 | 实现难度 |
|--------|------|------|---------|
| P0 | Z_GAMMA | ⭐⭐⭐ 地形立体感全局 | 低（1 条规则） |
| P0 | BUILDING_V2_DENSITY_THRESHOLD | ⭐⭐⭐ 建筑覆盖率 | 低 |
| P0 | BUILDING_PRINT_LIMIT_M2 | ⭐⭐⭐ 个体/聚合分界 | 低 |
| P1 | BUILDING_V2_ROAD_TIER | ⭐⭐ block 碎片度 | 低 |
| P1 | ROAD_WIDTH_MULTIPLIER | ⭐⭐ 路网视觉显著性 | 低 |
| P1 | WATER_MIN_AREA_M2 | ⭐⭐ 水碎片过滤 | 低 |
| P1 | WATER_HIGH_DETAIL | ⭐⭐ 大湖岛屿保留 | 低（bool） |
| P2 | TERRAIN_THICKNESS_MM | ⭐⭐ 打印强度 | 中 |
| P2 | ELEVATION_SMOOTHING_SIGMA | ⭐ 打印悬垂 | 中 |
| P2 | VEGETATION_MIN_AREA_M2 | ⭐ GEOS 安全 | 低 |
| P2 | brick corner_r_m / perlin_amp / shift_m | ⭐⭐ 手绘质感 | 中 |
| P3 | _ADAPTIVE_MIN/MAX_HW | ⭐⭐ 河流宽度 | 中（待集成） |
| P3 | WATERWAY_HALF_WIDTH | ⭐⭐ 线性水体宽 | 中 |
