# 对象实现验证流程规格

> **目的**: 确保每个对象实现质量符合预期后再继续下一对象
> **方法**: 增量式验证 + 用户确认
> **参考**: most_important_doc.md 第142行："每个对象要单独能生成3mf文件，以便确认是否符合预期"

---

## 一、验证流程总览

### 1.1 增量式开发流程

```
对象1 → 实现 → 生成验证文档 → 用户确认 ✅ → 继续对象2
对象2 → 实现 → 生成验证文档 → 用户确认 ✅ → 继续对象3
对象3 → 实现 → 生成验证文档 → 用户确认 ✅ → 继续对象4
对象4 → 实现 → 生成验证文档 → 用户确认 ✅ → 继续对象5
对象5 → 实现 → 生成验证文档 → 用户确认 ✅ → 最终装配
```

**原则**：
- ✅ 每个对象独立实现、独立验证
- ✅ 用户确认后才能继续下一对象
- ✅ 发现问题立即修复，避免累积错误

---

## 二、验证文档模板

### 2.1 标准验证文档结构

每个对象完成后，生成如下结构的验证文档：

```markdown
# 对象X验证报告 - [城市名称]

> **实现日期**: YYYY-MM-DD
> **Manifold方法**: [布尔运算类型]
> **验证状态**: [待确认/已确认]

---

## 一、实现方案

### 1.1 布尔运算逻辑

[描述使用的Manifold布尔运算类型和具体实现]

### 1.2 关键参数

| 参数名 | 值 | 说明 |
|--------|---|------|
| ... | ... | ... |

### 1.3 代码实现

[关键代码片段]

---

## 二、生成模型验证

### 2.1 3MF文件信息

- **文件路径**: `output/[object_name].3mf`
- **文件大小**: XX MB
- **顶点数**: XXXXX
- **面数**: XXXXX
- **Watertight**: [True/False]

### 2.2 网格质量检查

| 检查项 | 结果 | 是否通过 |
|--------|------|---------|
| Watertight | [True/False] | ✅/❌ |
| Manifold edges | [数量] | ✅/❌ |
| Degenerate faces | [数量] | ✅/❌ |
| Normal consistency | [True/False] | ✅/❌ |

### 2.3 几何特征验证

[具体描述该对象的几何特征是否符合预期]

---

## 三、参考模型对比

### 3.1 对比项清单

| 对比项 | 参考模型特征 | 当前实现 | 是否符合 |
|--------|-------------|---------|---------|
| [特征1] | [描述] | [描述] | ✅/❌ |
| [特征2] | [描述] | [描述] | ✅/❌ |

### 3.2 可视化对比

[插入模型截图、对比图]

---

## 四、问题与疑问

### 4.1 实现过程中的问题

[列出实现过程中遇到的问题和解决方案]

### 4.2 待确认的疑问

[列出需要用户确认的疑问]

---

## 五、确认清单

### 用户确认项

请用户确认以下内容：

- [ ] **模型质量**: 3MF文件能正常打开，网格无明显错误
- [ ] **几何特征**: 符合参考模型的特征描述
- [ ] **尺寸比例**: 与预期比例尺一致
- [ ] **遮挡处理**: 正确处理了与其他对象的遮挡关系
- [ ] **打印可行性**: 可以进行3D打印（如需要）

### 确认结果

**用户反馈**: [待填写]

**确认状态**: [待确认 → 已确认/需要修改]

**下一步**: [继续下一对象/修改当前对象]
```

---

## 三、各对象的具体验证标准

### 3.1 对象1：底板+水体

#### 验证清单

| 验证项 | 标准 | 检查方法 |
|--------|------|---------|
| **Watertight** | 必须为True | `mesh.is_watertight` |
| **水体浮雕** | 水体高于底板0.5mm | 可视化检查Z轴高度 |
| **边界融合** | 水体与底板无缝隙 | 检查边界edges数量 |
| **小水体过滤** | 面积<阈值的水体被排除 | 检查水体数量 |

#### 问题清单（参考most_important_doc.md第135-138行）

1. 水体雕刻高度如何确定？
2. 小水体过滤阈值是否合理？

