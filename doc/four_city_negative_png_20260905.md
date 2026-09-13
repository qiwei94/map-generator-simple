# 四城完整 PNG 实验（2026-09-05）

## 范围与参数

用户要求：巴黎完整图，随后西湖、芝加哥、新加坡；当前只做 PNG。
使用现有约 25 km 母范围，C 街区组织、源道路身份连续性、真实桥面。
普通道路全缝宽 **0.28 mm**，主干/桥梁 **0.42 mm**；没有改 PrinterProfile 默认值。
这不是参考 demo 实测匹配宽度，也不是正式 S0–S11/3MF 可打印性验收。
巴黎复用已校验 S5/C；其 manifest 中 `paris_organization_A_...` 是原记录任务名，实际读取的是 `C/layers.pkl`，不是 A 输出。

## 结果与判定

| 城市 | 节点 | PNG 收尾耗时 | 结果 |
| --- | --- | ---: | --- |
| 巴黎 | controller M1 Pro | 161.42 s | 完整图已生成；河湾与桥面可见，待人工评价粒度 |
| 芝加哥 | Windows Ubuntu-24.04 WSL2 | 57.85 s | 完整图已生成；湖面与格网可见，待人工评价 |
| 西湖 | Intel Mac | 79.19 s | 完整图已生成但 **rerun**：城市填充明显稀疏 |
| 新加坡 | Windows Ubuntu-24.04 WSL2 | 约 126 s | 海岸线接线修复后完整图已生成，海面恢复；待人工评价 |

以上仅为复用 C 街区结果后的 PNG 收尾，不含取数、预处理和 S6。
四图的源桥线水上部分在桥面几何中的缺失长度均为 0 m；它不等同于所有道路/水网已经逐项通过验收。

- [四城预览](../output/four_cities_negative_20260905/overview.png)
- [中文验图汇总及文件哈希](../output/four_cities_negative_20260905/review_summary.json)
- [巴黎原图](../output/paris_negative_bridges_25km_20260905_v4/city_topdown.png)
- [芝加哥原图](../output/four_cities_negative_20260905/chicago/chicago-negative-v3.png)
- [西湖失败对照图](../output/four_cities_negative_20260905/westlake/city_topdown.png)
- [新加坡原图](../output/four_cities_negative_20260905/singapore/singapore-coastal-v2.png)

## 本轮修复与证据

1. 全城道路身份匹配原先反复对整张路网缓冲，改为 STRtree 邻域查询。支撑比例 15%、支撑长度 30 m、匹配容差 0.05 m 不变；不新增虚构连接线。
2. 原来的巨型城市面合并阻塞超过十分钟。改为约 4 km 非重叠计算单元：源面先裁单元再合并；道路查询带 halo 且保留完整线，避免单元边界截断。最终仍是完整 25 km 图，未降低分辨率。保存前处理/C 的耗时不再重复支付。
3. 西湖源桥线有很短的折返，GEOS 的平头整线缓冲遗漏自身 0.1366 m；芝加哥旧收尾也报告 0.3467 m 缺失。对缓冲不能覆盖自身的源线，仅补入各真实线段等宽矩形，不外推端点、不放宽失败阈值；裁切和桥面使用相同走廊。
4. 新加坡首次 PNG 海面缺失。`generate_city_legacy.py` 的 CLI 入口未调用已有 `materialize_coastal_water`，只传入海岸线而非海面。现在在 S2 测量与缓存/决策之前转换，并从该入口离线重跑到 C/PNG；没有事后涂黑，也没有海外下载。

回归：道路身份支撑与旧全局缓冲等价的夹具；空间分块与整幅裁切等价；短折返桥自覆盖；直桥端点与宽度保持；海岸方向、岛屿留洞、内陆误标不淹没；canonical 入口及共享城市面。共 **50 项通过**，仅已有 Pillow 弃用警告。

## 西湖不得作为合格样品

这次精确 bbox 未命中 Intel 节点高德引导缓存，离线运行没有下载回退。
C 实验又只在源建筑支撑距离内填充街区，实际 PNG 大面积城市覆盖不足。
缺失缓存是确定事实；它对稀疏程度的独立贡献尚未做受控实验，不能把全部问题归咎于高德。
后续应先恢复同范围的国内引导数据，再比较已有国内街区支撑策略和 C 的覆盖率、粒度；不能随机填满灰色空地，也不能用调粗道路掩盖缺失。

## 节点与版本

- 原始分支 `agent/final-block-base-clearance-v17`，HEAD `3915c54`，存在本轮及之前未提交修改；不能只凭 HEAD 复现。
- 原实验代码包 `tmp/four_cities_bridges_20260905_v3.tar.gz`，SHA256 `25e0b8fa762dd15ac0f4cc4034c9f47b64f773be929fa3296ab958950dfcd3ad`。
- 本轮最终 `generate_city_legacy.py`：`f02da1925ade8c029f024b624dc44b7ec626096abc27d6d69cf83649b9fd9365`。
- `aesthetic/bridge_sources.py`：`4aca9a1d9b71b0ef73d6fb56b788fbd59c10d933b496981395deb22bed095d10`。
- `tools/render_negative_city_png.py`：`5421777f12821e2e1b229db902c439e4d241438804df56370b642085b347e2d4`。
- Intel 实验目录 `/Users/zhangqiwei/map-four-cities-20260905`，Windows `/home/mapworker/map-four-cities-20260905`；使用节点本地 PBF/DEM/cache，旧代码就地备份后只在实验目录换入修复。
- 两台云服务器没有部署、重启或传输改动。旧失败输出保留，没有删除项目数据。

## 明确限制

这是平面语义 PNG：未检查地形起伏、建筑高度、桥梁 Z/支撑和真实喷嘴切片。C 实验仍不能恢复前面已经丢弃的建筑/街区碎片。
按 `map-model-review` 的实际图像与来源检查，生成成功与发布通过分开：西湖不通过，其余留给人工审美判断。**本轮未发布画廊、未生成 3MF/GLB、未提交或推送 Git。**
