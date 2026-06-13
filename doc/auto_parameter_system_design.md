# Auto-Parameter System Design

> 目标：用户只输入 GPS 坐标或城市名 → 系统自动选择所有参数 → 出图无需人工调参。

---

## 1. 城市特征 → 风格选择

### 1.1 特征检测维度

| 特征维度 | 检测方法 | 数据来源 | 输出 |
|---------|---------|---------|------|
| **地形起伏** | `(DEM_max - DEM_min) / bbox_diagonal` | SRTM/ASTER DEM | relief_ratio: flat/moderate/mountainous |
| **水体覆盖率** | `water_area / bbox_area` | OSM natural=water + waterway | water_ratio: dry/moderate/water-dominant |
| **建筑密度** | `building_footprint_total / land_area` | OSM building=* | density: sparse/suburban/urban/hyper-urban |
| **路网密度** | `total_road_length / bbox_area` | OSM highway=* | road_density: sparse/normal/dense |
| **植被覆盖** | `vegetation_area / bbox_area` | OSM landuse=forest/grass + natural=wood | green_ratio: barren/moderate/lush |
| **海岸线存在** | `coastline_length > 0` | OSM natural=coastline | is_coastal: bool |
| **OSM 数据质量** | `building_count / expected_count` + height_tag_coverage | OSM tags | quality: poor/fair/good |

### 1.2 风格映射规则

```
IF relief_ratio == "mountainous" AND water_ratio > 0.05:
    style = "terrain-first"  # 强调地形，减少覆盖物
    
ELIF water_ratio > 0.15:
    style = "water-first"   # 强调水体和海岸线
    
ELIF density == "hyper-urban" AND road_density == "dense":
    style = "classic"       # 标准城市模式，路网+建筑+水

ELIF density == "sparse" AND green_ratio == "lush":
    style = "terrain-first" # 郊区/自然区，突出地貌

ELSE:
    style = "classic"       # 默认
```

### 1.3 风格 → 参数组

| 风格 | 建筑 | 路网 | 水体 | 植被 | terrain Z_GAMMA |
|------|------|------|------|------|----------------|
| classic | v2 block_fill | tier 5 (all) | full | enabled | 0.45 |
| terrain-first | 仅 landmarks | tier 2 (主干) | full | disabled | 0.35 (更平缓) |
| water-first | v2 block_fill | tier 3 | full + high-detail | enabled | 0.50 |
| minimal | disabled | disabled | simplified | disabled | 0.45 |

---

## 2. 指标 → 参数决策表

### 2.1 面积自适应（已实现，扩展）

| 指标 | 参数 | 逻辑 | 当前实现 |
|------|------|------|---------|
| `area_km2 < 5` | TERRAIN_GRID=512, no decimation | 小区域高精度 | ✅ get_area_class() |
| `area_km2 5-50` | TERRAIN_GRID=768, decimate 450K | 中等 LOD | ✅ |
| `area_km2 > 50` | TERRAIN_GRID=1024, decimate 280K, ROAD_FILTER=主干 | 大区域简化 | ✅ |

### 2.2 建筑密度自适应（待实现）

| 指标 | 参数 | 逻辑 | 影响 |
|------|------|------|------|
| `building_count / area_km2 > 2000` | BUILDING_V2_DENSITY_THRESHOLD=0.01 | 超密城区提高密度阈值，减少噪声 | 减少过小 block 填充 |
| `building_count / area_km2 < 200` | BUILDING_V2_DENSITY_THRESHOLD=0.001 | 稀疏区降低阈值保留信息 | 避免城区空白 |
| `avg_building_area > 500m²` | BUILDING_PRINT_LIMIT_M2=1500 | 大楼为主(CBD) → 降低聚合阈值 | 更多个体建筑保留 |
| `avg_building_area < 100m²` | BUILDING_PRINT_LIMIT_M2=4000 | 密集小楼(老城) → 提高聚合阈值 | 强制聚合为街区 |
| `height_tag_coverage < 0.30` | flat_mode=True | 高度数据不可靠 → 面积梯度代替 | 避免错误高度 |
| `height_tag_coverage > 0.30` | 正常 OSM 高度压缩 | 有可靠高度数据 | 真实高度映射 |

### 2.3 路网自适应（待实现）

| 指标 | 参数 | 逻辑 | 影响 |
|------|------|------|------|
| `road_length / area_km2 > 15 km/km²` | BUILDING_V2_ROAD_TIER=4 | 路网密 → 少取路切 block | 避免碎片化 |
| `road_length / area_km2 < 5` | BUILDING_V2_ROAD_TIER=5 | 路网稀 → 取全部路 | 保证有足够 block 切分 |
| `motorway_ratio > 0.3` | ROAD_WIDTH_MULTIPLIER=4.0 | 高速占比大 → 减小宽度倍数 | 避免高速路过粗遮挡地形 |
| `area_km2 > 50` | ROAD_FILTER=主干 | 大区域只保留主干道 | 已实现 ✅ |

### 2.4 水体自适应（部分实现）

