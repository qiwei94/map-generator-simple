# CompositionSpec：城市身份构图契约

`CompositionSpec` 是 15/25 km 城市模型的构图审计文件。它回答“哪些道路、水体是
画面的主元素，哪些只用于补足结构密度”，但不保存也不生成几何。

每次 `generate_city_legacy.py` 完成预处理后都会在输出目录写入
`composition_spec.json`；`--review-only` 也会写，因此评审图可以追溯到同一次角色
选择。正式 3MF 的 `design_spec.json` 会保存该文件名、schema 与 policy 版本。

## 固定分工

| 数据/模块 | 权限 | 明确禁止 |
|---|---|---|
| 高德 style-7 mask | 判断道路、水体在标准地图上的相对显著性，重排完整 OSM identity | 生成/描摹替代几何 |
| OSM | 提供道路、水体、建筑的精确源几何与名称/等级/宽度证据 | 因为数据多就全部显示为强前景 |
| 打印 profile | 约束喷嘴最小色条、缝隙、面积和高度 | 决定城市审美主次 |
| 角色选择器 | 输出 `primary`、`secondary`、`connector`、`background` | 控制 mesh 顶点、全局 Z 或布尔 |
| 渲染器/3MF builder | 按同一角色解析物理宽度；评审图可用灰度进一步表达主次 | 绕过打印下限把次级线变成不可打印细线 |
| Block base | 使用更密的 structural 网络保留街区切分和密度 | 要求每条结构线都成为深色材料 |

## 道路角色

- `primary`：在环路保护、道路等级预算或稀疏场景兜底阶段选出的完整城市主轴；
- `secondary`：在空间网格中确实补足空白象限/中心结构的完整走廊；
- `connector`：仅为修复已选骨架短断口而保留的两端连接段；
- `background`：继续参与 topology、block base 与结构缝，但不占高对比道路墨量。

角色只改变可见层级。主路保持既有物理宽度和深色，次路与连接段仅在源宽高于打印
下限时变细；所有角色仍受相同 `min_colored_strip_mm` 硬下限约束。

## 水体角色

- 达到场景占比的大水面直接成为 `primary`，例如海面、湖面、宽江面；
- 没有大水面时，完整候选中跨画幅、转折和高德显著性综合最高的一条成为主走廊；
- 命名护城河等紧凑城市身份围合可作为主元素；
- 其余已选完整河流/运河为 `secondary`，细碎水网只留在结构层。

水体仍使用项目现有统一材料逻辑；角色不会凭空创造河宽。OSM `width`、riverbank
和真实面形优先，高德只确认现有 OSM 走廊是否值得进入前景。

## 验收

审计至少检查：

1. `decision_contract.geometry_authority == "OSM source geometry"`；
2. 文件中没有坐标数组、mesh、Z 或 boolean 指令；
3. 25 km 输出存在可解释的主/次 identity，背景网络仍用于 Block base；
4. 评审 PNG 与正式道路 builder 使用同一个角色化宽度解析函数；
5. 最终仍需真实 3MF 验证器 0 errors / 0 warnings；CompositionSpec 本身不能替代
   打印验收或人的审美判断。

## 2026-08-24 代表场景证据

北京与上海使用相同 25 km 参数、真实本地 PBF、已缓存高德显著性 mask 重算；为把
本轮耗时限定在道路/水体/构图，俯视对照使用平坦 DEM，因此不能作为地形性能或地形
质量证据：

| 场景 | OSM 源道路 | 可见道路 | 主 identity | 水体判定 |
|---|---:|---:|---|---|
| 北京 25 km | 53,715 | 1,126 | 二/三/四/五环、首都机场高速 | 1.05% 大水面 + 筒子河围合为主元素 |
| 上海 25 km | 52,308 | 1,464 | 内环、延安西路、中山北路、华夏西路 | 3.33% 黄浦江面为主元素，3 条完整河廊为次级 |

输出分别位于（`output/` 默认被 Git 忽略）：

- `output/composition_beijing_25km_v3/`；
- `output/composition_shanghai_25km_v1/`。

正式 3MF 另用北京中心 5 km、真实 `N39E116` SRTM 与北京 PBF 生成：

- 文件：`output/composition_beijing_center_5km_real_dem/`
  `full_composition_beijing_center_5km_real_dem_0824_1749.3mf`；
- 10.45 MiB，OSM 道路源 4,370 条，正式可见道路 1,068 条、31,756 road faces；
- `design_spec.json` 保存 artifact SHA-256 与 `composition_spec.json` 引用；
- `tools/validate_3mf.py ... --json`：V1–V13 全通过，`0 errors / 0 warnings`，
  `strict_passed=true`。

一次平坦 DEM 正式试跑得到 V3 一条合理警告（实体 Z range 2 mm，不满足真实地形
4 mm 动态范围）。该结果未被包装成成功，也没有修改验证器规避警告；换真实 SRTM
后重新生成并通过严格验收。
