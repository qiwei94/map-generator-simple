# 高密度街区四城实验：纽约、洛杉矶、罗马、雅典

## 实验问题与边界

验证当前 C 街区组织是否更适合连续、高覆盖的城市街区。不能预先把四座城市
都归为同一种高密度场景，也不能把行政区 PBF 缺失、水面、自然用地当作建筑稀疏。

- 四城名义范围均为 25 km；经纬度方框投影后的实际面积约 628 km²，非精确 625 km²。
- 同一实验代码，C 建筑支撑街区 + 源道路身份恢复 + 负空间道路 + 真实桥面。
- 普通道路全缝宽 0.28 mm；主干/桥梁 0.42 mm。不是 demo 实测宽度。
- 仅输出平面 PNG，不改生产默认策略、不生成 3MF/GLB、不上线画廊。
- 生成进程禁用网络，使用已有 PBF、DEM。归档数据不是当日最新数据。
- 源足迹面积采用求和口径，可能含 building:part 重叠；独立楼栋数不能直接由记录数推断。

## 输入与节点

| 城市 | 中心经纬度（纬度、经度） | 计算节点 | 数据 |
| --- | --- | --- | --- |
| 纽约 | 40.7128, -74.0060 | Windows / Ubuntu-24.04 WSL2 | 已有 New York 州 PBF 的完整 ways 裁片；西岸 New Jersey 覆盖缺失 |
| 罗马 | 41.9028, 12.4964 | controller M1 Pro | 本地 centro-latest.osm.pbf；已有 SRTM N41E012、N42E012 |
| 雅典 | 37.9750, 23.7300 | Intel Mac | 云端已有 Greece PBF → 本机 native osmium 裁片；N37E023、N38E023 |
| 洛杉矶 | 34.0522, -118.2437 | controller M1 Pro | 云端已有 California PBF；N33W119、N34W119 |

本轮先按 map-cluster-ops 只读探测资源、任务、代码和缓存。
没有在 cloud-data 上安排模型生成，没有重启或部署公网服务。
数据节点原生 osmium 实际是 Python 兼容包装：首轮裁切超时，后续 libosmium
位置索引方案在约 1.9 GB RAM 节点被 OOM 终止。进程已退出、原数据未动，
不再在此节点重试。改为把归档 Greece（337,200,333 字节）、California
（1,312,366,643 字节）复制到本机裁切。这是国内归档传输，不是新海外下载。
失败输出保留且不用于生成。cloud-api 后续 SSH 超时，未做重启恢复尝试。

本轮没有修改生成算法。Intel 独立实验目录同步了上一轮已验证的 coastal 入口和
空间分块 PNG 收尾；最终关键文件哈希与 controller、Windows 相同：

- generate_city_legacy.py：`f02da1925ade8c029f024b624dc44b7ec626096abc27d6d69cf83649b9fd9365`
- aesthetic/bridge_sources.py：`4aca9a1d9b71b0ef73d6fb56b788fbd59c10d933b496981395deb22bed095d10`
- tools/render_negative_city_png.py：`5421777f12821e2e1b229db902c439e4d241438804df56370b642085b347e2d4`

原始代码包和上一轮完整环境说明见 [四城实验记录](four_city_negative_png_20260905.md)。
不能仅用 Git HEAD 复现未提交的实验代码。

## 中间结果（持续更新）

| 城市 | 源建筑记录数 | 中心 5 km 足迹求和占比 | 外围占比 | 最终普通城市面覆盖全框 | 实际图片检查 |
| --- | ---: | ---: | ---: | ---: | --- |
| 罗马 | 143,104 | 34.6% | 8.9% | 35.1% | 中心较饱满、外围零散；南段台伯河面不连续，不可发布 |
| 纽约 | 519,920 | 20.4% | 14.0% | 29.4% | 曼哈顿格网清楚；新泽西西岸缺失并误填成水，不可发布 |
| 雅典 | 368,159 | 41.3% | 10.5% | 28.1% | 城区与海湾清楚，部分街区偏实偏大块，待人工审美评价 |
| 洛杉矶 | 待完成 | — | — | — | 归档已校验，正在本机运行 |

中心/外围分母均含水面、绿地，不代表建成区净密度。纽约缺失源覆盖，不能用该
整图指标与其他城市做公平排名；曼哈顿等局部只能用于初步视觉观察。

密度测量脚本：`tmp/density_four_20260905/measure_density.py`。
它校验 S5 SHA256，测量源建筑足迹、中心/外围、1 km 网格分布以及最终城市面；
不改变任何生成阈值。1 km 单元按建筑质心归属，不是精确栅格 union 覆盖。

## 当前产物

- [罗马完整 PNG](../output/rome_C25_density_20260905_v2/negative_bridges/city_topdown.png)
- [罗马测量报告](../output/rome_C25_density_20260905_v2/density_measurements.json)
- [纽约完整 PNG（明确缺陷样本）](../output/density_four_cities_20260906/new_york.png)
- [纽约测量报告](../output/density_four_cities_20260906/new_york_density.json)
- [雅典完整 PNG](../output/density_four_cities_20260906/athens.png)
- [雅典测量报告](../output/density_four_cities_20260906/athens_density.json)
- Windows 原始目录：`/home/mapworker/map-four-cities-20260905/output/new_york_C25_density_20260905`
- Intel 原始目录：`/Users/zhangqiwei/map-four-cities-20260905/output/athens_C25_density_20260906`

罗马 PNG 收尾 69.49 秒，纽约 50.30 秒，均不包含取数、S0–S5 和 S6。
所有几何报告的生成成功都不等于人工验图通过；本轮不作地形高度、切片可打印性结论。

罗马生成日志内总计约 352.1 秒（review-only 282.6 + 最终负空间 PNG 69.49），
纽约约 822.5 秒（772.2 + 50.30；其中 S6 479.79）。不同数据量、缓存与硬件，
不能把这些数字当作节点性能横向基准。

雅典 S0–S7 review-only 日志耗时 1,537.6 秒（含 S6 982.6 秒），之后负空间 PNG
收尾 123.34 秒，总生成约 27.7 分钟，不含前期数据准备。雅典裁片检查 ways 所需
nodes 缺失数为 0；不代表所有 relations 的成员都完整。

原始归档 SHA256（复制前后核对一致）：

- Greece：`4415a6113e2596a4f65f4c2a4643e39c0c0265cf14127228df07f08cbbf839ec`
- California：`d0114c2e1025df50a9452e5024b47713083dcc3c6968545a275d8d3ce4a78d90`

## 继续检查的位置

洛杉矶已进入 S6，分配源建筑 740,915 条到 4,111 街区，其中 1,735 个进入 C
支撑处理。当前本机进程 PID 74539；日志为 `tmp/density_four_20260905/los_angeles.log`。
执行脚本 `tmp/density_four_20260905/run_los_angeles.sh` 在生成结束后会自动运行密度测量。
结果目录 `output/los_angeles_C25_density_20260906/negative_bridges/`；在出现最终 PNG、
report.json 且哈希核验通过之前，不得标记为完成。

本轮保留原始截图式 PNG 和失败样本，没有为了画面好看裁掉缺失区域，
也没有补画不存在于源数据中的城市或河面。未授权发布；所有结果仍属实验。
