# Manifold布尔运算实现规格说明书

> **版本**: v1.0
> **日期**: 2026-05-05
> **状态**: Ready for Implementation
> **前置验证**: 水体挤出Manifold替换已测试成功 ✅

---

## 一、背景与核心结论

### 1.1 OSM数据模型确认

**关键发现**：OpenStreetMap建筑高度是**相对于地面**的，不是海拔高度。

| 标签 | 测量起点 | 示例值 | 含义 |
|------|---------|-------|------|
| `height` | **地面** | 50m | 建筑从地面向上50m |
| `ele` | **海平面** | 50m | 地面海拔50m |
| **建筑顶部海拔** | 需计算 | 100m | `ele + height` |

**影响**：当前项目的建筑嵌入逻辑正确，无需修改。

---

### 1.2 参考模型分析（文档第9-144行）

参考模型（芝加哥/杭州）包含**5个独立对象**：

```
对象1: 底板+水体（底板上有水体浮雕）
对象2: 地形+道路+水体镂空（地形融合道路，镂空水体）
对象3: 植被-水体（植被板镂空水体区域）
对象4: 建筑（嵌入地形）
对象5: (可选) 单独的道路层
```

**关键工艺问题**：
- ✅ Q1: 水体挤出 → Manifold布尔并集解决
- ⚠️ Q2: 地形镂空水体 → 需Manifold布尔差集
- ⚠️ Q3: 植被镂空水体 → 需Manifold布尔差集
- ⚠️ Q4: 道路融合地形 → 需Manifold布尔并集
- ⚠️ Q5: 建筑自适应方案 → 需地形分类算法

---

## 二、对象遮挡关系处理

### 2.1 遮挡关系矩阵

**核心原则**：上层对象（Z轴高度更高）应镂空被遮挡的下层对象，确保XY平面无重叠冲突。

#### Z轴高度层次（从低到高）

```
Z轴结构（模型空间）：
├─ 水体层：Z=-2.0mm → 0.5mm（底板+水体浮雕）
├─ 地形层：Z=-0.17mm → 2.61mm（厚度约4mm）
├─ 植被层：地形表面 + 0.1mm
├─ 道路层：地形表面 + 0.51mm
└─ 建筑层：地形表面 - 0.04mm → +5mm（嵌入+暴露）
```

#### XY平面遮挡关系表

| 遮挡关系 | 上层对象 | 下层对象 | 是否遮挡 | 处理方式 | 实现优先级 |
|---------|---------|---------|---------|---------|----------|
| **建筑 vs 植被** | 建筑（+5mm） | 植被（+0.1mm） | ✅ 是 | 植被布尔差集建筑区域 | 🔴 **P0** |
| **建筑 vs 水体** | 建筑 | 水体 | ✅ 是 | 建筑数据过滤水体区域 | 🔴 **P0** |
| **道路 vs 植被** | 道路（+0.51mm） | 植被（+0.1mm） | ✅ 是 | 植被布尔差集道路区域 | 🟡 P1 |
| **植被 vs 水体** | 植被 | 水体 | ✅ 是 | 植被布尔差集水体区域 | 🔴 **P0** |
| **建筑 vs 道路** | 建筑（+5mm） | 道路（+0.51mm） | ✅ 是 | 建筑数据过滤主要道路 | 🟡 P1 |
| **道路 vs 水体** | 道路 | 水体 | ⚠️ **特殊** | 桥梁保留，其他道路避开水体 | 🟢 P2 |

---

### 2.2 遮挡处理策略

#### 策略1：数据预处理过滤（建筑）

**适用场景**：建筑不应建在水体或主要道路上。