#### 参考模型对比项

- 芝加哥：底板+河流浮雕（文档第65-68行）
- 杭州：底板+钱塘江/西湖浮雕（文档第105-108行）

---

### 3.2 对象4：地形+道路+水体镂空

#### 验证清单

| 验证项 | 标准 | 检查方法 |
|--------|------|---------|
| **Watertight** | 必须为True（镂空后仍封闭） | `mesh.is_watertight` |
| **水体镂空** | 水体区域完全镂空 | 与对象1嵌合检查 |
| **道路融合** | 道路融入地形表面 | 检查道路网格边界 |
| **地形起伏** | 高程数据正确渲染 | 可视化检查地形起伏 |

#### 问题清单（参考most_important_doc.md第139-141行）

1. 镂空时如何封闭mesh？
2. 道路融合开销是否合理？
3. 镂空后与对象1能否完美嵌合？

#### 参考模型对比项

- 芝加哥：地形起伏+河流镂空（文档第70-73行）
- 杭州：山体+钱塘江镂空+桥梁（文档第112-119行）

---

### 3.3 对象3：植被-水体（含遮挡处理）

#### 验证清单

| 验证项 | 标准 | 检查方法 |
|--------|------|---------|
| **Watertight** | 必须为True | `mesh.is_watertight` |
| **水体镂空** | 植被不覆盖水体区域 | 与水体数据对比 |
| **建筑镂空** | 植被不覆盖建筑区域 | 与建筑数据对比 |
| **道路镂空** | 植被不覆盖主要道路 | 与道路数据对比 |
| **Z轴位置** | 植被在地形表面+0.1mm | 检查Z轴高度 |

#### 问题清单

1. 植被镂空是否正确处理所有遮挡关系？
2. 植被厚度是否合理（0.2mm）？

#### 参考模型对比项

- 芝加哥：公园植被+河流镂空（文档第53-61行）
- 杭州：西溪湿地+植被分布（文档第95-101行）

---

### 3.4 对象5：建筑（含遮挡处理）

#### 验证清单

| 验证项 | 标准 | 检查方法 |
|--------|------|---------|
| **Watertight** | 必须为True（或方案B交集结果） | `mesh.is_watertight` |
| **地形分类** | 正确识别地形类型 | 检查分类算法输出 |
| **建筑过滤** | 正确排除水体/道路区域 | 检查建筑数量变化 |
| **建筑高度** | 高度范围3.0-5.3mm | 检查Z轴高度分布 |
| **嵌入深度** | 嵌入地形0.04mm（方案A） | 检查建筑底部Z值 |

#### 问题清单（参考most_important_doc.md第80行）

1. 建筑与地形Z轴关系是否正确？
2. 山地地形建筑是否完美贴合？

#### 参考模型对比项

- 芝加哥：建筑嵌入地形（文档第77-80行）
- 杭州：建筑分布+嵌入（文档第122-126行）

---

## 四、验证自动化工具

### 4.1 验证脚本示例