| 指标 | 参数 | 逻辑 | 影响 |
|------|------|------|------|
| `has_major_lake (area > 1km²)` | WATER_HIGH_DETAIL=True | 大湖保留细节(岛屿) | 三潭印月等不丢失 |
| `waterway_type == "river"` | WATERWAY_HALF_WIDTH=adaptive | 按河段邻近 polygon 推断 | 已设计 ✅ 待集成 |
| `water_ratio > 0.3` | WATER_MIN_AREA_M2=10000 | 水多 → 提高过滤减少小碎片 | 避免过度密集 |
| `is_coastal` | 增加 coastline buffer | 海岸线需要宽边缘 | 突出海陆边界 |

### 2.5 地形自适应（待实现）

| 指标 | 参数 | 逻辑 | 影响 |
|------|------|------|------|
| `elevation_range < 50m` | Z_GAMMA=0.60 | 极平坦 → 强力放大微起伏 | 平原城市也有立体感 |
| `elevation_range 50-300m` | Z_GAMMA=0.45 | 正常丘陵 | 标准压缩 |
| `elevation_range > 300m` | Z_GAMMA=0.35 | 大山区 → 减小 gamma 避免过陡 | 山顶不会过薄 |
| `elevation_range > 500m` | TERRAIN_THICKNESS_MM=5.0 | 高差大 → 增加底板厚度 | 低处不穿底 |
| `max_slope > 45°` | ELEVATION_SMOOTHING_SIGMA=3.0 | 陡峭 → 多平滑 | 打印时不出悬垂 |

### 2.6 砖石纹理自适应（待实现）

| 指标 | 参数 | 逻辑 | 影响 |
|------|------|------|------|
| `scale (mm/m)` | corner_r_m = 8 / scale_factor | 缩放比决定圆角半径 | 保持视觉一致 |
| `avg_block_area < 5000m²` | perlin_amp=2.0, shift_m=4.0 | 小 block → 减弱扰动避免穿插 | 避免重叠 |
| `avg_block_area > 50000m²` | perlin_amp=6.0, shift_m=12.0 | 大 block → 加大手绘感 | 增强纹理表现力 |

---

## 3. 影响矩阵

每个参数变化对最终输出的影响评级：

| 参数 | 视觉影响 | 打印影响 | 性能影响 | 鲁棒性风险 |
|------|---------|---------|---------|-----------|
| Z_GAMMA | ⭐⭐⭐ 地形立体感 | ⭐⭐ 最薄处强度 | 无 | 低 |
| BUILDING_V2_DENSITY_THRESHOLD | ⭐⭐⭐ 建筑密度 | ⭐ | ⭐ | 中-可能空白或过密 |
| BUILDING_PRINT_LIMIT_M2 | ⭐⭐ 建筑细节 | ⭐⭐⭐ 能否打印 | ⭐ | 中 |
| ROAD_WIDTH_MULTIPLIER | ⭐⭐ 路网显著性 | ⭐⭐ 薄壁问题 | 无 | 低 |
| WATERWAY_HALF_WIDTH | ⭐⭐⭐ 河流宽度 | ⭐ | 无 | 高-错误值很明显 |
| TERRAIN_GRID | ⭐⭐ 地形细节 | ⭐ 文件大小 | ⭐⭐⭐ | 低 |
| ELEVATION_SMOOTHING_SIGMA | ⭐ 平滑度 | ⭐ 悬垂角 | ⭐ | 低 |
| VEGETATION_MIN_AREA_M2 | ⭐⭐ 绿化显示 | ⭐ | ⭐ | 中-过小会 GEOS hang |
| brick corner_r_m | ⭐⭐ 手感 | 无 | ⭐ | 低 |
| brick perlin_amp | ⭐⭐ 手绘感 | ⭐ 极端值穿插 | ⭐ | 中 |

---

## 4. 调试工具体系

### 4.1 参数决策报告（自动生成）

每次运行自动输出 `output/{city}/param_decision.json`：

```json
{
  "city": "westlake",
  "bbox_km2": 156.25,
  "detected_features": {
    "relief_ratio": "moderate",
    "elevation_range_m": 456,
    "water_ratio": 0.08,
    "building_density_per_km2": 850,
    "avg_building_area_m2": 220,
    "height_tag_coverage": 0.12,
    "road_density_km_per_km2": 9.3,
    "vegetation_ratio": 0.22,
    "is_coastal": false
  },
  "style_selected": "classic",
  "params_applied": {
    "Z_GAMMA": {"value": 0.40, "reason": "elevation_range=456m > 300m → reduce gamma"},
    "BUILDING_V2_DENSITY_THRESHOLD": {"value": 0.005, "reason": "density=850 in normal range"},
    "TERRAIN_GRID": {"value": 768, "reason": "area=156km² → medium"},
    "ROAD_TIER": {"value": 5, "reason": "road_density=9.3 in normal range"},
    "flat_mode": {"value": true, "reason": "height_coverage=12% < 30%"}
  },
  "overrides_from_user": {}
}
```

### 4.2 对比 A/B 工具

```bash
# 快速对比两组参数的 PNG 差异
venv/bin/python tools/param_compare.py \
  --city westlake \
  --param Z_GAMMA \
  --values 0.35,0.45,0.55 \
  --output tmp/compare_zgamma/
```

输出 3 张并排 PNG + diff heat map，用于调参验证。

### 4.3 城市覆盖率仪表盘