```python
def filter_buildings_conflicts(buildings_gdf, water_gdf, roads_gdf):
    """过滤建筑数据，移除与水体、道路冲突的建筑。

    过滤规则：
    1. 建筑与水体重叠 > 30% → 排除该建筑
    2. 建筑与主要道路重叠 > 50% → 排除该建筑

    Returns:
        filtered_buildings_gdf: 过滤后的建筑数据
    """
    # 创建水体union
    water_union = unary_union(water_gdf.geometry)

    # 创建主要道路union（仅motorway/trunk/primary）
    major_roads = roads_gdf[roads_gdf['highway'].isin(['motorway', 'trunk', 'primary'])]
    roads_union = unary_union([road.buffer(width/2) for road in major_roads])

    # 过滤逻辑
    filtered_buildings = []
    for _, building in buildings_gdf.iterrows():
        footprint = building.geometry

        # 检查水体冲突
        if footprint.intersects(water_union):
            overlap_ratio = footprint.intersection(water_union).area / footprint.area
            if overlap_ratio > 0.3:  # 重叠>30%，排除
                continue

        # 检查道路冲突
        if roads_union and footprint.intersects(roads_union):
            overlap_ratio = footprint.intersection(roads_union).area / footprint.area
            if overlap_ratio > 0.5:  # 重叠>50%，排除
                continue

        filtered_buildings.append(building)

    return gpd.GeoDataFrame(filtered_buildings, crs=buildings_gdf.crs)
```

---

#### 策略2：布尔差集镂空（植被）

**适用场景**：植被不应覆盖建筑、道路、水体。

```python
def build_vegetation_with_exclusions(vegetation_gdf, buildings_gdf, roads_gdf,
                                      water_gdf, terrain_mesh, scale):
    """构建植被层，镂空建筑、道路、水体区域。

    布尔运算顺序：
    vegetation ⊖ (buildings ⊎ roads ⊎ water)

    Returns:
        vegetation_final: trimesh.Trimesh（watertight，已镂空）
    """
    # Step 1: 基础植被网格
    vegetation_mesh = build_deepseek_vegetation(vegetation_gdf, terrain_mesh, scale)
    vegetation_m = trimesh_to_manifold(vegetation_mesh)

    # Step 2: 创建建筑排除柱（用于镂空）
    building_columns_m = []
    for building in buildings_gdf:
        terrain_z = sample_terrain_z(terrain_mesh, building.centroid.x * scale,
                                      building.centroid.y * scale)
        building_col = extrude_exclusion_column(
            building.geometry,
            z_bottom=terrain_z - 0.5,  # 略低于植被
            z_top=terrain_z + building_height_mm + 1.0
        )
        building_columns_m.append(trimesh_to_manifold(building_col))

    # Step 3: 创建道路排除柱（用于镂空）
    roads_columns_m = []
    for road in roads_gdf:
        road_col = extrude_road_exclusion_column(
            road.geometry,
            width=road_width_m,
            z_bottom=terrain_z - 0.5,
            z_top=terrain_z + 0.51 + 0.4 + 1.0  # 道路层高度
        )
        roads_columns_m.append(trimesh_to_manifold(road_col))

    # Step 4: 创建水体排除柱
    water_columns_m = create_water_exclusion_columns(water_gdf, terrain_mesh)

    # Step 5: 合并所有排除区域（布尔并集）
    exclusion_union_m = manifold3d.Manifold()
    for col_m in building_columns_m:
        exclusion_union_m = exclusion_union_m.union(col_m)
    for col_m in roads_columns_m:
        exclusion_union_m = exclusion_union_m.union(col_m)
    for col_m in water_columns_m:
        exclusion_union_m = exclusion_union_m.union(col_m)

    # Step 6: 植被布尔差集（镂空）
    vegetation_final_m = vegetation_m - exclusion_union_m

    # Step 7: 转回trimesh并验证
    vegetation_final = manifold_to_trimesh(vegetation_final_m)
    print(f"[植被] 镂空后watertight: {vegetation_final.is_watertight}")

    return vegetation_final
```

---

### 2.3 特殊情况处理：桥梁

