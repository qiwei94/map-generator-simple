# 建筑高度数据：持久化、来源与使用边界

更新日期：2026-08-26

## 目标

远端建筑高度只查询一次，之后从本地持久库复用。数据获取与模型生成解耦：
网络故障或供应方限流不能让已经获取过的城市退化为默认高度。

默认目录：

```text
data/height_cache/
├── building_heights.sqlite3   # 标准化高度、覆盖范围、请求与地标缓存
├── building_heights.sqlite3-wal / -shm
└── overture_*.parquet         # Overture 原始响应，长期保留
```

`data/` 不进入 Git。它是运行数据，需要独立备份；代码提交不能替代高度库备份。
代码不会按 TTL 自动删除高度观测或原始响应。

## 已实现来源

### OSM

每次成功解析建筑后，将显式 `height` 和 `building:levels` 归档到
`height_observations`。无显式高度的普通建筑不作为“真实观测”写入。

### Overture Buildings

原始 GeoParquet 保留，合法高度与楼层数同时标准化到 SQLite。已导入的 bbox
会记录在 `source_coverage`；后续较小取景框被已有覆盖范围完全包含时，直接使用
SQLite RTree 查询，不再读取远程源。

OSM 与 Overture 建筑不再使用“任意相交、第一条胜出”。匹配依据最大几何重叠，
低于 IoU/覆盖率门槛的候选会被拒绝。

### Wikidata 地标

只有 OSM 已带 `wikidata=Q...` 的建筑才有资格查询。使用 Wikibase
`wbgetentities` API，每批最多 50 个 QID。以下内容均持久化：

- 成功的 `P2048` 高度和单位归一化结果；
- 无 `P2048` 的负结果；
- 原始实体 JSON；
- 请求错误记录。

负结果同样重要：它防止同一个无高度地标在每次生成时反复请求。

默认只读缓存，不联网：

```bash
WIKIDATA_HEIGHT_AUTO_FETCH=0
OVERTURE_AUTO_DOWNLOAD=0
```

显式预热时才打开对应变量。Wikidata 的小批量地标查询和 Overture 的区域建筑
下载应分开执行，不能因为生成任务开始就隐式消耗大量海外流量。

## 高度选择顺序

```text
OSM height
→ OSM building:levels
→ 已缓存 Wikidata 地标高度
→ 通过几何门禁的 Overture 高度
→ nDSM 最后栅格回退
→ 默认高度
```

nDSM 已降到 Overture 之后。所有来源只提供建筑高度证据；最终模型 Z 值仍由
确定性的高度压缩、地标策略、打印层高和稳定性规则计算。

## 从真实高度到模型 Z

高度库只保存来源观测，不保存某次模型的毫米高度。生成时使用
`city-relative-log-layer-v1`：

1. OSM 明确高度、OSM 楼层、Wikidata 和通过门禁的 Overture 共同描述城市高度
   分布；nDSM 和默认值不进入可信分布；
2. 天际线压缩上限只由 OSM 明确高度、Wikidata 和 Overture 的显式/测量高度上尾
   决定，避免几十万条普通 `building:levels` 把摩天楼身份淹没；少于 20 条时取
   样本最大值，样本足够时取 P99.5，并以 150 m/1200 m 作为下限/异常保护；
   已缓存 Wikidata 身份地标的最高可信值始终参与上限，避免少量真正地标又被大量
   普通显式高度的分位数压平；
3. 在城市上限内做对数压缩，避免 269 m 与 468 m 地标都被旧的固定 150 m 上限
   压成同一高度；
4. 最终毫米高度向上取整到完整的打印层高，避免切片时被舍入掉半层特征。

因此数据和渲染策略可以分别演进：同一份高度证据能用于不同模型尺寸、喷嘴与
层高。高度库的稳定指纹进入 building tile 和 pipeline layer 缓存键；仅刷新相同
原始响应不会触发重算，归一化高度或匹配证据发生变化才会失效。

## 运维命令

查看来源数量、覆盖范围、地标正/负缓存、RTree 和 SQLite 完整性：

```bash
python tools/building_height_cache.py status
```

同时校验已登记原始文件的 SHA-256：

```bash
python tools/building_height_cache.py status --verify-raw
```

SQLite 在线备份（目标已存在时拒绝覆盖）：

