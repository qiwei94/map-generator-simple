# 手绘 / 插画感艺术处理技巧清单

地图 PNG 增加手绘 / 插画感的技巧汇总。配合 [project-layered-design-philosophy](../) 的"内部肌理差异化"思路 —— 不靠多换 filament 槽位，而靠每层内部的视觉处理制造层次。

参考工具：`tools/block_polygonize_viz.py`（block 视觉诊断），主渲染：`tools/tune_buildings_v2.py:render()`。

---

## 项目里已经实现的

| 技巧 | 代码位置 | 效果 |
|---|---|---|
| Edge jitter / wobble | `tools/tune_buildings_v2.py:_jitter_polygon` | 边 resample + 法向 ±N 米随机偏移，polygon 边缘"抖动" |
| Low-poly shading | `tools/tune_buildings_v2.py:_draw_jittered_block_layer` 里调 `_lowpoly_triangles_in` | 每块 earcut 切三角，每三角随机 alpha 0–10% 阴影，水彩/折纸感 |
| Variable line width | `tools/tune_buildings_v2.py:1179` (railway) | `lw * uniform(0.85, 1.15)`，线宽 ±15% 随机 |
| Z-jitter（3MF only） | sub-mesh Z 基线 | 同类元素 Z 高度微变，避免大锅饭 |

---

## 待尝试（按性价比排序）

### A. matplotlib 原生 — 低成本高收益

#### A1. Hatching（剖面线 / 排线）
- **效果**：用 `//` `\\` `++` `..` 等平行/交叉/点阵代替实色填充，钢笔画 / 版画感
- **实现**：matplotlib `PathPatch(hatch='//', edgecolor=..., facecolor='none')`
- **注意**：matplotlib hatch 分辨率受 dpi 影响，大图细密度可能糊

#### A2. Drop shadow offset（投影 / 贴纸感）
- **效果**：每块下方偏移深色，像贴纸卡片
- **实现**：同 polygon 画两遍 —— 第一遍 `translate(+1mm, -1mm)` 偏深色（如 `#a8a08c`），第二遍正常颜色覆盖
- **优势**：matplotlib 原生，几乎零开销

#### A3. Double stroke（双线描边 / 漫画感）
- **效果**：边线画两遍，外层粗 + 浅色，内层细 + 深色 → Moebius / 漫画分镜
- **实现**：`LineCollection` 跑两遍不同 lw + color

---

### B. PIL / scipy 后处理 — 中成本高收益

#### B1. Paper texture multiply（纸张纹理）
- **效果**：底纹质感（牛皮纸 / 水彩纸 / 老地图）
- **实现**：savefig 后 PIL multiply blend，叠一张 1024×1024 纸纹 PNG（可循环平铺）
- **资源**：搜 "paper texture seamless PNG free"

#### B2. Speckle / film grain（颗粒噪声）
- **效果**：老照片 / 印刷感
- **实现**：`PIL.ImageFilter` 加 gaussian noise，或 `np.random.normal` 灰度噪声 + alpha 3-8% 叠加

#### B3. Watercolor edge bleed（水彩边缘羽化）
- **效果**：边缘溢出渐变，水彩晕染
- **实现**：每个 polygon 的 alpha mask 用 `PIL.ImageFilter.GaussianBlur(radius=3-8)` 羽化后 paste 回去
- **复杂度**：每块独立处理，大量 block 会慢

#### B4. Roughen filter（边缘细碎化）
- **效果**：比 jitter 更密更细的"打毛"边缘
- **实现**：类似 `_jitter_polygon` 但 `segment_m=2-4`（更密插点） + `jitter_m=0.5-1`（更小偏移）
- **参考**：Inkscape Filter → Distort → Roughen

#### B5. Vignette（四角暗角）
- **效果**：老地图 / 摄影感
- **实现**：savefig 后 PIL radial alpha overlay，中心 0 → 四角 0.3

---

### C. 高级算法 — 高成本但视觉差异大

#### C1. Hachure（Rough.js 风手绘填充）
- **效果**：每块用 N 条带 jitter 的平行短线填充代替实色 → 经典手绘地图（Stamen Watercolor 风）
- **实现**：每 polygon 内画 N 条角度固定的平行线（line spacing 5-10m），每条加 ±10% 抖动 + 端点延展
- **参考**：[rough.js](https://roughjs.com/) 算法，[sketchviz](https://sketchviz.com/) 风格

#### C2. Stipple shading（点画）
- **效果**：素描点画，密度变化表达明暗
- **实现**：Poisson disk sampling 在 polygon 内撒点 + matplotlib scatter
- **库**：`from scipy.spatial import KDTree`，自己实现 ~50 行；或用 `bridson` package

#### C3. Sumi-e ink wash（水墨）
- **效果**：边缘深→中心透明，水墨晕染感
- **实现**：每个 polygon 做 radial alpha mask（外深内浅）→ PIL paste

#### C4. Reduced palette / posterize（限色版画）
- **效果**：限定 6-8 色，risograph 印刷感
- **实现**：savefig 后 `PIL.ImageOps.posterize(img, bits=2)` 或 sklearn k-means 量化
- **配色**：先固化 4-6 色 palette，再量化

#### C5. Sobel sketch overlay（铅笔素描叠加）
- **效果**：整图黑色线条 overlay，铅笔素描
- **实现**：`scipy.ndimage.sobel(img.mean(axis=2))` 检测边缘 → 反相 → 半透叠加

#### C6. Voronoi micro-color（印章质感）
- **效果**：每个 Voronoi cell ±5% 色相微差
- **实现**：`scipy.spatial.Voronoi` 在 polygon 内撒种子 + 每 cell 给独立色
- **风险**：cell 数量大时性能差

---

## 复合配方（经典风格组合）

| 风格 | 组合 |
|---|---|
| **Stamen Watercolor 风** | hatching + jitter + drop shadow + paper texture |
| **Risograph 印刷感** | posterize + speckle + double stroke |
| **水彩** | watercolor bleed + paper texture + vignette |
| **铅笔素描** | sobel overlay + speckle + reduced palette |
| **手绘地图（当前已有 + 1 step）** | jitter + lowpoly + hatching |

---

## 实施建议

1. **一次只加一种**：跟现有 `lowpoly + jitter` 叠加测试，不要一次堆 5 种以上，视觉会过载
2. **CLI 单 toggle**：每个新效果做单独 flag（`--hatch` / `--paper` / `--grain` / `--vignette`）方便单独验证
3. **性能预算**：
   - A 类（matplotlib 原生）：几乎免费
   - B 类（PIL 后处理）：每张 ~1-2s
   - C 类（scipy / Voronoi / Stipple）：westlake 量级几秒；chicago 量级（64920 city_blocks）小心 — Voronoi / Stipple 常数大，可能到几十秒
4. **先在 `tools/block_polygonize_viz.py` 上 prototype**：诊断工具，迭代快；满意后再考虑迁入 `tune_buildings_v2.py` 主渲染
5. **保存中间产物**：复合配方下，每步存一张 PNG 方便回退对比
