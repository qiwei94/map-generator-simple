# 2026-08-15～18 会话总结：视觉主线恢复、Studio 重构、巴黎 OOM 救援与真实 3MF 验收

## 一、结论先行

本轮没有把 `geometry-quality-foundation` 整体合并进视觉主线，而是保留
`v0.2-with-gemeni-advise` 已验证过的西湖 25 km 视觉效果，在独立分支
`agent/web-premium-studio` 中选择性吸收正确性与工程能力。

截至 2026-08-18：

- 公网产品入口 `http://118.31.184.240/` 正常，`studio.service` active；
- 大巴黎原先的 OOM 已解除，四种风格 4/4 完成；
- 真实 25.2 km² 巴黎 3MF 已生成，项目验证器 12/12、0 errors、0 warnings；
- 正式管线会保存 `design_spec.json`，包含成品哈希、参数依据及非零要素证据；
- 无原生 osmium 时的 pyosmium 回退已做真实提取，得到 823 个道路要素；
- 非慢速测试为 **288 passed, 2 skipped, 11 deselected**；
- 代码已推送至 `origin/agent/web-premium-studio`，最新提交 `e9300b6`；
- Draft PR：<https://github.com/qiwei94/map-generator-simple/pull/4>。

本轮并未完成“全球 PBF 全量存储”和“按数据质量自动选择 block base”两个长期任务，
不要把本次验收误读为整个产品已经完成。

---

## 二、仓库、分支与路线决策

### 2.1 本地与远端

- 本地仓库：`/Users/kiwi/Documents/ChatGPT/for_better_map/map-generator-simple`
- GitHub：<https://github.com/qiwei94/map-generator-simple>
- 当前工作分支：`agent/web-premium-studio`
- 当前远端提交：`e9300b6`
- PR 基线：`v0.2-with-gemeni-advise`

### 2.2 三条重要分支的定位

1. `agent/conversational-design-log`
   - 文档/交接分支，不是代码运行基线。
   - 包含对话式地图设计的工程设想与恢复说明。

2. `agent/geometry-quality-foundation`
   - 更重视几何正确性、可打印性、校验器和 fail-closed 思路。
   - 视觉调校不如 v0.2 主线，不能整体覆盖当前高质量西湖效果。
   - 正确策略是选择性回移：便携 osmium、地形一致性、道路/桥梁正确性、
     植被闭合、结构校验、golden fingerprint 等。

3. `v0.2-with-gemeni-advise`
   - 当前视觉质量主线。
   - `real_back_up_westlake_cli.py` 生成的西湖 25 km 输出，是本轮确认的视觉基准。
   - 水体、建筑、block base 和层间退避均已有较成熟调校。

因此新工作没有破坏上述分支，而是在 `agent/web-premium-studio` 独立推进。

---

## 三、生成入口与 block base

### 3.1 正式入口

高质量西湖脚本已提升为正式入口 `generate_city.py`，历史文件
`real_back_up_westlake_cli.py` 保留兼容入口。通用城市、网页自定义框仍使用
`generate_city_legacy.py`，避免把西湖专用参数错误套到其他城市。

### 3.2 block base 的当前认识

block base 的真实产品定位已经明确：

- 它是 OSM 建筑/地块数据不足时的视觉补偿层；
- 中国等数据较稀疏区域往往需要，但不能硬编码成“中国开启”；
- 欧洲、美国的高质量城市数据可能应该关闭；
- 选择依据必须是建筑覆盖率、街区占用率、建筑密度、语义分类覆盖率等实测信号；
- LLM 不得直接控制网格、全局 Z 或布尔运算。

当前已经具备：

- `off / flat / textured` 三种显式模式；
- 可配置边缘退避，减少取景框边缘被整块填满的违和感；
- 西湖三档真实对比产物；
- `design_spec.json` 基础结构和显式 legacy 模式记录。

尚未完成：

- `auto` 模式的数据质量规则；
- 稀疏中国区域与高质量欧美区域的正式 A/B 门槛验收；
- 所有生成入口统一保存 block-base 请求值、解析值、阈值和理由。

---

## 四、水体与山水/城市场景

本轮保留了此前已经修复的钱塘江/西湖水体链路：

