# Codex 工程交接：可对话地图设计与可打印 3MF

> 目的：让接手的 Codex 在不依赖对话上下文的情况下，能判断项目现状、恢复丢失的实现、在真实 Mac 上验证，并按优先级推进。
>
> 仓库：`qiwei94/map-generator-simple`  
> 基线：`agent/geometry-quality-foundation`  
> 文档分支：`agent/conversational-design-log`  
> 日期：2026-08-14

## 0. 先说结论

这个项目值得继续做，但应定位为：

```text
自然语言意图
  → 受约束、可版本化、可复现的 DesignSpec
  → 确定性的地理数据 / 地形 / 3MF 编译流水线
  → 预览、验证、制造交付
```

不要把它做成“LLM 直接操纵任意 3D 几何”的工具。核心竞争力应是：用户能用自然语言表达地图设计意图，而系统仍能稳定地产出可打印模型。

## 1. 已完成且有价值的基础

### 1.1 几何质量基础（远程基线应存在）

分支 `agent/geometry-quality-foundation` 是后续工作的基线。此前已完成与验证：

- 面向可打印地形模型的几何质量处理；
- 水面贴合地形的生产测试对齐；
- 西湖完整端到端测试通过：8 项测试，约 6–7 分钟。

接手前请先检查该分支与 `main` 的差异、测试命令及是否已有 PR；不要假定它已合并。

### 1.2 已验证的真实运行样例

“西湖道路网络”曾在 Linux 沙箱成功生成：

| 项目 | 结果 |
| --- | --- |
| BBox | 30.13,120.01 → 30.36,120.29 |
| 覆盖面积 | 约 685.1 km² |
| 地形 | 1,729,816 个三角面 |
| 渲染道路线 | 10,320 条 |
| 3MF 文件 | 19.3 MB |
| 耗时 | 140.2 秒 |
| 3MF 验证 | 0 errors / 0 warnings |

最后一次非慢速测试快照：`153 passed, 26 warnings`。

### 1.3 参考项目与应吸收的部分

- **Terrology**：产品层和打印约束的对标；
- **TrailPrint3D**：可组合后处理 / 打印模块；
- **OSM2World**：OSM 语义处理的鲁棒性；
- **Planetiler**：声明式选择器；
- **MapLibre GL JS**：交互式低成本预览；
- **Mapbox MCP**：受限配置、校验、预览、差异化工作流。

这些仅是设计参考，不应贸然引入为运行时依赖。

## 2. 曾完成但尚未推送、需要恢复的源码

临时沙箱自动清理了原工作目录。下面两笔本地提交在清理前存在，但目前不在远程分支中：

- `9b8e9cd feat: add constrained conversational design specs`
- `16f6ab9 fix: support portable OSM extraction fallback`

**不要把它们当作已发布实现。** 接手者应从下面的规格重新实现，或在开发者其他本地克隆 / Git 对象中先尝试找回。

### 2.1 必须恢复：DesignSpec

预期文件与职责：

- `_TEXTURE_STYLE_OF_DEEPSEEK/design_spec.py`
  - 版本化 JSON schema；
  - `DesignSpec`、`LayerSpec`；
  - tag filters；
  - `resolve_design_spec`、`filter_features`；
  - 确定性 fingerprint。
- 预设：
  - `city_texture`
  - `terrain_only`
  - `road_network`
  - `water_focus`
- pipeline：
  - 接收 `design_spec`；
  - 仅拉取 / 构建被选中的数据层；
  - 支持仅地标的数据来源；
  - 在输出目录保存 `design_spec.json`。
- CLI：
  - `--preset`
  - `--design-spec`
- 安全约束：
  - 当前可打印模式必须保留地形，避免要素悬空。
- 测试：
  - `tests/test_design_spec.py`
- 文档：
  - `doc/conversational_design_spec.md`

### 2.2 必须恢复：OSM 提取的可移植回退

问题：系统没有原生 `osmium` 时，曾导致道路提取结果为零。

期望修复：

- 修改 `_TEXTURE_STYLE_OF_DEEPSEEK/terrain3d/fetchers/osmium_cli_fetcher.py`；
- 系统 `osmium` 不存在时，调用仓库内 `tools/osmium_pyosmium.py`；
- 以命令数组而不是拼接 shell 字符串传入 `subprocess`；
- `requirements.txt` 和 `environment.yml` 加入 `osmium`、`fast-simplification`；
- 新增 `tests/test_osmium_cli_fetcher.py`；
- 文档：`doc/macbook_16gb_runtime.md`。