```python
def validate_object_mesh(mesh: trimesh.Trimesh, object_name: str) -> dict:
    """自动验证对象网格质量。

    Returns:
        {
            "watertight": bool,
            "manifold_edges": int,
            "degenerate_faces": int,
            "normal_consistency": bool,
            "volume": float,
            "bounds": tuple,
        }
    """
    result = {
        "watertight": mesh.is_watertight,
        "manifold_edges": len(mesh.edges_unique),
        "degenerate_faces": len(mesh.degenerate_faces()),
        "normal_consistency": len(mesh.faces) > 0,
        "volume": mesh.volume if mesh.is_watertight else 0,
        "bounds": mesh.bounds,
    }

    # 打印验证结果
    print(f"\n[{object_name}] 验证结果:")
    print(f"  Watertight: {result['watertight']} {'✅' if result['watertight'] else '❌'}")
    print(f"  顶点数: {len(mesh.vertices)}")
    print(f"  面数: {len(mesh.faces)}")
    print(f"  Degenerate faces: {result['degenerate_faces']} {'✅' if result['degenerate_faces'] == 0 else '❌'}")
    print(f"  Volume: {result['volume']:.2f} mm³")
    print(f"  Z范围: {mesh.bounds[0][2]:.2f} → {mesh.bounds[1][2]:.2f} mm")

    return result


def generate_validation_report(mesh, object_name, city_name, output_dir):
    """生成验证文档。

    Args:
        mesh: trimesh对象
        object_name: "water_plate" | "terrain_final" | "vegetation_final" | "buildings_final"
        city_name: 城市名称
        output_dir: 输出目录

    Returns:
        验证文档路径
    """
    import datetime

    # 验证网格
    validation_result = validate_object_mesh(mesh, object_name)

    # 生成Markdown报告
    report_path = f"{output_dir}/validation_{object_name}_{city_name}.md"

    with open(report_path, 'w') as f:
        f.write(f"# {object_name}验证报告 - {city_name}\n\n")
        f.write(f"> **实现日期**: {datetime.datetime.now().strftime('%Y-%m-%d')}\n")
        f.write(f"> **验证状态**: 待确认\n\n")
        f.write("---\n\n")
        f.write("## 一、网格质量检查\n\n")
        f.write("| 检查项 | 结果 | 是否通过 |\n")
        f.write("|--------|------|---------|\n")
        f.write(f"| Watertight | {validation_result['watertight']} | {'✅' if validation_result['watertight'] else '❌'} |\n")
        f.write(f"| 顶点数 | {len(mesh.vertices)} | ✅ |\n")
        f.write(f"| 面数 | {len(mesh.faces)} | ✅ |\n")
        f.write(f"| Degenerate faces | {validation_result['degenerate_faces']} | {'✅' if validation_result['degenerate_faces'] == 0 else '❌'} |\n")
        f.write(f"| Volume | {validation_result['volume']:.2f} mm³ | ✅ |\n")
        f.write("\n---\n\n")
        f.write("## 二、确认清单\n\n")
        f.write("请用户确认以下内容：\n\n")
        f.write("- [ ] 模型质量: 3MF文件能正常打开，网格无明显错误\n")
        f.write("- [ ] 几何特征: 符合参考模型的特征描述\n")
        f.write("- [ ] 尺寸比例: 与预期比例尺一致\n")
        f.write("- [ ] 遮挡处理: 正确处理了与其他对象的遮挡关系\n")
        f.write("\n---\n\n")
        f.write("**用户反馈**: [待填写]\n\n")
        f.write("**确认状态**: 待确认\n\n")

    print(f"[验证文档] 已生成: {report_path}")
    return report_path
```

---

### 4.2 验证流程集成

```python
def implement_object_with_validation(object_name, city_name, data, output_dir):
    """实现对象并自动生成验证文档。

    流程：
    1. 实现对象（Manifold布尔运算）
    2. 导出3MF文件
    3. 自动验证网格质量
    4. 生成验证文档
    5. 等待用户确认

    Returns:
        (mesh, validation_report_path)
    """
    print(f"\n{'='*60}")
    print(f"  实现对象: {object_name}")
    print(f"{'='*60}")

    # Step 1: 实现对象
    if object_name == "water_plate":
        mesh = build_water_plate_manifold(data['water_gdf'], data['bbox'], data['scale'])
    elif object_name == "terrain_final":
        mesh = build_terrain_with_holes_manifold(data['elevation'], data['water_gdf'],
                                                  data['roads_gdf'], data['scale'])
    elif object_name == "vegetation_final":
        mesh = build_vegetation_with_exclusions_manifold(data['vegetation_gdf'],
                                                          data['buildings_gdf'],
                                                          data['roads_gdf'],
                                                          data['water_gdf'],
                                                          data['terrain_mesh'],
                                                          data['scale'])
    elif object_name == "buildings_final":
        mesh = build_buildings_adaptive_manifold(data['buildings_gdf'],
                                                  data['terrain_mesh'],
                                                  data['elevation'],
                                                  data['scale'])
    else:
        raise ValueError(f"Unknown object: {object_name}")

    # Step 2: 导出3MF
    output_path = f"{output_dir}/{object_name}_{city_name}.3mf"
    export_deepseek_3mf({object_name: mesh}, output_path)
    print(f"[导出] {output_path}")

    # Step 3: 自动验证
    validation_result = validate_object_mesh(mesh, object_name)

    # Step 4: 生成验证文档
    report_path = generate_validation_report(mesh, object_name, city_name, output_dir)

    # Step 5: 提示用户确认
    print(f"\n{'='*60}")
    print(f"  请确认对象: {object_name}")
    print(f"{'='*60}")
    print(f"  1. 打开3MF文件: {output_path}")
    print(f"  2. 检查验证文档: {report_path}")
    print(f"  3. 确认后输入 'continue' 继续下一对象")
    print(f"  4. 如有问题输入 'modify' 修改当前对象")
    print(f"{'='*60}\n")

    return mesh, report_path
```

