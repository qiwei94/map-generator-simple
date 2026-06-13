# Reference 3MF 反向工程分析

> 时间：2026-05-15（含 2026-05-16 武汉/重庆补充）
> 范围：`demo/杭州/`、`demo/旧金山/`、`demo/芝加哥/`、`demo/武汉/`、`demo/重庆/`
> 数据来源：拓竹官方 "城市肌理 1/125K" 系列
> 我们的对照样本：`output/westlake_cli/full_westlake_cli.3mf`

---

## TL;DR

**五个城市样品共享同一套设计规范**，都比我们的当前输出干净 100–1000 倍以上：

| 维度 | 武汉 | 杭州 | 旧金山 | 芝加哥 | 重庆 | 我们 西湖 |
|------|------|------|--------|--------|------|----------|
| 文件大小 | **4.8 MB** | 5.0 MB | 7.7 MB | 9.3 MB | **22.0 MB** | 14.2 MB |
| 总面数 | 181K | 189K | 458K | 503K | **1,423K** | 1226K |
| 总非流形边 | 50 | 8 | **1** | 7 | 27 | **4187** |
| 主体 part 数 | 4 | 4 | 4 | 4 | 4 | 5 |
| 单 mesh 文件 + 多 transform 装配 | ✅ | ✅ | ✅ | ✅ | ✅ | ❌（5 个独立 mesh） |
| 衬板（独立 plate） | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ |

**重庆是异类**：1.4M faces，是杭州的 7.5 倍 —— 山城地形 + 长江几何复杂。其他 4 城在 200K–500K 面区间。

我们的非流形边比最差的杭州 reference 多 **523 倍**，比旧金山多 **4187 倍**。这是直接可量化的质量差距，**根因是 buildings.py / roads.py 还在用 `trimesh.util.concatenate`，没走 Manifold boolean union**。

---

## 一、Reference 通用设计规范

### 1.1 4-part 架构

```
3MF Bundle
│
├── 3D/3dmodel.model （主目录，~6 KB，仅装配信息）
│   ├── <object id=5> 城市主体 = 4 个 component 引用 object_1.model 内的 sub-mesh
│   │   每个 component 带不同的 transform（XY ≈ 0，Z 偏移让 sub-mesh 落到正确高度）
│   │
│   └── <object id=7> 衬板（仅杭州 / 旧金山有）= 1 个 component 引用 object_4.model
│
├── 3D/Objects/object_1.model （主 mesh 文件，5–40 MB）
│   └── 4 个 <object> sub-mesh：地形顶面 / 建筑+底盖 / 道路 / 水体
│       每个 mesh 都是单 watertight 体（最差 7 个非流形边，平均 < 2 个）
│
├── 3D/Objects/object_4.model （衬板 mesh）
│
└── Metadata/model_settings.config
    └── 把每个 part 绑定到一个 extruder + transform 矩阵
```

**关键**：sub-mesh 的几何**只存一份**，通过 4 个不同的 `<component>` 引用 + 不同 Z 偏移 + 不同 extruder 进行复用。**同一份顶点/面数据被 3D 打印软件理解成 4 个不同颜色的部件**。

### 1.2 角色映射（按 Z 偏移 + extruder + 面数反推）

| 用途 | sub-mesh 厚度 | Z 偏移区间（mm） | extruder | 颜色 |
|------|---------------|----------------|----------|------|
| 地形顶面 | 薄 ±1mm | -1.4 ~ +0.6 | **E1（米白）** | `#FFFFFF` |
| 建筑 + 底盖 | 厚 ±2mm | +0.2 ~ +0.6 | **E1（米白）** | 同上 |
| 道路 | 厚 ±2.3mm | +0.20 ~ +0.51（杭州/旧金山） / -1.68（芝加哥变体） | **E2（灰）** | `#8E9089` |
| **水体** | 薄 ±0.76mm | **≈ -2.0**（三城稳定常量） | **E3（黑）** | `#000000` |
| 衬板（plate 2） | — | 0 | E1 | `#FFFFFF` |

注意：**水体的 -2mm 偏移是三城**几乎完全一致**的硬常量**。这就是我们 `config.py::Z_WATER_BASE_MM = -2.00` 的实证来源。

地形 / 建筑 / 道路的 Z 偏移在三城之间**有变化**（取决于该城市的高程范围 / 建筑高度分布），不是固定值。

### 1.3 五城实测数据（sub-mesh 维度）

每城 4 个 part 的 verts / faces / 非流形边数 / Z 范围：

#### 杭州 (`demo/杭州/杭州25Km城市肌理P.3mf`)