```bash
# 批量跑多城市，验证参数系统鲁棒性
venv/bin/python tools/batch_validate.py \
  --cities westlake,chicago,tokyo,paris,dubai,manhattan \
  --output tmp/validation_report/
```

输出每城市：参数决策 JSON + PNG + 异常标注（空白区域、过密区域、GEOS 超时）。

### 4.4 单参数影响可视化

```bash
# 单独看某参数对输出的影响范围
venv/bin/python tools/param_sensitivity.py \
  --city westlake \
  --param BUILDING_V2_DENSITY_THRESHOLD \
  --range 0.001,0.005,0.01,0.05 \
  --metric block_fill_ratio,building_count,empty_area_ratio
```

### 4.5 运行时告警

| 异常 | 检测条件 | 动作 |
|------|---------|------|
| 建筑空白区 | `filled_blocks / total_blocks < 0.3` | 降低 DENSITY_THRESHOLD |
| 过密 | `filled_blocks / total_blocks > 0.95` | 提高 DENSITY_THRESHOLD |
| 地形穿底 | `min(terrain_z_mm) < 0.3` | 提高 TERRAIN_THICKNESS_MM |
| 河流异常宽 | `river_width > 2km` | clamp WATERWAY_HALF_WIDTH |
| GEOS 超时 | `operation_time > 60s` | 增大 VEGETATION_MIN_AREA_M2, 减少 road tier |
| 建筑高度错误 | `max_height_m > 500` | 切 flat_mode |

---

## 5. 验证方法

### 5.1 回归测试（自动化）

```
test_cities = [
    "westlake",     # 丘陵+大湖+中密度
    "chicago",      # 平坦+大湖+超密度
    "tokyo",        # 平坦+海岸+超密度
    "paris",        # 平坦+河流+高密度
    "dubai",        # 平坦+海岸+稀疏(沙漠)
    "manhattan",    # 平坦+海岸+超高密度
    "chongqing",    # 山地+江河+高密度
    "reykjavik",    # 丘陵+海岸+极稀疏
    "venice",       # 平坦+水域极高+中密度
    "la_paz",       # 高原山地+无水+稀疏
]
```

每城市验证标准：
1. **无崩溃** — pipeline 完整运行不 crash
2. **无明显空白** — building layer 不能出现 >20% 面积无任何元素
3. **无明显溢出** — 没有元素超出 bbox
4. **水体完整** — 主要水体形态可识别
5. **路网连通** — 主干道不断裂
6. **地形合理** — 没有明显平面（除非真的平坦）
7. **打印安全** — 没有 <0.3mm 的悬空薄壁

### 5.2 人工抽检节点

自动化不能替代的：
- 第一次跑新大洲/新类型城市 → 必须人工看 PNG
- Z_GAMMA 或 brick 参数变化 → 必须人工确认手感
- 新增 OSM tag 类型 → 确认渲染正确

### 5.3 指标衰减监控

长期运行时跟踪：
- `pipeline_success_rate` — 连续 N 城市成功率
- `avg_generation_time` — 平均耗时趋势
- `user_rejection_rate` — 用户看 PNG 后拒绝率（需要前端上报）

---

## 6. 实现路线图

### Phase 1: 特征检测器（1-2天）

```python
# 新文件: _TEXTURE_STYLE_OF_DEEPSEEK/city_profile.py

@dataclass
class CityProfile:
    """从 OSM 数据统计出的城市特征向量。"""
    area_km2: float
    elevation_range_m: float
    relief_ratio: str          # flat/moderate/mountainous
    water_ratio: float
    building_density: float    # buildings per km²
    avg_building_area_m2: float
    height_tag_coverage: float
    road_density_km_per_km2: float
    vegetation_ratio: float
    is_coastal: bool
    osm_quality: str           # poor/fair/good

def detect_city_profile(
    bbox_wgs84, utm_crs, 
    buildings_gdf, roads_gdf, water_gdf, vegetation_gdf,
    dem_array, dem_transform
) -> CityProfile:
    """从预处理数据中提取城市特征。在 preprocess_layers 之后调用。"""
    ...
```

### Phase 2: 参数解析器（1天）

```python
# 新文件: _TEXTURE_STYLE_OF_DEEPSEEK/param_resolver.py

@dataclass
class ResolvedParams:
    """从 CityProfile 自动推算出的全部运行参数。"""
    style: str
    z_gamma: float
    terrain_grid: int
    building_density_threshold: float
    building_print_limit_m2: float
    road_tier: int
    road_width_multiplier: float
    water_high_detail: bool
    waterway_half_width: dict
    vegetation_min_area_m2: float
    elevation_smoothing_sigma: float
    terrain_thickness_mm: float
    flat_mode: bool
    brick_corner_r_m: float
    brick_perlin_amp: float
    # ... all params

def resolve_params(profile: CityProfile, user_overrides: dict = None) -> ResolvedParams:
    """规则引擎：CityProfile → 参数集。user_overrides 可覆盖任何自动决策。"""
    ...

def explain_decisions(profile: CityProfile, params: ResolvedParams) -> dict:
    """生成人可读的决策报告 JSON。"""
    ...
```

### Phase 3: Pipeline 集成（0.5天）

