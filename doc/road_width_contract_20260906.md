# 第二项：道路视觉宽度与物理约束分离

状态：本地接线与单材料小样切片完成；不是整城、桥梁或多材料打印验收。

## 实现

`aesthetic/road_width_contract.py` 集中解析两种显式视觉预设，S6 统一消费其结果：

|预设|普通街缝全宽|主干走廊全宽|
|---|---:|---:|
|printer-default|0.55 mm|0.84 mm|
|negative-space-v1|0.28 mm|0.42 mm|

这里两组数字是不同预设，不是 PNG 与 3MF 各自选宽。S7/S8 继续消费冻结几何。
未修改硬件 PrinterProfile，也未降低 S11 的旧保守验收阈值。

S6 `road_width_contract` 随 surface_plan_evidence 记录，包含以下独立检查责任：

- 负空间：城市材料缺失、下有底座。不能把喷嘴直径直接当空槽最小宽度；检查逐层挤出包络、通路、深度。
- 正实体：道路或桥面有独立挤出。检查连续走线、横截面、支撑或经过验证的桥跨。
- 多材料：另验对象到耗材映射、逐材料存活、边界侵入；不能因单材料切片成功就认定配色可打印。

所有物理状态仍为 pending。当前第二项完成语义分离和直槽/凸条基准，尚未完成
全形状、多材料标定；不能用本次小样把 0.14 mm 升格为产品默认。

## 实际切片

12 块样片：6 个宽度（0.14、0.28、0.42、0.55、0.63、0.84 mm），各做空槽/凸条。
每块 12×8 mm，底板厚 0.8 mm，槽深/条高 0.48 mm。所有测试有底板支撑，不测试悬空桥。

Bambu Studio 02.08.02.60；X1 Carbon 0.4 nozzle；Generic PLA；0.12 mm Fine；
首层 0.20 mm；Classic 墙生成器；薄壁检测关闭；无熨烫、无支撑、无裙边；关闭圆弧拟合。
有效设置从最终 G-code 读取，不只看输入 JSON。

|宽度|空槽顶层走线包络|凸条上层走线|
|---|---|---|
|0.14 mm|保留约 0.14 mm 间隙|没有上层挤出|
|0.28 mm|保留约 0.28 mm 间隙|没有上层挤出|
|0.42 mm|保留约 0.42 mm 间隙|保留|
|0.55 mm|保留约 0.55 mm 间隙|保留|
|0.63 mm|保留约 0.63 mm 间隙|保留|
|0.84 mm|保留约 0.84 mm 间隙|保留|

结论仅适用于本组设置：同宽槽与条的存活不同，因此不能用同一个宽度阈值。
换 Arachne/薄壁策略可能改变凸条结果。测量以 G-code 声明线宽构造平面包络，
并沿内部 61 条截线采样；没有模拟材料流动、挤出膨胀、冷却和拉丝。
图显示走线中心而非挤出宽度；正实体统计沉积区间总长度，不把两线间舍入量级的小缝误判为整体丢失。

## 证据与复现

目录 `output/road_width_slice_20260906/`：

- `manifest.json`：样片身份、STL 哈希、尺寸和边界。
- `coupons.stl`：封闭、水密、正体积的测试几何。
- `plate_1.gcode`、`widths_sliced.3mf`：真实切片结果；已核对 3MF 内 G-code 与独立文件逐字节相同。
- `analysis.json`：有效参数、G-code 哈希、各样片各上层宽度/走线统计和限制。
- `toolpaths.png`：中文走线对照。
- `result.json`：切片/导出返回码 0，该板 warning_message 为空。不是项目严格验证器通过声明。

生成工具 `tools/verify_road_width_slicing.py prepare` 不覆盖既有目录；
分析 `tools/verify_road_width_slicing.py analyze` 只读实际切片文件并输出报告。

在上述测试目录内运行本机离线命令：

```sh
/Applications/BambuStudio.app/Contents/MacOS/BambuStudio \
  --datadir /private/tmp/map-road-width-bambu-20260906 \
  --load-settings 'machine.json;process.json' --load-filaments filament.json \
  --arrange 0 --orient 0 --slice 0 --export-3mf widths_sliced.3mf \
  --outputdir /Users/kiwi/Documents/ChatGPT/for_better_map/map-generator-simple/output/road_width_slice_20260906 \
  coupons.stl
```

回归：宽度契约、路径解析（排除空走/回抽、绝对挤出复位、拒绝未支持圆弧）、
S6 负空间、共享城市面、实际 GLB/3MF 回读、阶段域与验收等共 54 项通过。
16 条既有 Pillow 弃用警告，不是切片器警告。没有启动实体打印、部署或推送 Git。

后续：曲线/交叉口、坡地、不同槽深的小样；独立多材料验证；最后才决定正式
产品的宽度默认和新验收门槛。诊断 verdict 为 human_review，完整模型的打印验收尚未进行。