实测回退读取到西湖 `primary/secondary` 共 8,027 条记录（原始缓存为 61,998 条），因此验收不应只是“进程未报错”，而必须校验道路数大于零。

## 3. 推荐实施顺序（按优先级）

### P0：先恢复可运行、可验证的基础

1. 克隆仓库并检出 `agent/geometry-quality-foundation`；
2. 按现有 README / 环境文件安装依赖；
3. 运行既有快速测试，记录命令与结果；
4. 恢复 OSM 回退及其单测；
5. 恢复 DesignSpec、CLI、pipeline 接入与单测；
6. 运行全部非慢速测试；
7. 用小区域跑一次真实 3MF，并用项目现有验证器验证。

**P0 验收标准**

- 原有测试不回退；
- 新增 DesignSpec 测试通过；
- 无原生 `osmium` 时道路不再为零；
- 输出包含 `design_spec.json`；
- 至少一个真实 3MF 输出经验证器返回 0 errors / 0 warnings；
- 每项改动都独立提交，并以草稿 PR 提交审查。

### P1：把“对话”做成受控编译，不要直接接管几何

实现一个受限的“自然语言 → DesignSpec patch”层：

- 只允许选择预定义层、标签过滤、视觉强调、材料 / 打印目标；
- 每次返回完整 JSON patch、schema 校验结果和 fingerprint；
- 对无效请求给出可操作的澄清，而不是猜测；
- 永远把最终 DesignSpec 保存到输出中；
- 支持同一输入 + 同一数据快照的重复构建和 diff。

建议先用规则 / 显式映射实现，LLM 只负责提案。任何 LLM 输出必须通过 schema 校验和业务约束再进入 pipeline。

### P2：增加预览与选择能力

- MapLibre 低分辨率三维预览；
- 区域 / POI / 空间筛选器；
- 提交前展示将会生成的层、过滤条件、面积估算、预计面数与内存风险；
- “预览”与“高精度 3MF 导出”使用不同质量档位。

不要在 P2 前投入大型前端：P0 输出必须先稳定。

### P3：真实 16GB Mac 基准

Linux 沙箱约 21 GiB 内存、9 CPU，不能代表目标机。用户目标是 16GB MacBook Pro，建议：

- 单任务运行；
- 使用 Homebrew 安装 `osmium`、GDAL；
- 安装并验证 `fast-simplification`；
- 每次先小区域，再扩大；
- 记录：总耗时、峰值内存、输出大小、面数、道路 / 水体 / 建筑数量；
- 避免同时运行多个 IDE、浏览器重负载页、并行模型生成任务。

推荐输出一个机器可读的 benchmark JSON，便于后续回归比较。

## 4. 需要特别避免的错误

- 不要让聊天模型输出全局 Z 值、布尔运算、任意网格操作；
- 不要因“能运行”就接受道路数为零；
- 不要把 Linux 沙箱测试说成 Mac 性能结论；
- 不要把引用项目直接搬入依赖树；
- 不要合并未跑过真实 3MF 验证的几何改动；
- 不要遗漏输出 DesignSpec；它是可复现性与售后排障的关键；
- 不要将临时沙箱当作唯一源码存储。每完成一个可用切片就应提交 / 推送。

## 5. 建议的 PR 切分

1. `fix: restore portable OSM extraction fallback`
2. `feat: add constrained DesignSpec foundation`
3. `feat: compile conversational requests to DesignSpec patches`
4. `feat: add lightweight map design preview`
5. `test: add 16GB Mac benchmark fixture`

每个 PR 需要写清：

- 变更内容；
- 为什么需要；
- 测试命令与结果；
- 对输出格式 / 可复现性的影响；
- 是否跑过真实 3MF 及验证器。

## 6. 第一个可执行指令

接手的 Codex 先执行：

```text
1. 检查 agent/geometry-quality-foundation 与 main 的状态；
2. 恢复并测试 osmium 回退；
3. 恢复并测试 DesignSpec；
4. 跑非慢速测试；
5. 选一个小区域跑真实 3MF；
6. 仅在上述验收通过后，推送草稿 PR。
```

若任何一步因环境或数据源失败，先把命令、完整错误、系统版本、依赖版本与可复现最小输入写进 Issue / PR，不要用“本机可以”代替证据。