```python
# 在 generate_city.py / backup_westlake_cli.py 中:

profile = detect_city_profile(bbox_wgs84, utm_crs, ...)
params = resolve_params(profile, user_overrides=cli_overrides)
log_decisions(params, output_dir)  # 写 param_decision.json

# 然后用 params.xxx 替代所有硬编码常量
```

### Phase 4: 验证工具（1天）

- `tools/param_compare.py` — A/B 对比
- `tools/batch_validate.py` — 批量城市测试
- `tools/param_sensitivity.py` — 单参数影响曲线

### Phase 5: 迭代调优（持续）

用 batch_validate 跑 10 城市 → 发现问题 → 调整规则 → 重跑验证 → 收敛。

---

## 7. 关键设计决策

1. **规则引擎 > ML** — 参数少（~20个关键）、可解释、可调试。不用 ML 黑盒。
2. **检测在前，决策在后** — CityProfile 是纯粹的事实描述，ResolvedParams 是决策结果，分离关注点。
3. **user_overrides 永远优先** — 自动参数是默认值，用户可以 override 任何一个。
4. **决策可追溯** — 每个参数附带 reason 字符串，事后可审计。
5. **渐进式接管** — 先接管最安全的参数（area_class/terrain_grid），再逐步接管风险高的参数（Z_GAMMA/density_threshold）。
6. **Fail-open** — 检测失败时使用保守默认值（classic + medium 参数），而不是 crash。

---

## 8. 已有基础 vs 待建部分

| 能力 | 状态 | 位置 |
|------|------|------|
| 面积分级 (small/medium/large) | ✅ 已实现 | config.py: get_area_class() |
| 建筑高度质量检测 | ✅ 已实现 | buildings.py: height_coverage → flat_mode |
| 自适应河流宽度 | ✅ 已验证 | _water_supplement.py: _adaptive_buffer_segments() |
| ROAD_FILTER by area | ✅ 已实现 | config.py: ROAD_FILTER |
| TERRAIN_GRID by area | ✅ 已实现 | config.py: TERRAIN_GRID |
| 城市特征检测器 | ❌ 不存在 | 需新建 city_profile.py |
| 参数解析器(规则引擎) | ❌ 不存在 | 需新建 param_resolver.py |
| 决策报告输出 | ❌ 不存在 | 需集成到 pipeline |
| A/B 对比工具 | ❌ 不存在 | 需新建 tools/param_compare.py |
| 批量验证工具 | ❌ 不存在 | 需新建 tools/batch_validate.py |
| 运行时告警 | ❌ 不存在 | 需集成到各阶段 |

---

## 9. 喷嘴精度适配层（后处理 filter）

独立于参数系统，在 3MF 导出后执行：

```python
# 新文件: _TEXTURE_STYLE_OF_DEEPSEEK/nozzle_filter.py

@dataclass
class NozzleSpec:
    diameter_mm: float = 0.4       # 当前：Bambu Lab 0.4mm
    min_wall_mm: float = 0.45      # 最小打印壁厚 = nozzle × 1.12
    min_gap_mm: float = 0.5        # 相邻部件最小间距
    min_feature_mm: float = 0.6    # 最小可辨识特征尺寸

def apply_nozzle_filter(mesh: trimesh.Trimesh, spec: NozzleSpec) -> trimesh.Trimesh:
    """后处理：将低于精度限制的特征合并/移除/加粗到可打印尺寸。
    
    未来喷嘴升级到 0.2mm 时，只需改 NozzleSpec.diameter_mm = 0.2。
    """
    ...
```

此 filter 在参数系统之外独立运行，技术升级时只改一个数字。

---

## 10. AI 美学评估系统

规则引擎解决"不出错"，AI 解决"好不好看"。三层递进设计：

### 10.1 Layer 1: AI 评审（生成后质量门禁）

生成 PNG 后，vision model 对输出做结构化评分，决定是否需要调参重跑。

**评分维度：**

| 维度 | 评估内容 | 评分 1-5 | 调参触发条件 |
|------|---------|---------|-------------|
| **构图平衡** | 信息分布是否偏重某侧/某角 | ≤2 触发 | 调整 road_tier / vegetation 范围 |
| **信息密度** | 太空（无趣）vs 太挤（不可读） | ≤2 或 =5 触发 | 调整 DENSITY_THRESHOLD / PRINT_LIMIT |
| **层次可读性** | 路/建筑/水/地形能否一眼分清 | ≤2 触发 | 调整 Z 高度差 / 颜色对比 |
| **风格一致性** | 砖石纹理在当前比例下是否自然 | ≤2 触发 | 调整 brick perlin_amp / corner_r |
| **主体突出度** | 主要水体/地标是否足够显眼 | ≤3 触发 | 调整 WATER_HIGH_DETAIL / road 宽度 |
| **整体美感** | 直觉性审美判断 | ≤2 触发 | 综合调参 |

**评审 Prompt 结构：**