---

## 五、实施流程示例

### 5.1 完整实施脚本

```python
# 文件: implement_objects_incremental.py

def run_incremental_pipeline(city_name, bbox):
    """增量式实施流水线。

    每个对象完成后等待用户确认。
    """
    # Stage 0: 数据获取
    data = fetch_all_data(bbox)

    # Stage 1: 数据预处理
    data['buildings_gdf'] = filter_buildings_conflicts(
        data['buildings_gdf'], data['water_gdf'], data['roads_gdf']
    )

    output_dir = f"output/{city_name}"
    os.makedirs(output_dir, exist_ok=True)

    # Stage 2: 增量式实现（每个对象完成后等待确认）
    objects_sequence = [
        "water_plate",      # 对象1
        "terrain_final",    # 对象4
        "vegetation_final", # 对象3
        "buildings_final",  # 对象5
    ]

    completed_objects = {}

    for object_name in objects_sequence:
        # 实现对象 + 自动验证 + 生成文档
        mesh, report_path = implement_object_with_validation(
            object_name, city_name, data, output_dir
        )

        # 等待用户确认
        while True:
            user_input = input("用户确认状态 (continue/modify): ").strip().lower()

            if user_input == 'continue':
                print(f"[确认] {object_name} 已通过验证，继续下一对象")
                completed_objects[object_name] = mesh
                break
            elif user_input == 'modify':
                print(f"[修改] {object_name} 需要修改，请提供修改意见")
                feedback = input("修改意见: ")
                # 根据反馈修改实现
                # ... 修改逻辑 ...
                # 重新生成验证文档
                mesh, report_path = implement_object_with_validation(
                    object_name, city_name, data, output_dir
                )
            else:
                print(f"无效输入，请输入 'continue' 或 'modify'")

    # Stage 3: 最终装配（所有对象确认后）
    print(f"\n{'='*60}")
    print(f"  所有对象已确认完成")
    print(f"{'='*60}")

    # 生成装配后的完整模型
    final_mesh = assemble_all_objects(completed_objects)

    return completed_objects
```

---

## 六、验证文档目录结构

```
output/
└─ [city_name]/
   ├─ water_plate_[city].3mf
   ├─ terrain_final_[city].3mf
   ├─ vegetation_final_[city].3mf
   ├─ buildings_final_[city].3mf
   ├─ validation_water_plate_[city].md     ← 对象1验证文档
   ├─ validation_terrain_final_[city].md   ← 对象4验证文档
   ├─ validation_vegetation_final_[city].md ← 对象3验证文档
   └─ validation_buildings_final_[city].md  ← 对象5验证文档
```

---

## 七、用户确认流程图

```
┌─────────────────────┐
│  实现对象X           │
│  (Manifold布尔运算)  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  导出3MF文件         │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  自动验证网格质量     │
│  (watertight/体积)   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  生成验证文档         │
│  (Markdown格式)      │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  用户打开3MF文件      │
│  检查模型质量         │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  用户填写验证文档     │
│  (确认清单)          │
└──────────┬──────────┘
           │
           ▼
      ┌────┴────┐
      │  用户确认 │
      └────┬────┘
           │
    ┌──────┴──────┐
    │             │
   ✅            ❌
继续下一对象     修改当前对象
```

---

**文档结束**

> **核心原则**: 量实现 + 增量验证 + 用户确认 = 确保质量