**道路 vs 水体的特殊关系**：

- **桥梁**：道路跨越水体，应保留（不避开水体）
- **普通道路**：道路在水体旁，应避开水体（但这种情况OSM数据中较少）

**处理策略**：

```python
def filter_roads_for_bridges(roads_gdf, water_gdf):
    """识别并保留桥梁，其他道路保持原样。

    识别规则：
    1. road标签包含 `bridge=yes` → 保留桥梁
    2. 其他道路 → 保持原样（OSM数据通常已正确处理）
    """
    # 标记桥梁
    roads_gdf['is_bridge'] = roads_gdf.get('bridge', '') == 'yes'

    # 桥梁保持原样，其他道路无需特殊处理
    print(f"[道路] 桥梁数量: {roads_gdf['is_bridge'].sum()}")

    return roads_gdf
```

---

## 三、Manifold布尔运算方案

### 3.1 对象1：底板+水体

**当前问题**：
- 简单合并可能导致网格不watertight
- 水体和底板之间可能有缝隙

**Manifold方案**：**布尔并集**

```python
# 输入
base_plate = _build_base_plate(...)           # 底板网格
water_feature = _extrude_water_feature(...)   # 水体网格

# Manifold布尔运算
base_plate_m = trimesh_to_manifold(base_plate)
water_m = trimesh_to_manifold(water_feature)
water_plate_m = base_plate_m.union(water_m)   # 布尔并集

# 输出
water_plate = manifold_to_trimesh(water_plate_m)
# watertight: True ✅
```

**验证状态**: ✅ 已在另一窗口测试成功

---

### 2.2 对象4：地形+道路+水体镂空

**这是最复杂的对象，需要3步布尔运算**。

#### Step 1: 地形重建（基础）

```python
terrain_mesh = build_deepseek_terrain(elevation_grid, ...)
terrain_m = trimesh_to_manifold(terrain_mesh)
```

#### Step 2: 道路布尔并集

```python
roads_mesh = build_deepseek_roads(roads_gdf, terrain_mesh, ...)
roads_m = trimesh_to_manifold(roads_mesh)

# 布尔并集：地形 + 道路
terrain_with_roads_m = terrain_m.union(roads_m)
```

#### Step 3: 水体布尔差集（镂空）

```python
# 创建水体挤出柱（关键：从地形底部到地形顶部）
water_column = _extrude_water_column_for_cutting(
    water_polygon,
    z_bottom=Z_TERRAIN_BASE,      # -0.17mm（地形底部）
    z_top=terrain_z_max + 1.0     # 略高于地形，确保完全穿透
)
water_column_m = trimesh_to_manifold(water_column)

# 布尔差集：地形+道路 - 水体 = 镂空地形
terrain_with_holes_m = terrain_with_roads_m - water_column_m

# 输出
terrain_final = manifold_to_trimesh(terrain_with_holes_m)
# watertight: True ✅
# 水体镂空完美契合对象1 ✅
```

**关键实现**：新增 `_extrude_water_column_for_cutting()` 函数

---

### 2.3 对象3：植被-水体

**当前问题**：
- 植被会覆盖水体区域（不符合参考模型）
- 参考模型中植被镂空了河流部分（文档第59行）

**Manifold方案**：**布尔差集**

```python
# 输入
vegetation_mesh = build_deepseek_vegetation(vegetation_gdf, ...)
vegetation_m = trimesh_to_manifold(vegetation_mesh)

# 创建水体挤出柱（用于切割）
water_column = _extrude_water_column_for_cutting(
    water_polygon,
    z_bottom=vegetation_z - 1.0,  # 略低于植被
    z_top=vegetation_z + 1.0      # 略高于植被
)
water_column_m = trimesh_to_manifold(water_column)

# 布尔差集：植被 - 水体
vegetation_without_water_m = vegetation_m - water_column_m

# 输出
vegetation_final = manifold_to_trimesh(vegetation_without_water_m)
# watertight: True ✅
# 植被镂空水体区域 ✅
```