```
你是一位地图艺术品的视觉审美评审。
这是一张 {city_name} 的 3D 打印城市地图预览图。
目标风格：手绘砖石质感，层次分明，适合作为桌面摆件。

请对以下维度打分（1-5）并给出具体修改建议：
1. 构图平衡 — ...
2. 信息密度 — ...
...

输出 JSON：
{
  "scores": {"balance": 4, "density": 2, ...},
  "overall": 3,
  "issues": ["建筑区域西南角过于密集，与东北角空旷形成过强对比"],
  "suggestions": [
    {"param": "BUILDING_V2_DENSITY_THRESHOLD", "direction": "increase", "reason": "..."},
    ...
  ]
}
```

**执行流程：**

```
rules_params = resolve_params(profile)     # 规则引擎出初始参数
png = generate_png(rules_params)           # 生成 PNG
 
for attempt in range(MAX_REFINE_ROUNDS):   # 最多 2-3 轮
    review = ai_review(png, profile)       # AI 评审
    if review["overall"] >= 4:
        break                              # 通过，结束
    adjusted = apply_suggestions(rules_params, review["suggestions"])
    png = generate_png(adjusted)           # 重跑

# 最终输出 png + review log
```

**约束：**
- 最多 3 轮迭代（成本控制）
- 每轮只调 ≤3 个参数（避免震荡）
- AI 建议只能在规则引擎允许的范围内调参（有上下界 clamp）
- 如果 3 轮后仍 <3 分，标记为"需人工审核"而非继续迭代

### 10.2 Layer 2: AI 艺术指导（生成前策略）

在规则引擎之前，让 AI 基于城市特征做战略性风格决策。不是具体参数，而是"应该强调什么、弱化什么"。

**输入：**
- CityProfile（特征向量）
- 城市名称 + 地理背景
- 3-5 张被用户认可的参考图（few-shot）

**输出：**
```json
{
  "emphasis": ["地形落差", "江河交汇"],
  "de_emphasis": ["独栋建筑细节"],
  "style_notes": "建筑应聚合为连片灰色带，作为地形的附属而非主角",
  "param_overrides": {
    "Z_GAMMA": 0.35,
    "BUILDING_V2_ROAD_TIER": 3,
    "ROAD_WIDTH_MULTIPLIER": 2.5
  }
}
```

**何时触发：**
- 新城市首次生成（无历史数据）
- 城市特征组合命中"罕见模式"（规则引擎没有明确路径时）
- 用户主动请求（"帮我想想这个城市怎么做好看"）

**与规则引擎的关系：**
```
ai_strategy = ai_art_direction(profile, references)  # 可选，仅首次
rules_params = resolve_params(profile, overrides=ai_strategy["param_overrides"])
# 后续同 Layer 1 流程
```

### 10.3 Layer 3: 用户偏好学习（长期闭环）

通过用户对 PNG 的 accept/reject 反馈，逐步学习个人审美偏好。

**数据收集：**

```json
// preference_log.jsonl — 每条记录一次用户判断
{"city": "westlake", "params": {...}, "png_path": "...", "verdict": "accept", "user_note": ""}
{"city": "chicago", "params": {...}, "png_path": "...", "verdict": "reject", "user_note": "建筑太密了"}
{"city": "chongqing", "params": {...}, "png_path": "...", "verdict": "accept", "user_note": "地形很好"}
```

**偏好提取（10-20 条后即可启动）：**

将 accept/reject 历史喂给 AI，提取偏好规则：

```
基于用户的历史判断：
- 接受的图共性：地形突出、建筑密度中等、水体面积占比 8-15%
- 拒绝的图共性：建筑过密遮盖地形、路网太粗
- 推断偏好：用户偏好"地形优先"风格，建筑作为点缀而非主体

建议调整默认参数：
- Z_GAMMA 默认从 0.45 → 0.40（更平缓地形，更突出）
- BUILDING_V2_DENSITY_THRESHOLD 默认从 0.005 → 0.008（减少建筑量）
```

**应用方式：**
- 偏好作为 `user_style_bias` 注入 resolve_params，类似 user_overrides 但优先级低于显式 override
- 偏好可按城市类型分组（"山地城市"偏好 vs "平原城市"偏好）
- 用户随时可 reset（"忘掉之前的偏好"）

### 10.4 评估 Prompt 工程要点

**关键原则：**
1. **锚定参考** — 始终附带 2-3 张"好"的参考图，不让 AI 凭空判断
2. **物理约束提醒** — prompt 中说明"这是 3D 打印品，需考虑实体呈现效果"
3. **风格词汇固定** — 定义明确术语表（"手绘感"、"砖石纹理"、"层次感"），避免 AI 用模糊表述
4. **结构化输出** — 强制 JSON 格式，直接可解析为参数调整
5. **温度 = 0** — 评审需要确定性，不能每次给不同分

**成本控制：**
- Layer 1 评审：~$0.02/次 × 最多 3 轮 = $0.06/城市
- Layer 2 艺术指导：~$0.05/次 × 仅首次 = $0.05/新城市
- Layer 3 偏好提取：~$0.03/次 × 每 10 条触发 = 极低频
- 总成本：< $0.15/城市（可接受，产品售价 >> 成本）

### 10.5 与现有系统的集成点