- pyosmium relation 水体不会再全部丢失；
- 钱塘江不再退化成统一粗细的抢眼蓝线；
- 高德/卫星补面只作为可追踪的补强证据，不掩盖 OSM 原始计数；
- 3MF 材料数量有限时，水面仍可以共享材料，但预览表达不应把江、湖、河渠画成
  同一视觉权重。

千岛湖测试暴露了“把山水区域按城市高密路网风格渲染”的错误：粗黑道路和行政/主干
结构压过了湖面与山体。当前画廊已经具有 `urban / landscape / water_landscape` 场景
分类与风格变体基础，但仍需继续做真实山水区域验收，不能只依赖分类代码存在。

---

## 五、网页产品与交互进展

网页从旧式工具页改造成轻量编辑工作台，主要变化包括：

- 使用真实西湖/钱塘江输出作为首屏视觉，不再使用虚构 CSS 地形；
- 产品文案改为旅行、回忆和桌面实体模型的情绪价值表达；
- “然后交付可打印的多色 3MF”收敛为“然后交付可打印的模型”；
- 生成入口支持 classic、质量 flat、质量 textured 等隔离配置；
- 选定风格会真实传到后台，不再出现前端选了但后台忽略；
- 支持 job token、分享链接和跨刷新任务找回；
- 重复区域请求会复用已有任务/成品，不再把“正在处理中”作为错误；
- 进度提示改为真实阶段映射，并根据区域大小给出 8～15、12～25、20～40 分钟级
  预期，不再承诺不可信的“约 1 分钟”；
- 修复窄页面下“选位置”区域的横向挤压；
- 修复地点搜索的景点列表偶发消失、埃菲尔铁塔等搜索问题；
- 清理“西湖质量”这类内部工程名称在用户界面的泄漏。

已知视觉问题：

- 页面整体方向明显好于旧版，但还需要持续统一字体、留白、地图控件和结果卡片；
- 云机缺少 CJK 字体，诊断图/联系表的中文可能显示方框；
- 大区域缩略图下 baseline 与 block-fill 差别偏弱；
- 快速预览在冷缓存、2 核云机上仍不是真正“即时”。

---

## 六、巴黎 OOM 根因与修复

### 6.1 根因

巴黎失败并非简单的“区域太大”，而是多个内存放大同时发生：

1. 大量瓦片缺失时，旧逻辑仍先读取已有超大瓦片；
2. osmium GeoJSON 的稀疏 OSM tag 被 GeoPandas 扩成数千个全空列；
3. 全框、瓦片拆分和 DataFrame 副本同时驻留；
4. 大巴黎旧进程 RSS 接近 15 GB，最终被 OOM killer 杀死。

### 6.2 工程修复

- 缺失瓦片达到一半或缺失范围覆盖全框时，直接整框刷新；
- 瓦片缓存仅保留管线实际消费的 OSM 字段；
- 原子压缩 12 个超大 building/road GeoJSON 缓存，保持要素数量不变；
- 道路预处理改为向量化过滤，减少 `iterrows` 和临时对象；
- landmark/减法复用 prepared geometry；
- 大范围道路档位上限从 5 收敛为 4，删除低于喷嘴可打印尺度的 footway/path/steps；
- 无效 OSM ring 在聚合和减法阶段修复，不再直接终止整种风格；
- 正式环境安装原生 osmium 1.19.1；
- 配置 4 GB swap 作为峰值保险，但不拿 swap 掩盖内存问题。

### 6.3 大巴黎验收

原失败框：

```text
[48.7906, 2.2229, 48.9262, 2.4277]
```

冷提取证据：

- buildings：1,239,622
- roads：540,673
- water：4,111
- vegetation：67,064
- 峰值 RSS：9.49 GB
- swap：0
- OOM：无

最终画廊：

- 4/4 风格完成；
- 缓存后完整画廊耗时 1,052.2 秒；
- 最终道路 10,527、水体 223，均非零；
- 塞纳河在大区域图中可辨识；
- baseline 与 block-fill 在该缩放级别仍偏接近，属于后续视觉问题。

这些是 2 核 Linux 云机数据，**不能**作为 16 GB Mac 的性能结论。

---

## 七、portable osmium 的真实回退验收

仅让脚本“存在”不等于回退可用。本轮第一次真实测试发现：`tools/osmium` 使用
`/usr/bin/env python3`，云机因此选到没有安装 pyosmium 的系统 Python，回退被误判为
不可用，实际道路数为 0。

