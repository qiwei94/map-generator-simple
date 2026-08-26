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

## 诊断与验收

正式 3MF 的 `design_spec.json` 使用 schema 1.2，在
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

“缓存存在”不是验收结果；必须同时检查实际匹配数、默认高度占比和高度来源分布。

## 后续来源

Microsoft Global ML Buildings 可以按同一 `height_observations` 协议导入；原始
分区文件、QuadKey、版本与许可需写入覆盖记录。Microsoft TEMPO、Google Open
Buildings 2.5D 和 Copernicus Urban Atlas 是街区级栅格，不应伪装成单栋高度，
后续应进入独立的 `height_zone_evidence` 表。

多计算节点当前各自持有本地 SQLite。SQLite 文件不得直接放在不可靠的网络文件
系统上供多个节点同时写；正确方案是节点本地写入、任务结束导出增量，由数据节点
合并并做不可变备份。该集群同步流程尚未实施。