```
┌─────────────────────────────────────────────────────────────┐
│                     完整参数决策流程                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  OSM Data ──→ CityProfile ──→ [AI 艺术指导 (可选)]          │
│                    │                    │                    │
│                    ▼                    ▼                    │
│              resolve_params() ◄── ai_overrides              │
│                    │              user_overrides             │
│                    │              user_style_bias            │
│                    ▼                                        │
│              generate_png()                                  │
│                    │                                        │
│                    ▼                                        │
│              [AI 评审] ──→ score ≥ 4? ──→ ✅ 输出           │
│                    │              │                          │
│                    │         score < 4                       │
│                    ▼              │                          │
│              apply_suggestions() ◄┘                          │
│                    │                                        │
│                    ▼                                        │
│              re-generate (max 3 rounds)                      │
│                    │                                        │
│                    ▼                                        │
│              最终 PNG + param_decision.json + review_log     │
│                                                             │
│  [用户 accept/reject] ──→ preference_log ──→ style_bias     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 10.6 实现优先级

| 阶段 | 内容 | 依赖 | 工期 |
|------|------|------|------|
| Phase A | AI 评审（Layer 1） | 规则引擎 + PNG pipeline 可运行 | 1 天 |
| Phase B | 评审 → 自动调参闭环 | Phase A + param 上下界定义 | 1 天 |
| Phase C | AI 艺术指导（Layer 2） | 参考图库 ≥ 5 张 | 0.5 天 |
| Phase D | 用户偏好学习（Layer 3） | 前端 accept/reject 按钮 | 1 天 |

Phase A 可以和规则引擎 Phase 1-3 并行开发。

---

## 11. 关键设计决策（补充）

8. **规则做底线，AI 做上限** — 规则引擎保证功能正确（不崩溃、不穿底、不空白），AI 把"60 分能用"提升到"85 分好看"。
9. **AI 不直接控制参数** — AI 只输出建议，必须经过 clamp + 安全检查才能生效。避免 hallucination 导致极端参数。
10. **可关闭** — `--no-ai-review` 跳过 AI 评审，用于批量跑 / 调试 / 省成本场景。
11. **评审结果可缓存** — 同一 CityProfile + 同一参数组 → 缓存评审结果，避免重复调用。

---

## 12. 拆件单色打印（打印效率优化）

### 12.1 问题

当前模型使用 3 色（白/灰/黑）AMS 换料打印：
- 每次换色：~30s 抽拉 + ~3g 废料塔（purge tower）
- 200mm 高模型约换色 150-300 次（每层有多色区域都换一次）
- 废料塔消耗 ≈ 模型用料的 30-50%
- 总打印时间因换色增加 2-3 小时

### 12.2 方案：拆件 + 单色打印 + 组装

利用现有 Z-stack 天然分层，将模型拆为可独立打印的单色部件：

```
┌─────────────────────────────────────────────────────┐
│ 组装关系（从底到顶）                                   │
├─────────────────────────────────────────────────────┤
│                                                     │
│  Piece C: 建筑 + Block Base (白 E1)                  │
│    ↓ 扣入                                           │
│  Piece B: 地形 + 道路 + 植被 (灰 E2)                 │
│    ↓ 叠放                                           │
│  Piece A: 水底板 (黑 E3)                             │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**Piece A — 水底板（黑）**
- 几何：flat plate，Z = -2.0 ~ -1.6mm
- 当前已是独立 mesh
- 单色打印：~15 min，无废料
- 组装方式：底部直接叠放（靠重力 + 边框配合）

**Piece B — 地形体 + 道路 + 植被（灰）**
- 几何：主体浮雕，Z = -1.6 ~ +2.4mm（地形顶面）
- 道路、植被与地形同色，合并为一个 mesh
- 单色打印：主打印件，~4-6h
- 关键特征：顶面预留建筑嵌入槽（凹坑）

**Piece C — 建筑 + Block Base（白）**
- 几何：薄板 + 凸起建筑体
- 单色打印：~1-2h
- 组装方式：从上方扣入地形预留槽

### 12.3 配合设计（关键工程）

| 要素 | 方案 | 精度要求 |
|------|------|---------|
| **A↔B 对齐** | 边框台阶（step joint）：水板外扩 0.5mm 边缘包住地形底 | ±0.1mm |
| **B↔C 对齐** | 定位销（registration pins）：地形顶面 2-4 个 φ2mm 圆孔 | ±0.15mm |
| **B↔C 嵌合** | 建筑底部带 0.04mm 凸台嵌入地形凹坑（现有 Z_BUILDING_EMBED 自然复用） | 间隙 0.1mm |
| **间隙补偿** | 所有配合面单边留 0.05-0.10mm 间隙（FDM 公差） | 设计时加 |
| **防脱落** | 可选：轻微锥形卡扣 / 胶水 / 磁铁底座 | — |

### 12.4 打印效率对比

| 指标 | 当前 3-色 AMS | 拆件单色 | 改善 |
|------|-------------|---------|------|
| 换色次数 | 150-300 | 0 | 100% ↓ |
| 废料塔 | 30-50% 额外用料 | 0 | 100% ↓ |
| 总打印时间 | ~8-10h | ~6-7h（3件并行可更快） | 20-40% ↓ |
| 失败风险 | 高（换色堵头） | 低 | 显著 ↓ |
| 多件并行 | N/A | 3 台同时打 → 2-3h | 60-70% ↓ |
| 组装复杂度 | 0（一体） | 3 件对齐 | ↑（可接受） |

