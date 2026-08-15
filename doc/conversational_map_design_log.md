# 对话式地图设计：工作记录与恢复说明

记录日期：2026-08-14  
基线分支：`agent/geometry-quality-foundation`  
本文记录本轮地图转 3MF 工具的产品决策、已验证结果、运行环境结论与恢复状态。

## 一、产品方向

目标不是“万能聊天生成 3D”，而是一个**可对话的地图设计工具**：

```text
自然语言意图 → 受约束且可版本化的 DesignSpec → 确定性的几何 / 可打印模型编译器
```

聊天层只能提出地图要素、筛选条件、视觉强调与打印目标；不能直接控制全局 Z 值、布尔运算或底层算法。这保证同一设计规格可以复现、验证和比较。

外部项目的借鉴边界如下：

| 项目 | 采用的优点 |
| --- | --- |
| Terrology | 产品层与打印约束的基准 |
| TrailPrint3D | 可组合的后处理 / 打印模块 |
| OSM2World | OSM 语义处理的鲁棒性 |
| Planetiler | 声明式选择器，启发 DesignSpec |
| MapLibre GL JS | 低成本地图预览 |
| Mapbox MCP | 受限配置、验证、预览与差异工作流 |

这些项目仅作参考与设计借鉴，**没有**被作为本仓库的运行时依赖整合。

## 二、已完成并验证的基础能力

### 几何质量基础

几何质量分支完成了面向可打印地形模型的基础处理；西湖完整端到端测试曾通过（8 项测试，约 6–7 分钟），生产测试已与“水面贴合地形”的行为对齐。

### DesignSpec（对话设计规格）基础

此前的本地实现包含：

- 版本化 JSON 规格与确定性指纹；
- `LayerSpec`、标签过滤、`resolve_design_spec` 与 `filter_features`；
- 预设：`city_texture`、`terrain_only`、`road_network`、`water_focus`；
- 为避免悬空模型，现有可打印模式强制保留地形；
- pipeline 接收 `design_spec`，并把 `design_spec.json` 写入输出目录；
- CLI 参数：`--preset`、`--design-spec`；
- pipeline 仅拉取并构建被选中的数据层，支持仅地标的数据来源；
- 配套测试与文档。

### 便携式 OSM 提取回退

原生 `osmium` 缺失时，道路会被错误提取为零。此前本地实现增加了回退：

- 系统 `osmium` 不可用时，使用仓库内 `tools/osmium_pyosmium.py`；
- 统一以命令数组调用 `subprocess`；
- 依赖中增加 `fast-simplification` 与 `osmium`；
- 加入 macOS 16GB 运行说明与 fetcher 测试。

在西湖道路数据上，回退模式读取到 8,027 条 `primary/secondary` 记录（原始缓存为 61,998 条）。

## 三、真实运行结果

已成功生成“西湖道路网络”3MF：

- 边界框：30.13,120.01 → 30.36,120.29（约 685.1 km²）
- 地形：1,729,816 个三角面
- 渲染道路线：10,320 条
- 3MF：19.3 MB
- 运行时间：140.2 秒
- 3MF 验证器：0 errors / 0 warnings

当时环境中缺少 `fast-simplification`，因此该指标不是最佳性能。建议在目标 Mac 上安装 Homebrew 的 `osmium` 和 GDAL，并保持单任务运行。

## 四、16GB MacBook Pro 的环境结论

开发沙箱为 Linux（约 21 GiB 内存、9 CPU），不能替代实际 16GB Mac 的基准数据。

在 16GB Mac 上建议：

- 单并发任务；
- 安装 `osmium`、GDAL、`fast-simplification`；
- 先使用较小区域验证；
- 将低分辨率预览与最终高分辨率 3MF 生成分开；
- 运行时避免同时启动大型 IDE、浏览器标签页和多个模型生成任务。

## 五、后续待做

1. 实现“自然语言 → DesignSpec patch”的对话编译器；
2. 增加 MapLibre 低分辨率三维预览；
3. 增加空间选择与 POI 选择器；
4. 在实际 16GB MacBook Pro 上跑性能与内存基准；
5. 恢复并重新提交下节说明的本地源代码改动。

## 六、工作区恢复状态（重要）

本记录发布前，临时沙箱自动清理了原本的工作目录；因此下列**仅存在于本地、尚未推送的源码提交**目前不在远程仓库中：

- `9b8e9cd feat: add constrained conversational design specs`
- `16f6ab9 fix: support portable OSM extraction fallback`

这两个提交所代表的实现与测试结果已按本记录保存，但源码需要从本说明重新实现或从任何保留的本地克隆 / Git 对象中恢复。当前 PR 只提交此工作记录，不把它误表示为完整功能代码发布。

## 七、验证快照

本地非慢速测试在清理前最后一次结果：

```text
153 passed, 26 warnings
```

真实西湖道路网络生成也已通过 3MF 验证器。