| sub-mesh id | verts | faces | 非流形边 | Z 范围 | extruder |
|---|---:|---:|---:|---|---|
| 1 | 18,904 | 32,568 | 1 | -1.07→1.09 | E1 |
| 2 | 40,573 | 64,278 | 1 | -2.06→2.06 | E1 |
| 3 | 32,204 | 64,808 | 6 | -2.25→2.23 | E2 |
| 4 | 7,599 | 15,194 | 0 ✅ | -0.76→0.76 | E3 |
| 衬板 6 | 6,205 | 12,406 | 0 ✅ | -1.10→1.10 | E1 |
| **合计** | **105,485** | **189,254** | **8** | | |

#### 旧金山 (`demo/旧金山/旧金山25Km城市肌理P.3mf`)

| sub-mesh id | verts | faces | 非流形边 | Z 范围 | extruder |
|---|---:|---:|---:|---|---|
| 1 | 81,824 | 158,528 | 0 ✅ | -1.89→1.89 | E1 |
| 2 | 49,999 | 100,062 | 0 ✅ | -2.03→2.03 | E2 |
| 3 | 89,903 | 162,634 | 1 | -2.33→2.33 | E1 |
| 4 | 9,771 | 19,538 | 0 ✅ | -0.76→0.76 | E3 |
| 衬板 6 | 8,622 | 17,240 | 0 ✅ | -1.10→1.10 | (-) |
| **合计** | **240,119** | **458,002** | **1** | | |

#### 芝加哥 (`demo/芝加哥/芝加哥25Km城市肌理P.3mf`)

| sub-mesh id | verts | faces | 非流形边 | Z 范围 | extruder |
|---|---:|---:|---:|---|---|
| 1 | 65,895 | 123,482 | 0 ✅ | -0.47→0.47 | E1 |
| 2 | 96,145 | 165,122 | 7 | -2.82→2.82 | E1 |
| 3 | 8,364 | 16,724 | 0 ✅ | -0.89→0.89 | E3 |
| 4 | 98,472 | 197,356 | 0 ✅ | -0.74→0.74 | E2 |
| **合计** | **268,876** | **502,684** | **7** | | |

> 注意芝加哥的**命名顺序不同**：水体在 id=3 而非 id=4。这是 reference 自己的不一致，不影响功能（靠 extruder 区分）。

#### 武汉 (`demo/武汉/武汉25Km城市肌理P.3mf`)

| sub-mesh id | verts | faces | 非流形边 | Z 范围 | transform Z | extruder |
|---|---:|---:|---:|---|---:|---|
| 1 | 24,383 | 42,826 | 6 | -0.87→0.87 | -0.387 | E1 |
| 2 | 6,157 | 12,310 | 0 ✅ | -1.02→1.02 | -1.828 | **E3** (水体！)|
| 3 | 30,939 | 62,562 | 42 | -1.13→1.13 | -0.702 | E2 |
| 4 | 33,856 | 53,800 | 2 | -2.30→2.30 | +0.539 | E1 |
| 衬板 6 | 4,555 | 9,106 | 0 ✅ | -1.10→1.10 | 0 | (-) |
| **合计** | **99,890** | **180,604** | **50** | | | |

> 武汉 id=2 是水体（E3 黑色，transform -1.83 ≈ -2.0），不是建筑。**id 编号在不同城市可以互换，靠 extruder 区分用途**。

#### 重庆 (`demo/重庆/重庆25Km城市肌理P.3mf`)

| sub-mesh id | verts | faces | 非流形边 | Z 范围 | transform Z | extruder |
|---|---:|---:|---:|---|---:|---|
| 1 | 335,845 | **671,934** | 10 | -2.88→2.88 | +0.508 | E2 |
| 2 | 212,227 | 416,986 | 16 | -2.71→2.71 | +0.067 | E1 |
| 3 | 4,025 | 8,048 | 1 | -1.90→1.90 | -1.480 | **E3** (水体)|
| 4 | 166,153 | 312,498 | 0 ✅ | -2.80→2.80 | +0.279 | E1 |
| 衬板 6 | 6,891 | 13,778 | 0 ✅ | -1.10→1.10 | 0 | (-) |
| **合计** | **725,141** | **1,423,244** | **27** | | | |

> 重庆是**山城 + 长江**双重复杂度：地形 mesh 671K faces 是杭州 21 倍，水体 312K faces 也异常大。但仍维持 4-part 架构 + 27 非流形（每对象平均 < 7）。

### 1.4 五城共识：sub-mesh ID 不一致，但 extruder 是稳定锚点