### 12.5 代码改动点

```python
# 新增: exporter.py 中增加 split mode
def export_deepseek_3mf(meshes, output_dir, *, split_mode="combined"):
    """
    split_mode:
      "combined"   — 当前行为，单 3MF 多色
      "split"      — 输出 3 个单色 3MF + 组装说明
      "split_ab"   — 水板+地形合并（2件模式，适合无黑色料时）
    """
    if split_mode == "split":
        # Piece A: water mesh → water_plate.3mf (E3)
        # Piece B: terrain + roads + vegetation → terrain_body.3mf (E2)
        # Piece C: buildings + block_base → buildings_cap.3mf (E1)
        # + 配合特征（定位销孔、台阶边框、间隙补偿）
        _export_piece_a(...)
        _export_piece_b(...)
        _export_piece_c(...)
        _export_assembly_guide(...)
```

### 12.6 约束 & 注意事项

1. **最小嵌合面积** — 配合特征（销孔、台阶）自身不能小于 nozzle 精度
2. **Piece C 翘曲** — 白色薄板件容易翘曲，需加 brim 或底部加强筋
3. **Z 精度** — FDM 层高 0.2mm，配合尺寸必须是层高整数倍
4. **色彩边界不锐利** — 单色拼接的颜色分界比 AMS 换色更锐利（反而是优势）
5. **用户组装门槛** — 需提供纸质/视频组装说明，或设计免工具卡扣
6. **可退化** — 拆件模式可选，用户也可选传统 AMS 模式

---

## 13. 缩放自适应参数系统（多尺寸支持）

### 13.1 问题

当前所有参数为 **25km×25km → 196mm** 这一固定比例标定：
- scale ≈ 0.00784 mm/m
- BUILDING_PRINT_LIMIT_M2 = 2500 → 模型中 ≈ 0.12mm² (刚好 nozzle 可印)

当 bbox 或模型尺寸变化时，原参数不再适用：
- 5km×5km → 100mm: scale = 0.02, 精度提升 2.5x, 可保留更小建筑
- 50km×50km → 300mm: scale = 0.006, 精度降低, 需更激进过滤
- 10km×10km → 150mm: 中等精度

### 13.2 两个自由度

```
用户输入:
  bbox_area_km2  — 实世界覆盖面积 (1 ~ 100+ km²)
  model_span_mm  — 成品物理尺寸 (80 ~ 350 mm)

推导:
  scale = model_span_mm / max(width_m, height_m)   # mm/m
  min_feature_m = NOZZLE_DIAM_MM / scale            # 模型中 1 nozzle 对应多少实地 m
  min_printable_m2 = min_feature_m ** 2             # 最小可印面积 m²
```

### 13.3 范围约束

| 维度 | 下限 | 上限 | 限制原因 |
|------|------|------|---------|
| bbox_area_km2 | 1 | 2500 (50×50) | <1km² OSM 数据太稀疏；>50km 数据量爆炸+GEOS 风险 |
| model_span_mm | 80 | 350 | <80mm 细节全丢；>350mm 超热床/重量不实际 |
| scale (mm/m) | 0.002 | 0.08 | 推导值，不直接设置 |
| min_feature_m | 5 | 200 | 决定了能印什么粒度的真实物体 |

### 13.4 Scale-Dependent 参数公式

**核心思想：所有"m²"或"m"量纲的过滤/聚合参数，都应从 min_feature_m 反推。**

```python
def scale_dependent_params(scale: float, nozzle_mm: float = 0.4) -> dict:
    """从 scale factor 推算所有精度相关参数。"""
    
    min_feature_m = nozzle_mm / scale                       # 1 nozzle 对应的实地距离
    min_printable_m2 = (min_feature_m * 1.2) ** 2           # 1.2x 余量
    
    return {
        # 建筑
        "BUILDING_PRINT_LIMIT_M2": min_printable_m2,        # ≈ 2500 @ 25km/196mm
        "BUILDING_MIN_AREA_M2": min_printable_m2 * 0.01,   # 噪声阈值
        "BUILDING_AGGREGATE_BUFFER_M": min_feature_m * 0.4, # 聚合半径 ≈ 0.4 nozzle 宽
        "BUILDING_AGGREGATE_SIMPLIFY_M": min_feature_m * 0.3,
        "BUILDING_SIMPLIFY_TOL_M": min_feature_m * 0.5,
        
        # 道路
        "ROAD_WIDTH_MULTIPLIER": max(2.0, min(8.0,
            nozzle_mm * 2.5 / (ROAD_WIDTHS["residential"] * scale))),
            # 保证最细道路 ≥ 2.5 nozzle 宽
        
        # 水体
        "WATER_MIN_AREA_M2": min_printable_m2 * 20,        # 水面积阈值更大（视觉需要）
        "WATERWAY_HALF_WIDTH_SCALE": max(1.0, min_feature_m / 50),
            # 线性水体宽度系数（确保河流 ≥ 1 nozzle 宽）
        
        # 植被
        "VEGETATION_MIN_AREA_M2": min_printable_m2 * 2,
        "VEGETATION_SIMPLIFY_TOL_M": min_feature_m * 0.1,
        
        # 砖石纹理
        "brick_corner_r_m": min_feature_m * 0.16,           # 圆角 ≈ 0.16 nozzle
        "brick_shift_m": min_feature_m * 0.16,
        "brick_perlin_amp": min_feature_m * 0.08,
        "brick_resample_m": min_feature_m * 0.24,
        
        # Block Base
        "BLOCK_BASE_MIN_AREA_M2": min_printable_m2 * 0.4,
    }
```

