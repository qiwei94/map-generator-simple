# 手绘砖石风格规范（Hand-Drawn Brick Style Spec）

用户 2026-05-20 制定，**v2 修正**同日。适用于建筑、地标这些"小块状元素"的视觉渲染。

> **v2 修正要点**：
> 1. 圆角 r 改成"极小固定值"（~0.5-1 像素），不再按 sqrt(area) 自适应 — 保证原 polygon 99% 尺寸不变
> 2. 新增 §2 个体微扰层：每块砖整体微旋（±1°）+ 微位移（1-2 像素），由 Perlin 噪声基于中心坐标决定（**相邻砖渐变**）
> 3. 皮肉层振幅从"1-3 像素"细化为"1-2 像素"（≈ 6-12m at 25km/220dpi）

## 核心美学目标

生成一组具有"**匠人手绘感**"与"**砖石砌筑感**"的图形：画面整体应当**精密又错落有致**，像老匠人砌出的石墙 —— 有严谨的排布逻辑，但每一块石头的大小、角度都有微妙的差异。

### 绝对避免（红线）

- ❌ 机器生成的死板感（横平竖直的网格）
- ❌ 完美的几何交汇
- ❌ **任何尖锐的角**（重点！）
- ❌ 微观上的锯齿毛糙（"狗啃边"）

---

## 技术实现路线（三层架构）

### 1. 骨架层：错落排布与圆角化

**目标**：消除尖角与死板排布。

#### 1.0 Voronoi 适用判断（2026-05-20 实践修正）

Voronoi 用于"从无到有生成基础多边形"的场景。**当 polygon 已经由路网+水网切好**（如 city_block，5222 块）或本身就是真实建筑形态（如 landmark），**polygon 自己就是一块砖**，**跳过 Voronoi 切割**，直接走圆角 + 皮肉 + 灵魂三层。

| 几何源 | 是否切 Voronoi |
|---|---|
| city_block（路网+水网切的多边形） | ❌ 不切，block 自己就是砖 |
| landmark（OSM 真实建筑 polygon） | ❌ 不切，建筑自己就是砖 |
| 无几何源、需"砖石填充"覆盖大片空白 | ✅ 撒 Perlin 偏移网格种子 + Voronoi 切 |

工具 `tools/brick_render.py` 的 `--brick-density` 参数：
- `0`（默认）= polygon 自己当砖
- `> 0` = Voronoi 切（在每个 polygon 内撒种子切成 N 块小砖）

下方"Voronoi 几何基础"的规则只在需要切的场景下生效。

#### 1.1 Voronoi 几何基础（仅"需要切"的场景）

| 决策 | 选择 | 理由 |
|---|---|---|
| 几何基础 | **Voronoi 图（泰森多边形）** | 天然砖石错落感 |
| 不用 | Delaunay 三角剖分 | 三角形带尖角 |
| 不用 | 规则矩形网格 | 机器感 |
| 种子点 | **带 Perlin/Simplex 噪声偏移的网格点** | 整体有规律 + 局部有错位 |
| 不用 | 纯随机散布 | 团聚 + 稀疏分布不均 |

**关键步骤 — 圆角化（祛除尖角）**：

**v2 修正**：r = **极小固定米数**（不是 sqrt(area) × ratio），保证原 polygon 99% 尺寸位置不变，效果是"风化磨边"，不是"软化变形"。

```python
# 先外扩 r：尖角变圆弧
poly_round = polygon.buffer(r, resolution=8, join_style=1)  # 1=ROUND
# 再内缩 r：恢复尺寸，圆角保留
poly_round = poly_round.buffer(-r, resolution=8, join_style=1)
```

`r` 取值：~0.5-1 像素。换算 25km bbox / 18inch / 220dpi → 6.3 m/px → **r ≈ 3-6 米**。
工具默认：`--corner-radius-m 4.0`（≈ 0.6 像素）。

### 2. 个体微扰层（v2 新增）：打破矩阵死板感

**目标**：让每块砖整体微旋微移，从"机器对齐方阵"变成"人手砌的略歪斜墙面"。