---

### 2.4 对象5：建筑自适应融合

**自动化策略**：根据地形分类选择方案。

#### 地形分类算法

```python
def classify_terrain_type(elevation_grid):
    """自动分类地形类型。

    Returns:
        "flat"     → 方案A/C（布尔并集/嵌入法）
        "mountain" → 方案B（布尔交集）
        "moderate" → 混合方案（局部自适应）
    """
    z_range = np.nanmax(elevation_grid) - np.nanmin(elevation_grid)
    z_std = np.nanstd(elevation_grid)

    # 阈值（基于真实城市案例）
    if z_range < 30 and z_std < 10:
        return "flat"        # 如芝加哥（高程差5-10m）
    elif z_range > 100 or z_std > 30:
        return "mountain"    # 如杭州（高程差100-200m）
    else:
        return "moderate"    # 如深圳、北京
```

#### 方案A/C：平坦地形（布尔并集）

```python
# 建筑从地形表面向上挤出
z_bottom = terrain_z - 0.04mm  # 嵌入深度
z_top = terrain_z + building_height_mm

building_mesh = _extrude_building(footprint, z_bottom, z_top)
building_m = trimesh_to_manifold(building_mesh)
terrain_m = trimesh_to_manifold(terrain_mesh)

# 布尔并集：建筑叠加在地形上
fused_m = terrain_m.union(building_m)
# 结果：建筑暴露在地形上方 ✅
```

#### 方案B：山地地形（布尔交集）

```python
# 建筑柱体从地形底部到建筑顶部
z_bottom = Z_TERRAIN_BASE        # -0.17mm
z_top = terrain_z + building_height_mm

building_column = _extrude_building(footprint, z_bottom, z_top)
building_m = trimesh_to_manifold(building_column)
terrain_m = trimesh_to_manifold(terrain_mesh)

# 布尔交集：只保留建筑在地形内的部分
fused_m = terrain_m.intersection(building_m)
# 结果：建筑完美贴合地形轮廓 ✅
```

#### 混合方案：局部自适应

```python
# 计算每个建筑所在位置的局部坡度
slope_grid = compute_slope_grid(elevation_grid)

for building in buildings_gdf:
    centroid = building.geometry.centroid
    local_slope = sample_slope_at_point(slope_grid, centroid.x, centroid.y)

    terrain_z = sample_terrain_z(terrain_mesh, centroid.x * scale, centroid.y * scale)

    if local_slope > 0.05:  # 坡度 > 5%（山坡建筑）
        # 使用方案B（布尔交集）
        building_mesh = build_building_intersection(footprint, terrain_z, building_height)
    else:  # 平坦区域建筑
        # 使用方案A/C（布尔并集）
        building_mesh = build_building_union(footprint, terrain_z, building_height)

    building_meshes.append(building_mesh)
```

---

## 三、Manifold布尔运算对照表

| 对象 | 原实现 | Manifold方法 | 布尔运算类型 | 目的 | 实现难度 |
|------|--------|-------------|-------------|------|---------|
| **对象1** 底板+水体 | 简单合并 | `union()` | **并集** | 确保watertight融合 | ⭐ 简单 |
| **对象4** 地形+道路 | 独立ribbon | `union()` | **并集** | 道路融入地形 | ⭐⭐ 中等 |
| **对象4** 地形-水体 | 未实现 | `-` 或 `difference()` | **差集** | 水体镂空 | ⭐⭐⭐ 复杂 |
| **对象3** 植被-水体 | 未镂空 | `-` 或 `difference()` | **差集** | 植被镂空水体 | ⭐⭐ 中等 |
| **对象5** 建筑(平坦) | 嵌入法 | `union()` | **并集** | 建筑叠加 | ⭐ 简单 |
| **对象5** 建筑(山地) | 不适用 | `intersection()` | **交集** | 完美贴合地形 | ⭐⭐⭐ 复杂 |