### 13.5 Scale-Independent 参数（不随尺寸变）

| 参数 | 原因 |
|------|------|
| 所有 `*_MM` 厚度/Z 常量 | 物理空间固定，不随真实世界缩放变 |
| Z_GAMMA | 感知量，与比例无关 |
| EXTRUDER_MAP / 颜色 | 打印工艺固定 |
| OSM 标签过滤 | 数据语义固定 |
| NOZZLE_DIAM_MM | 硬件固定 |
| TERRAIN_GRID | 按面积分级，已独立处理 |
| BUILDING_V2_DENSITY_THRESHOLD | 比例量(%)，无量纲 |
| BUILDING_V2_COUNT_THRESHOLD | 计数量，无量纲 |

### 13.6 Model Size → 结构强度约束

模型尺寸变化影响结构完整性：

| model_span_mm | 最小壁厚要求 | TERRAIN_THICKNESS_MM | 原因 |
|---------------|-------------|---------------------|------|
| 80-120 | 2.0mm | 3.0 | 小件易碎，需厚底 |
| 120-200 | 1.5mm | 3.5-4.0 | 标准 |
| 200-350 | 1.0mm | 4.0-5.0 | 大件自重支撑足，可薄 |

### 13.7 预设尺寸组合（产品 SKU）

| SKU | bbox | model_span | 场景 | scale |
|-----|------|-----------|------|-------|
| mini | 3×3 km | 80mm | 钥匙扣/冰箱贴 | 0.027 |
| standard | 12×12 km | 150mm | 桌面摆件 | 0.013 |
| classic | 25×25 km | 196mm | 标准产品（当前） | 0.008 |
| large | 25×25 km | 300mm | 大尺寸摆件 | 0.012 |
| poster | 50×50 km | 350mm | 展示级 | 0.007 |

### 13.8 实现路径

```python
# 新增: _TEXTURE_STYLE_OF_DEEPSEEK/scale_engine.py

@dataclass
class ScaleSpec:
    bbox_area_km2: float
    model_span_mm: float = 196.0    # 默认当前值
    nozzle_mm: float = 0.4
    
    @property
    def scale(self) -> float:
        side_m = (self.bbox_area_km2 ** 0.5) * 1000
        return self.model_span_mm / side_m
    
    @property
    def min_feature_m(self) -> float:
        return self.nozzle_mm / self.scale
    
    def resolve_scale_params(self) -> dict:
        """推算所有 scale-dependent 参数。"""
        ...
    
    def validate(self) -> List[str]:
        """检查是否在合理范围内，返回 warnings。"""
        warnings = []
        if self.min_feature_m > 150:
            warnings.append("精度极低：最小可印特征 > 150m，大部分建筑不可见")
        if self.min_feature_m < 8:
            warnings.append("精度极高：数据量大，建议限制 bbox 面积")
        ...
        return warnings
```

### 13.9 与参数系统集成

```
用户输入: (GPS/城市名, 可选 model_size)
    │
    ▼
ScaleSpec.resolve_scale_params()  ←── 推算精度相关参数
    │
    ▼
CityProfile.detect()              ←── 检测城市特征
    │
    ▼
ParamResolver.resolve(            ←── 合并：scale params + city params + user override
    scale_params,
    city_profile,
    user_overrides
)
    │
    ▼
generate_model()
```

**优先级顺序：** user_override > scale_params > city_adaptive > defaults

### 13.10 边界情况 & 风险

| 场景 | 风险 | 防御 |
|------|------|------|
| mini SKU (80mm) | 建筑/道路全被过滤掉 → 空白 | 强制保留 top-N landmarks 无视面积阈值 |
| poster SKU (350mm) | 数据量爆炸 → GEOS 卡死 | TERRAIN_GRID 上限 + road filter + veg filter |
| 非正方形 bbox (5:1 长条) | 模型变形 / 一侧信息稀疏 | 限制 aspect ratio ≤ 2:1，超出警告 |
| 超小 bbox (< 1km²) | OSM 数据极稀疏 → 空白 | 警告 + 降低所有阈值到极限 |
| 跨时区 / 跨 UTM zone | 坐标变换误差 | 已有 bbox_to_utm 处理 |

---

## 14. 关键设计决策（再补充）

12. **拆件是可选模式** — 默认仍输出单体 3 色 3MF；`--split-print` 切换为拆件输出。不影响 PNG 预览流程。
13. **scale 参数从物理约束反推** — 不允许用户直接设 BUILDING_PRINT_LIMIT_M2 这类中间量；只暴露 bbox + model_size，其余自动算。
14. **min_feature_m 是统一锚点** — 所有"m/m²"量纲的过滤参数都从这一个值派生，保证一致性。
15. **SKU 预设 > 自由输入** — 产品上优先推荐 5 个标准 SKU（mini/standard/classic/large/poster），自定义尺寸作为高级选项。