| 决策 | 选择 | 理由 |
|---|---|---|
| 旋转 | **±1°**，基于 polygon 中心 | 比 ±5° 微妙得多，肉眼说不上但感受得到 |
| 位移 | **1-2 像素**（6-12m at 25km/220dpi）| 同上 |
| 控制源 | **Perlin 噪声（polygon 中心坐标 × 低频）** | **相邻 polygon 扰动渐变** —— 整片墙朝同一个方向歪一点，不是各砖独立乱抖 |
| 不用 | random.uniform | 各砖独立乱抖 → 视觉混乱 |
| 应用顺序 | 圆角化**之后**、提边**之前** | 微扰整 polygon，不影响圆角形态 |

**实现**（`tools/brick_render.py:_individual_perturb`）：

```python
freq = 0.005  # 低频缓变，相邻 polygon 扰动相近
c = polygon.centroid
angle = noise.noise2(c.x*freq + seed, c.y*freq) * rot_deg
dx = noise.noise2(c.x*freq + 100 + seed, c.y*freq) * shift_m
dy = noise.noise2(c.x*freq, c.y*freq + 100 + seed) * shift_m
out = sa.rotate(polygon, angle, origin='centroid')
out = sa.translate(out, xoff=dx, yoff=dy)
```

### 3. 皮肉层：手绘线条渲染

**目标**：消除直线与机器感。

提取圆角多边形的边界线后：

| 决策 | 选择 | 理由 |
|---|---|---|
| 偏移噪声 | **Perlin 或 Simplex 1D 噪声**（`pnoise1` / `opensimplex`） | 连续性保证平滑微弯 |
| 不用 | `random.uniform()` 加坐标 | **狗啃锯齿的根源** |
| 偏移方向 | 垂直于线段方向 | 模拟笔尖横向抖动 |
| 振幅 | **1–2 像素**（≈ 6-12m at 25km/220dpi）| v2 收紧；工具 default `--perlin-amp 8.0`（≈ 1.3 像素）|
| 频率 | 适中 | 每 5–10 像素一个噪声采样 |

**实现伪代码**：
```python
for i, pt in enumerate(line_pts):
    tangent = unit_vec(line_pts[i+1] - line_pts[i-1])
    normal = perpendicular(tangent)
    noise_val = pnoise1(i * 0.1, octaves=1)   # -1..1
    offset = normal * noise_val * amplitude
    new_pt = pt + offset
```

### 4. 灵魂层：线条交汇处理

**目标**：消除完美相交的矢量感。

即使骨架已圆角化，**渲染时也严禁线条完美、严丝合缝地交汇在顶点**。

**留白技巧**：每条边的起点和终点各往内收缩 **3–5% 长度**，留出微小缺口。

```python
def shrink_segment(p0, p1, ratio=0.04):
    """从两端各收缩 ratio 比例，留出灰缝。"""
    v = p1 - p0
    return p0 + v * ratio, p1 - v * (1 - ratio)
```

**类比**：模拟手绘时笔尖无法精准相交的失误感，也像砖块之间的灰缝，让画面有呼吸感。

### 可选进阶

**变化线宽（模拟手绘压感）**：线条粗细随 Perlin 噪声轻微变化（转弯处稍粗，中段稍细）。

---

## 编程语言与库

| 用途 | 库 |
|---|---|
| 语言 | Python |
| 结构生成 | `scipy.spatial.Voronoi` |
| 几何处理 | `shapely.geometry.Polygon` + `.buffer()` 圆角化 |
| 噪声生成 | `noise.pnoise1` 或 `opensimplex` |
| 渲染绘图 | matplotlib (PathCollection) / Pillow / cairo |

## 实施约束

代码中**必须有清晰注释**说明四个核心要求的实现位置：
1. "圆角祛除尖角"（buffer 正负组合，r 极小固定米数）
2. "个体微扰"（每砖微旋微移，Perlin 基于中心 - 相邻渐变）
3. "Perlin 边偏移防锯齿"（不能用 random.uniform）
4. "交汇留白"（每条边端点收缩）

## 应用范围

适用于"小块状元素"：buildings、landmarks、城市 block。**不**适用于：
- 路网线条（railway 已有自己的风格化）
- 水体大面（湖、河应保持光滑边界）
- 植被大色块（保持半透叠加风格）