---

## 四、完整流水线架构

```
Stage 0: 数据获取 + 比例计算
    ↓
Stage 1: 并行重建基础网格
    ├─ 水体网格（water_gdf）
    ├─ 地形网格（elevation_grid）+ 地形分类
    ├─ 道路网格（roads_gdf）
    ├─ 植被网格（vegetation_gdf）
    └─ 建筑网格（buildings_gdf）
    ↓
Stage 2: Manifold布尔运算流水线
    ├─ 对象1: base_plate ⊎ water → water_plate (✅已验证)
    ├─ 对象4: terrain ⊎ roads ⊖ water → terrain_final
    ├─ 对象3: vegetation ⊖ water → vegetation_final
    └─ 对象5: buildings (adaptive) → buildings_final
    ↓
Stage 3: 导出5个独立3MF文件
    ├─ water_plate.3mf (挤出机3, #3C96DC)
    ├─ terrain_final.3mf (挤出机1, #C8B48C)
    ├─ vegetation_final.3mf (挤出机4, #6B8E23)
    ├─ buildings_final.3mf (挤出机1, #F5E6C8)
    └─ roads.3mf (挤出机2, #5A5A5A) ← 仅对象4未融合道路时使用
```

---

## 五、实现优先级

### Phase 1: 高优先级（核心功能）

**优先级排序依据**：
1. 对参考模型还原度的影响
2. 对打印质量的影响
3. 实现难度

| 任务 | 优先级 | 原因 | 预计工作量 |
|------|--------|------|-----------|
| **对象4水体镂空** | 🔴 P0 | 参考模型核心特征，影响装配契合度 | 4小时 |
| **地形分类算法** | 🔴 P0 | 对象5前置依赖，自动化必需 | 2小时 |
| **对象3植被镂空** | 🟡 P1 | 参考模型特征，实现简单 | 3小时 |

### Phase 2: 中优先级（增强功能）

| 任务 | 优先级 | 原因 | 预计工作量 |
|------|--------|------|-----------|
| **对象4道路融合** | 🟢 P2 | 参考模型中道路已融入（文档第119行） | 4小时 |
| **对象5山地方案** | 🟢 P2 | 仅杭州等山地城市需要 | 6小时 |
| **对象1优化** | ⚪ P3 | 当前实现已足够，Manifold为增强 | 2小时 |

---

## 六、新增函数清单

### 6.1 核心布尔运算函数

```python
# 文件: _TEXTURE_STYLE_OF_DEEPSEEK/boolean_ops.py (新建)

def boolean_union(mesh_a, mesh_b, name="union"):
    """Manifold布尔并集"""
    pass

def boolean_difference(mesh_a, mesh_b, name="difference"):
    """Manifold布尔差集"""
    pass

def boolean_intersection(mesh_a, mesh_b, name="intersection"):
    """Manifold布尔交集"""
    pass
```

### 6.2 辅助几何函数

```python
# 文件: _TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/processors/water_column.py (新建)

def extrude_water_column_for_cutting(water_polygon, z_bottom, z_top):
    """创建水体挤出柱，用于布尔差集切割"""
    pass

def extrude_building_column(footprint, z_bottom, z_top):
    """创建建筑挤出柱，用于布尔交集"""
    pass
```

### 6.3 地形分类函数

```python
# 文件: _TEXTURE_STYLE_OF_DEEPSEEK/terrain_classification.py (新建)

def classify_terrain_type(elevation_grid):
    """自动分类地形类型"""
    pass

def compute_slope_grid(elevation_grid, cell_size_m):
    """计算坡度网格"""
    pass

def sample_slope_at_point(slope_grid, x, y):
    """采样指定点的坡度"""
    pass
```

---

## 七、城市案例验证表