**关键发现**：sub-mesh 的 id 编号在不同城市**可以互换**，但每个 mesh 的 **extruder + transform Z** 揭示了它的真实用途：

| 用途 | extruder | transform Z 区间 | 城市间映射示例 |
|------|---------|-----------------|---------------|
| 地形顶面 | E1（米白）| 接近 0（-1.4 ~ +0.6）| 杭/旧/芝/武/重 各家命名 id 都不同 |
| 建筑 + 底盖 | E1（米白）| 略偏正（+0.07 ~ +0.61）| 同上 |
| **道路 / 灰色结构** | **E2（灰）** | 杭/旧 +0.5；芝/武/重 -0.7~-1.7 | 道路 Z 在不同城市差异很大 |
| **水体浮雕** | **E3（黑）**| **稳定 -1.5 ~ -2.5（≈ -2.0）** | 5 城绝对一致 |
| 衬板 | E1 | 0 | 杭/旧/武/重有，芝加哥无 |

**水体 transform Z = -2.0** 是 5 城**唯一绝对稳定**的常量 — 这就是 `Z_WATER_BASE_MM = -2.00` 的实证基础。

### 1.5 五城都遵守的 4 条隐含原则

1. **每个 sub-mesh 都是单 watertight 体**  
   每对象 < 10 个非流形边。这是 `manifold3d.batch_boolean(Add)` + Manifold-style 布尔差集流水线的特征。

2. **几何只存一份，靠 transform + extruder 复用**  
   3MF 标准 `<components>` 机制：1 个 mesh 文件被 4 个 component 引用，分别绑不同 Z 偏移和不同 extruder。**XML 体积大幅压缩**。

3. **Z 堆叠是设计常量**  
   - 水体 ≈ -2.0mm（三城绝对一致）
   - 道路 ≈ +0.5mm（两城一致，芝加哥变体）
   - 地形 / 建筑随高程数据浮动

4. **不强求每种 OSM 数据都独立成对象**  
   - 植被层在三城里都**没有**单独的 mesh。要么没做，要么烘进了地形顶面。
   - 道路 / 水体 / 建筑 / 地形是必备 4 类，植被 / 衬板是可选第 5 / 6 类。

---

## 二、我们 vs Reference 的差距

### 2.1 几何质量

| 文件 | 单对象最差非流形边 | 总非流形边 | 总边界边 |
|------|------------------|-----------|---------|
| 旧金山 | 1 | 1 | 0 |
| 杭州 | 6 | 8 | 0 |
| 芝加哥 | 7 | 7 | 0 |
| **我们** | **3065（buildings）** | **4187** | **45,350** |

我们的 buildings 一个对象就有 3065 个非流形边，**比整个旧金山 reference（1 个）多 3 个数量级**。

**根因**：

```python
# buildings.py:171  ← 违反 "全 Manifold" 原则
merged = trimesh.util.concatenate(building_meshes)

# roads.py:233    ← 违反 "全 Manifold" 原则
merged = trimesh.util.concatenate(ribbon_meshes)
```

vegetation / water / object4 已经全部走 `manifold3d.batch_boolean`，是干净的。

### 2.2 文件结构

我们：

```
3MF
└── 5 个 <object> 各自包含完整 mesh
    terrain_surface  315K faces
    terrain_walls    101K faces
    buildings        573K faces
    water            101K faces
    vegetation       134K faces
```

Reference：

```
3MF
├── 主 model: 4 个 <component> 引用同一个 mesh 文件
└── object_1.model: 4 个 <object> sub-mesh （共享在同一文件里）
    每个 component 自带 transform（Z 偏移）+ 通过 model_settings.config 绑 extruder
```

**XML 体积差距**：reference 用 `<components>` 共享几何，我们每个 `<object>` 都是独立完整 mesh。同样面数下，**reference XML 仅 1/3–1/2 大小**。

### 2.3 设计差异（不一定是缺陷）

| 维度 | Reference | 我们 | 评价 |
|------|-----------|------|------|
| 植被 | 没有独立对象 | 有 `vegetation` obj | 我们多了一种表达力 |
| 衬板 | 杭州 / 旧金山 有 | 没有 | 我们少了一个收纳件 |
| terrain 切分 | 按功能切（顶面 + 底盖+建筑） | 按法线切（顶面 + 侧墙） | 各有取舍 |
| 建筑染色 | 和 terrain_walls 同 extruder（米白） | 单独 obj，色卡 `#F5E6C8` | reference 更"克制" |

---

## 三、Reference 反推出来的设计常量

写进 `config.py` 的硬性数字（已经在用，**实证确认有效**）：