```bash
python tools/building_height_cache.py backup /safe/path/building_heights.sqlite3
```

导出为可移植 GeoParquet：

```bash
python tools/building_height_cache.py export /safe/path/building_heights.parquet
```

从 Windows 金库恢复已经通过在线备份得到的独立 SQLite 到 WSL 热工作区：

```bash
bash tools/install_height_cache_snapshot.sh \
  /mnt/f/map-generator-vault/incoming/height-cache-20260826.tar.gz \
  /home/mapworker/map-generator-simple
```

安装器先做 SQLite 完整性和正高度数量校验；若热库已存在，会复制到
`data/height_cache/backups/` 后再原子替换。安装完成会输出实际证据指纹，不能只
以命令退出码作为成功依据。

### 为已生成城市预热地标高度

先从现有 PBF 的 25 km 成品取景框中筛出已经带 `wikidata=Q...` 身份的建筑。
这一步只读取本地 PBF，不下载地图数据：

```bash
python tools/collect_showcase_landmarks.py \
  --pbf-dir pbf_cache --size-km 25 \
  --output data/height_cache/showcase_landmarks.json
```

不同数据节点生成的清单可以一起传给预热命令。默认先跳过已有 OSM `height` 或
`building:levels` 的 QID，再用紧凑 SPARQL 批次发现真正含 `P2048` 的地标，最后
只对命中项查询完整实体。这样避免为上万个无高度实体下载全部 claims。成功高度、
无高度负结果、请求元数据和原始响应都会写入 SQLite：

```bash
python tools/prefetch_landmark_heights.py \
  data/height_cache/showcase_landmarks*.json
```

同时保留便于人工检查的 `showcase_landmarks_enriched.json` 和
`showcase_landmark_heights.csv`。生成时 OSM 明确高度仍然优先于 Wikidata。

需要审计标签而不考虑流量时可显式使用 `--query-policy all`；常规预热不得启用。
若完整实体接口返回 429，紧凑 SPARQL 高度仍会先落库，实体补全按最多 50 个一批
延后重试，不会重复扫描已经缓存的负结果。

## 诊断与验收

正式 3MF 的 `design_spec.json` 使用 schema 1.3，在
`evidence.building_height_sources` 记录本次实际采用的来源数量，例如：

```json
{
  "osm_height": 18,
  "osm_levels": 203,
  "wikidata": 3,
  "overture": 842,
  "ndsm": 0,
  "default": 17560
}
```

`evidence.building_height.store` 还记录规范化库指纹、观测/正负缓存数量；
`evidence.building_height.mapping` 记录城市高度分位数、映射上限、打印层高和最终
可打印地标的模型高度范围。由此可以回答某个 3MF 到底用了哪批高度证据、为什么
形成当前 Z，而不是仅证明数据库文件存在。

“缓存存在”不是验收结果；必须同时检查实际匹配数、默认高度占比和高度来源分布。
对已有、可信的 pipeline GDF 缓存可以先做轻量采用审计，不必为了检查高度重新生成
道路、水体、DEM 和网格：

```bash
python tools/audit_city_height_usage.py \
  cache/pipeline/showcase_chicago_25km_aesthetic/gdfs_v1_4bfeedffe027.pkl \
  --city chicago \
  --output output/height_audit/chicago.json
```

报告包含建筑总数、实际来源数量、QID 命中、城市高度分布上限和映射后的基础模型
高度。它只读取项目自产的 pickle；不得对用户上传或来源不明的 pickle 使用该工具。
最终能否进入可打印模型，仍以正式 3MF 的 DesignSpec 和验证器为准。

## 后续来源

Microsoft Global ML Buildings 可以按同一 `height_observations` 协议导入；原始
分区文件、QuadKey、版本与许可需写入覆盖记录。Microsoft TEMPO、Google Open
Buildings 2.5D 和 Copernicus Urban Atlas 是街区级栅格，不应伪装成单栋高度，
后续应进入独立的 `height_zone_evidence` 表。

多计算节点当前各自持有本地 SQLite。SQLite 文件不得直接放在不可靠的网络文件
系统上供多个节点同时写；正确方案是节点本地写入、任务结束导出增量，由数据节点
合并并做不可变备份。该集群同步流程尚未实施。