| 城市 | 地形类型 | 高程范围 | 标准差 | 平均坡度 | 推荐建筑方案 |
|------|---------|---------|---------|---------|-------------|
| **芝加哥** | `flat` | 5-10m | 2-3m | <0.05 | 方案A/C（并集） |
| **杭州** | `mountain` | 100-200m | 40-50m | >0.10 | 方案B（交集） |
| **北京** | `moderate` | 30-50m | 10-15m | 0.03-0.05 | 混合方案 |
| **深圳** | `moderate` | 30-80m | 15-25m | 0.05-0.08 | 混合方案 |
| **重庆** | `mountain` | 200-400m | 80-100m | >0.15 | 方案B（交集） |

---

## 八、测试计划

### 8.1 单元测试

```python
# 文件: tests/test_manifold_boolean.py

def test_water_union():
    """测试对象1：底板+水体布尔并集"""
    pass

def test_terrain_difference():
    """测试对象4：地形-水体布尔差集"""
    pass

def test_vegetation_difference():
    """测试对象3：植被-水体布尔差集"""
    pass

def test_building_adaptive():
    """测试对象5：建筑自适应方案"""
    pass
```

### 8.2 集成测试

```python
def test_chicago_pipeline():
    """完整流水线测试：芝加哥（平坦地形）"""
    pass

def test_hangzhou_pipeline():
    """完整流水线测试：杭州（山地地形）"""
    pass
```

---

## 九、风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Manifold布尔运算失败 | 网格不watertight | 回退到trimesh-native修复 |
| 地形分类阈值不准确 | 建筑方案选择错误 | 提供手动override选项 |
| 大规模网格布尔运算慢 | 性能瓶颈 | 分区域并行处理 |
| 水体镂空后地形太薄 | 打印质量问题 | 设置最小厚度阈值 |

---

## 十、下一步行动

### 立即可执行（基于验证结果）

1. ✅ **对象1已验证** → 无需修改（已在另一窗口测试）
2. 🔴 **实现对象4水体镂空** → 优先级最高
3. 🔴 **实现地形分类算法** → 对象5前置依赖

### 建议执行顺序

```
Week 1:
  ├─ 实现地形分类算法 (2h)
  ├─ 实现水体挤出柱函数 (2h)
  └─ 实现对象4水体镂空 (4h)

Week 2:
  ├─ 实现对象3植被镂空 (3h)
  ├─ 实现对象4道路融合 (4h)
  └─ 测试芝加哥完整流水线 (3h)

Week 3:
  ├─ 实现对象5山地方案 (6h)
  ├─ 测试杭州完整流水线 (3h)
  └─ 性能优化和调优 (4h)
```

---

## 附录：代码示例

### A.1 对象4完整实现

```python
def build_terrain_with_water_holes(elevation_grid, water_gdf, roads_gdf,
                                   width_m, height_m, area_km2, scale):
    """构建地形+道路融合+水体镂空（对象4）。

    Returns:
        terrain_final: trimesh.Trimesh (watertight)
    """
    # Step 1: 地形重建
    terrain_mesh = build_deepseek_terrain(elevation_grid, width_m, height_m,
                                          area_km2, scale)
    terrain_m = trimesh_to_manifold(terrain_mesh)

    # Step 2: 道路融合（可选）
    if roads_gdf is not None and len(roads_gdf) > 0:
        roads_mesh = build_deepseek_roads(roads_gdf, terrain_mesh, area_km2, scale)
        if roads_mesh is not None:
            roads_m = trimesh_to_manifold(roads_mesh)
            terrain_m = terrain_m.union(roads_m)
            print(f"[terrain] 道路融合完成，faces={len(terrain_m.to_mesh().tri_verts)}")

    # Step 3: 水体镂空
    if water_gdf is not None and len(water_gdf) > 0:
        terrain_z_max = terrain_mesh.vertices[:, 2].max()

        for _, row in water_gdf.iterrows():
            water_polygon = row.geometry
            if water_polygon.is_empty:
                continue

            # 创建水体挤出柱
            water_column = extrude_water_column_for_cutting(
                water_polygon,
                z_bottom=Z_TERRAIN_BASE - 0.5,  # 略低于地形底部
                z_top=terrain_z_max + 0.5       # 略高于地形顶部
            )
            water_column_m = trimesh_to_manifold(water_column)

            # 布尔差集
            terrain_m = terrain_m - water_column_m
            print(f"[terrain] 水体镂空完成")

    # Step 4: 转回trimesh并验证
    terrain_final = manifold_to_trimesh(terrain_m)
    print(f"[terrain] 最终网格: watertight={terrain_final.is_watertight}")

    return terrain_final
```