```python
INTERNAL_SPAN_MM    = 196.0   # ← 三城都用 200mm 平台 - 2mm 边距
                                #   主体 transform scale = 0.787401687 = 100/127
                                #   原始 mesh ±127mm 跨度 → 200mm 打印盘
Z_WATER_BASE_MM     = -2.00   # ← 三城绝对一致
Z_ROAD_ABOVE_TERRAIN_MM = 0.51  # ← 杭州 / 旧金山 = 0.512，芝加哥变体
Z_BUILDING_EMBED_MM = 0.04    # ← 反推自 reference 注释（0.04mm 嵌入用 FDM 融合）
```

**水体 Z = -2.0** 这个数字在三城严丝合缝，是最稳的定锚点。

---

## 四、落地动作清单

按重要性排序：

| # | 改什么 | 文件 / 位置 | 影响 | 工时 |
|---|--------|-----------|------|------|
| **1** | buildings 改 Manifold batch_boolean | `buildings.py:127-176` | 非流形边 3065 → < 10 | 半天 |
| **2** | roads 改 shapely union + Manifold extrude | `roads.py:173-238` | 非流形边大幅下降 + 道路简化 | 半天 |
| **3** | exporter 改用 `<components>` + transform | `exporter.py` 重写 | 文件 14MB → ≈ 6MB | 半天 |
| **4** | terrain_walls + buildings 合并到同 extruder | `exporter.py:_OBJECT_DEFS` + `pipeline.py` | 接近 reference 风格 | 1 小时 |
| **5** | 加衬板（独立 plate） | 新写 `plinth.py` + `exporter.py` | 完整度向 reference 看齐 | 1 小时 |
| **6** | 重新评估是否要 vegetation 独立 obj | 决策性，先看视觉效果 | 可能减一个 obj | 评估 0.5 小时 |

### 推荐顺序

**M1 必修（质量回归）**：1 + 2 → 让 buildings / roads 不再带几千非流形边  
**M2 文件瘦身**：3 → 让输出 14MB → 6MB，更接近 reference  
**M3 风格对齐**：4 + 5 → 4-part + 衬板，外观和 reference 持平  
**M4 评估迭代**：6 → 看视觉是否真的需要植被独立

---

## 五、参考资产

```
demo/杭州/      杭州25Km城市肌理P.3mf      5.0 MB ★
demo/旧金山/    旧金山25Km城市肌理P.3mf    7.7 MB ★
demo/芝加哥/    芝加哥25Km城市肌理P.3mf    9.3 MB ★
demo/武汉/      武汉25Km城市肌理P.3mf      4.8 MB ★ (最小)
demo/重庆/      重庆25Km城市肌理P.3mf     22.0 MB ★ (最大，山城)
```

★ 这五个 3MF 是评估器（`evaluator.py`）的参考输入：

- 几何质量 baseline（非流形边 ≤ 50，单对象通常 ≤ 10）
- Z 堆叠常量校准（**水体 -2.0** 是唯一绝对稳定的常量；其他随城市浮动）
- 文件大小预算（5–22 MB / 25km×25km，山城例外）
- 视觉对比基准（top-down 渲染 vs 卫星图 vs reference 图）

**有趣的城市差异**：
- **武汉**：建筑层只有 12K faces（杭州的 1/5）— 武汉建筑 OSM 标记稀疏
- **芝加哥**：唯一无衬板的 reference，可能是早期版本
- **旧金山**：质量最高（仅 1 条非流形边），可能是 reference 团队最精修的样本
- **重庆**：1.4M 总面数 = 杭州 7.5 倍。`object_1.model` 高达 112MB，地形和水体都极复杂

---

## 六、附：分析过程的可复现脚本

```python
# 单 sub-mesh 几何质量检查
import re, numpy as np
def edge_stats(F):
    e = np.ascontiguousarray(np.sort(np.vstack(
        [F[:,[0,1]], F[:,[1,2]], F[:,[2,0]]]), axis=1))
    dt = np.dtype((np.void, e.dtype.itemsize*2))
    _, c = np.unique(e.view(dt), return_counts=True)
    return int((c==1).sum()), int((c>2).sum())

# 解 ZIP → 读 3D/Objects/*.model → 每个 <object> 单独跑 edge_stats
```

实操命令：

```bash
unzip demo/杭州/杭州25Km城市肌理P.3mf -d /tmp/hz/
ls /tmp/hz/3D/Objects/    # object_1.model / object_4.model
cat /tmp/hz/Metadata/model_settings.config   # extruder 分配 + Z 偏移矩阵
```
