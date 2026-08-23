# 2026-08-20 城市取景与双机队列交接

本文记录 `agent/durable-queue-auth` 在 2026-08-20 的可恢复状态。它是运行现场快照，不替代 README、部署文档或提交历史。

## 当前版本与入口

- 分支：`agent/durable-queue-auth`
- Draft PR：<https://github.com/qiwei94/map-generator-simple/pull/5>
- 公网页面：<http://118.31.184.240/?release=v53>
- 正式成品固定提供 15 km × 15 km 与 25 km × 25 km 两档。
- 快速 3D 预览只使用同一中心点周围的 5 km 范围；正式 3MF 使用完整取景框。
- `DesignSpec`、任务记录和输出同时保存 `source_bbox` 与 `preview_bbox`，避免预览误用西湖等默认数据。
- `aesthetic/framing.py` 使用确定性水系构图评分，为河流转折、水陆反差明显的区域推荐 25 km 档。评分只控制构图建议，不允许 LLM 直接控制网格、全局 Z 值或布尔运算。

## 首页样品状态

当前公开样品只有以下三组：

1. 巴黎，15 km。
2. 杭州西湖与钱塘江，25 km；使用已确认的优质历史图 `webapp/static/assets/westlake-real-output.jpg`。
3. 芝加哥，15 km。

苏州历史结果经物理边界复核约为 15.00 km × 15.03 km，但其 `area.json` 仍带有“西湖”名称，且视觉上缺少完整城市尺度感。为避免把身份污染的产物当作可靠样品，它已从首页精选与回退数据中撤下，后续应重新生成并校验名称、边界和场景类型。

## 已验证内容

最近一次完整非慢速测试命令：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/map_generator_pycache \
  .venv/bin/pytest -q tests -m 'not slow'
```

结果：`327 passed, 2 skipped, 11 deselected`。注意必须限定 `tests/`；从仓库根目录无范围运行会收集 `tools/test_amap_water.py`，该诊断脚本在未配置 `AMAP_KEY` 时会主动退出，并不表示项目单元测试失败。

本次 worker 改动的针对性验证命令：

```bash
PYTHONPYCACHEPREFIX=/private/tmp/map_generator_pycache \
  .venv/bin/pytest -q tests/test_cloud_worker_limits.py tests/test_web_worker_queue.py
```

结果：`5 passed`。

窄屏 900 × 900 浏览器复核结果：页面没有横向溢出；15/25 km 控件均可见；选择 25 km 后正式生成按钮同步更新；控制台为 0 errors / 0 warnings。

## 两台云主机

### 主节点 `118.31.184.240`

- 对外提供页面、API、队列和主要渲染 worker。
- 原生 osmium 位于 `/opt/osmium-native/bin/osmium`。
- 已通过 systemd drop-in 把 `OSMIUM_BIN=/opt/osmium-native/bin/osmium` 注入正在运行的 worker，并从进程环境确认生效。
- `deploy/setup_queued_studio.sh` 也已加入该环境变量，保证以后重新安装服务时不会退回慢速 Python 提取器。

### 辅助节点 `8.136.0.235`

- 原生 osmium 位于 `/usr/local/bin/osmium`。
- 已同步当前 worker 代码。
- 已复制意大利西北部 PBF：`/root/map-generator-simple/pbf_cache/nord-ovest-latest.osm.pbf`。
- 文件大小：579,996,203 bytes。
- 两端 SHA-256：`980323d00a7150270c84484a770a36579f5a0bf0276df4eeab4130b0daafed85`。
- 当前没有常驻 secondary worker。

辅助节点曾以无能力约束的通用 worker 启动，随后领取了本机没有对应 PBF 的东京、上海、广州、柏林和西湖任务，并将其错误标记为 `render_failed`。这些失败不是几何质量结论。现阶段只能在确认“队首任务所需 PBF 已存在”后，用 `--max-tasks 1` 启动一次性 worker；长期方案应让服务端按 worker 已安装的 PBF/区域能力匹配任务。

## Worker 修复

本次保存的 worker 改动包括：

- 单任务默认超时从 2700 秒提高到 7200 秒，避免 15 km 高密度城市在仍有进展时被 45 分钟硬杀。
- 增加 `--task-timeout`，允许部署时明确覆盖超时。
- 增加 `--max-tasks`，辅助节点可在完成一个已匹配任务后退出，降低误领其他区域任务的风险。
- 主节点服务固定使用原生 osmium；此前误用 `tools/osmium_pyosmium.py` 时，单次 PBF 提取约需 7–10 分钟。

## 队列现场快照

以下是保存状态前最后一次采集的现场：

| 任务 | 城市 | 状态 | worker | 解释 |
| --- | --- | --- | --- | --- |
| `75e3f7f7` | New York · Manhattan retry | running | local-primary | 正在使用已缓存预处理数据重试；不要为部署重启主 worker |
| `522e62fc` | New York · Manhattan | failed | local-primary | 旧 45 分钟超时，不是几何失败 |
| `24271081` | Milan · City Centre | failed | local-primary | 旧 45 分钟超时；四类 GeoDataFrame 已缓存 |
| `ea66c451` | Rome · Eternal City | done | local-primary | 真实成功 |
| `990841e1` | Melbourne · Yarra River | failed | local-primary | 真实 `render_failed`，重试前必须读日志定位 |
| `c589c05b` | London · Thames | done | local-primary | 真实成功 |
| `139db54b` | Tokyo · Central | failed | secondary-8-136 | 辅助节点缺 PBF 的无效失败 |
| `004841ff` | Shanghai · Huangpu River | failed | secondary-8-136 | 辅助节点缺 PBF 的无效失败 |
| `94299037` | Guangzhou · Pearl River | failed | secondary-8-136 | 辅助节点缺 PBF 的无效失败 |
| `17801a90` | Berlin · City Centre | failed | secondary-8-136 | 辅助节点缺 PBF 的无效失败 |
| `37fadce1` | Hangzhou · West Lake and Qiantang River | failed | secondary-8-136 | 辅助节点缺 PBF 的无效失败 |

纽约重试最后观察到的生成进程持续占用约 100% 单核 CPU、RSS 约 755 MB，说明当时仍在计算而非僵死。不要把该 Linux 云主机的耗时直接外推为 16 GB Mac 的性能结论。

## 建议恢复顺序

1. 先确认纽约重试 `75e3f7f7` 的最终状态和产物，不要重复提交。
2. 重新入队米兰。主节点已有缓存；若使用辅助节点，先确认米兰是下一条可领取任务，再以 `--max-tasks 1` 启动。
3. 读取墨尔本失败日志并定位根因，再决定是否重试。
4. 东京、上海、广州、柏林和西湖必须在主节点重排，或先把各自精确 PBF 复制到辅助节点后逐个一次性执行。
5. 在实现 worker 能力/PBF 匹配前，不要恢复无限轮询的辅助 worker。
6. 每个完成任务都要校验城市身份、实际边界、道路/建筑/水体数量、PNG 视觉结果和 3MF 验证结果；“命令没有报错”不等于成功。

## 未纳入 Git 的本地状态

`data/studio.db`、`data/studio.db-shm`、`data/studio.db-wal` 是本地运行数据库及其 WAL 文件，包含瞬时任务状态，不应提交到仓库。本次提交会明确排除它们。