---

## 十一、验证和确认流程

### 11.1 增量式验证原则

**参考**: most_important_doc.md 第142行
> "每个对象要单独能生成3mf文件，以便确认是否符合预期"

**核心流程**：
```
对象X → 实现 → 导出3MF → 自动验证 → 生成验证文档 → 用户确认 ✅ → 继续对象X+1
```

### 11.2 验证文档规范

完整的验证流程详见：[validation_workflow_spec.md](validation_workflow_spec.md)

**关键要素**：
1. 每个对象完成后生成独立的验证文档（Markdown格式）
2. 验证文档包含：网格质量检查、几何特征验证、问题清单、确认清单
3. 用户必须填写确认清单后才能继续下一对象

### 11.3 验证文档目录结构

```
output/[city_name]/
├─ water_plate_[city].3mf
├─ terrain_final_[city].3mf
├─ vegetation_final_[city].3mf
├─ buildings_final_[city].3mf
├─ validation_water_plate_[city].md     ← 对象1验证文档
├─ validation_terrain_final_[city].md   ← 对象4验证文档
├─ validation_vegetation_final_[city].md ← 对象3验证文档
└─ validation_buildings_final_[city].md  ← 对象5验证文档
```

### 11.4 用户确认清单模板

每个验证文档包含以下确认清单：

```markdown
请用户确认以下内容：

- [ ] 模型质量: 3MF文件能正常打开，网格无明显错误
- [ ] 几何特征: 符合参考模型的特征描述
- [ ] 尺寸比例: 与预期比例尺一致
- [ ] 遮挡处理: 正确处理了与其他对象的遮挡关系
- [ ] 打印可行性: 可以进行3D打印（如需要）

用户反馈: [待填写]
确认状态: [待确认 → 已确认/需要修改]
下一步: [继续下一对象/修改当前对象]
```

### 11.5 实施顺序（含验证步骤）

```
Week 1:
  ├─ 实现对象1: 底板+水体 (✅已验证)
  ├─ 实现地形分类算法 (2h)
  ├─ 实现水体挤出柱函数 (2h)
  ├─ 实现对象4: 地形+道路+水体镂空 (4h)
  └─ ⏸️ 生成验证文档，等待用户确认 ✅

Week 2:
  ├─ 实现对象3: 植被镂空（含建筑+道路遮挡） (3h)
  ├─ ⏸️ 生成验证文档，等待用户确认 ✅
  ├─ 实现对象4道路融合优化 (4h)
  └─ 测试芝加哥完整流水线 (3h)

Week 3:
  ├─ 实现对象5: 建筑自适应方案 (6h)
  ├─ ⏸️ 生成验证文档，等待用户确认 ✅
  ├─ 测试杭州完整流水线 (3h)
  └─ 最终装配和性能优化 (4h)
```

**注意**：每个对象完成后必须等待用户确认，不得跳过验证步骤。

---

**文档结束**

> **总结**：本spec基于OSM数据模型验证、参考模型分析、对象1实测成功、以及增量式验证流程，为后续4个对象的Manifold布尔运算提供了完整的实现蓝图。所有对象必须通过用户验证确认后方可继续。