修复后，解析器始终使用当前管线解释器启动 `tools/osmium_pyosmium.py`。

验收环境明确执行：

- 移除 `OSMIUM_BIN`；
- PATH 不包含 `/opt/osmium-native/bin`；
- 实际命令解析为：

```text
/usr/local/python3.9/bin/python3.9 /root/map-generator-simple/tools/osmium_pyosmium.py
```

巴黎小夹具完整执行 `extract → tags-filter → export`：

```text
PORTABLE_ROAD_COUNT 823
LineString 574
Point      241
Polygon      8
总耗时       1.0s
```

这才构成“无原生 osmium 时道路提取不为零”的有效证据。

生产环境仍优先使用：

```text
/opt/osmium-native/bin/osmium  # 1.19.1
```

---

## 八、真实 3MF 与 DesignSpec 验收

正式巴黎框：

```text
[48.838, 2.3125, 48.8833, 2.3807]
实际成品范围约 25.2 km²
```

成品：

```text
output/custom_a42bd1/full_custom_a42bd1_0818_0334.3mf
size:   13,193,097 bytes
sha256: 3b3eedecabb3f7087d21555a82da346a5d170b99e8266d4361302e2c519c1d4c
```

下载：

- 3MF：<http://118.31.184.240/files/custom_a42bd1/full_custom_a42bd1_0818_0334.3mf>
- DesignSpec：<http://118.31.184.240/files/custom_a42bd1/design_spec.json>
- 俯视图：<http://118.31.184.240/files/custom_a42bd1/custom_a42bd1_topdown.png>

项目验证器：

```text
12/12 rules passed
errors:   0
warnings: 0
```

关键要素证据：

| 类型 | 精确裁剪后源数据 | 最终可打印数据 |
|---|---:|---:|
| 建筑 | 131,263 | BL 1,875 + BO 1,238 |
| 道路 | 101,111 | 1,249 |
| 水体 | 500 | 43 |
| 植被 | 6,252 | VL 67 + VO 22 |
| landuse | 4,489 | block base 957 |

### 8.1 植被流形修复

第一版 3MF 是 0 errors / 1 warning。旧 V12 规则要求植被 90% 水平，与当前贴地形植被
冲突；但进一步检查也发现了真实缺陷：点接触片区会让竖边被 4 个面共享。

最终修复不是关闭警告，而是：

- 按共享边拆开只在一点相触的三角岛；
- 对全局连通但局部形成“8 字夹点”的顶点拆分独立 face fan；
- V12 改为检查有限坐标、XY 范围、边界边和非流形边。

最终结果：

```text
finite=True
in_bounds=True
boundary_edges=0
nonmanifold_edges=0
vegetation watertight=True
```

### 8.2 DesignSpec

新增 `_TEXTURE_STYLE_OF_DEEPSEEK/design_spec.py`：

- 仅作为确定性审计 sidecar，不参与网格生成；
- 写入采用同目录临时文件 + `os.replace`；
- 记录 bbox、pipeline、resolved params、decision reasons、profile、block-base 状态、
  源要素和最终要素数量；
- 对 3MF 记录 filename、size 和 SHA-256；
- 正式 3MF 成功导出后自动写 `design_spec.json`；
- 快速 gallery draft 也复用统一写入器。

注意：当前自动参数 profile 可能基于 snap 取数框，而成品 bbox 是用户精确框；后续应在
DesignSpec 中把“参数测量范围”和“最终成品范围”显式分开，避免面积数字被误读。

---

## 九、云端环境与存储现状

### 9.1 计算主机 B

- 公网：`118.31.184.240`
- 连接：

```bash
ssh -i ~/.ssh/map_generator_ed25519 -o IdentitiesOnly=yes root@118.31.184.240
```

- 项目：`/root/map-generator-simple`
- Python：`/usr/local/python3.9/bin/python3.9`
- 服务：`studio.service`，端口 80，active
- 内存：16 GB
- swap：`/swapfile-mapgen` 4 GB，验收运行使用 0
- 系统盘：约 120 GB，本轮结束约剩余 19 GB
- 原生 osmium：`/opt/osmium-native/bin/osmium`
- B 没有 Git，可执行文件部署仍以 scp 为主；GitHub 源码以本地 Mac 分支为准。

### 9.2 数据主机 A

- 公网：`8.136.0.235`
- `/data` 盘曾出现 I/O 错误、不可可靠写入；不要在没有重新做磁盘健康检查前把它当生产盘。
- 当前约 80 个 PBF + DEM 的双机数据体系，不等于“全球数据已经落盘”。

### 9.3 尚未完成的全球数据方案

- 需要按 Geofabrik 大区/国家分层，而不是把 planet PBF 直接复制到每台计算机；
- 数据仓库与计算缓存应分层，热门区域在 B 本地，长尾区域在 A/对象存储；
- 需要清单、版本日期、校验和、下载源优先级和增量更新；
- BBBike 更适合城市级补充，Geofabrik 更适合稳定的大区镜像；
- 在 `/data` 盘硬件问题解决前，不能宣称已有全球容量。

---

## 十、测试、提交与 PR

最终非慢速测试：

```bash
.venv/bin/pytest -q tests -m 'not slow'
```

结果：

```text
288 passed, 2 skipped, 11 deselected, 14 warnings
```

14 条 warning 来自 urllib3/LibreSSL 与 matplotlib/pyparsing 依赖弃用，不是本轮管线失败。
仓库根目录直接执行 pytest 会收集 `tools/test_amap_water.py`，未配置 `AMAP_KEY` 时会在
import 阶段退出；离线回归必须限定 `tests/`。

本轮后半段关键提交：

```text
e9300b6 fix: run portable osmium with active Python
de4e229 fix: preserve portable osmium executable entry
addeb5a fix: split pinched vegetation vertex fans
7e63a80 fix: keep point-touching vegetation edge-manifold
423b672 feat: persist design spec beside generated artifacts
8a06b0a perf: cap unprintable road detail for large areas
2d6ac75 perf: reuse prepared cutter for layer subtraction
c2ee71b fix: scale generation estimates and log native extractor
895e20f fix: report the active gallery style in progress
e34d322 perf: reuse prepared landmark index for roads
d12d351 perf: speed up dense-city road preprocessing
80a0740 fix: harden block aggregation against invalid OSM rings
25b6638 fix: repair invalid polygons during layer subtraction
8c6e820 fix: bound dense-city osmium cache memory
```

Draft PR #4 已补充完整测试、OOM 恢复、portable osmium、真实 3MF、DesignSpec 和已知限制证据。

---

## 十一、下一步优先级

### P0

1. 完成 data-quality-aware block-base `auto` 策略及边界值测试。
2. 让 `generate_city.py`、legacy full、gallery draft 等所有 3MF 入口都保存统一 DesignSpec。
3. 给 DesignSpec 增加 exact bbox 与 snap/profile bbox 的显式测量范围。
4. 继续选择性回移 geometry-quality-foundation：
   - 精确规则网格地形插值；
   - 道路 buffer 裁到可打印地形；
   - 普通道路/桥梁分开 union；
   - 每层 finite / bounds / watertight / sane-Z gate；
   - 小型真实 fixture 的结构 golden fingerprint。
5. 对稀疏中国城市、欧美高质量城市、山水景区各做至少一个真实 3MF A/B。

### P1

1. 安装并配置 CJK 字体，清理诊断图方框字。
2. 继续优化冷缓存快速预览；优先减少 matplotlib 大图渲染和不必要的重复诊断图。
3. 加强山水类道路降级策略，避免千岛湖式粗黑主干线压过湖山。
4. 设计全球 PBF/DEM 分层存储，在动 `/data` 前先完成磁盘健康检查。
5. 在目标 16 GB Mac 上做单独基准，不使用 2 核 Linux 云机结果替代。

---

## 十二、不可违反的工程约束

- 不允许 LLM 直接控制底层网格、全局 Z 或布尔运算；
- 不把“进程未报错”当成功，必须检查道路/水体/建筑等最终数量；
- 不因截图看起来像水就掩盖 OSM 水体为零；
- 不整体覆盖 v0.2 已验证的视觉管线；
- 不把 Linux 沙箱或 2 核云机性能当 16 GB Mac 结论；
- 真实交付必须同时具备成品、DesignSpec、验证器结果和可复核要素证据。

