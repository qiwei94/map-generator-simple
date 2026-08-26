# 建筑高度证据进入可打印 3MF：2026-08-26 验收

## 结论

建筑高度不再只是下载后保存的旁路数据。提交 `e4d33a7`–`2971683` 已将离线证据
接入 building tile、pipeline layer cache、城市相对 Z 映射、打印层高量化与
DesignSpec 1.3；Windows WSL 已恢复热数据库，并用芝加哥 25 km 生成真实 3MF。

项目验证器 V1–V14 全部通过：`passed=true`、`strict_passed=true`、
`0 errors / 0 warnings`。

## 数据与决策边界

- SQLite 保存原始归一化高度、来源、覆盖范围、Wikidata 正/负缓存；不保存某个
  模型尺寸专用的 Z。
- 运行时来源顺序为 OSM `height`、OSM `building:levels`、已缓存 Wikidata、
  Overture、nDSM、默认值。
- 城市整体分布使用所有可信矢量高度；天际线上限使用 OSM 明确高度、Wikidata、
  Overture 的上尾，并强制保住已识别 Wikidata 地标的最高可信值。普通楼层数不能
  再把少量摩天楼压回固定 150 m 上限。
- 模型 Z 经对数压缩、类别偏移和窄建筑稳定性规则后，向上量化到完整打印层。
  LLM 不控制 mesh 顶点、全局 Z 或布尔运算。
- 数据库内容修订号进入缓存键；同证据刷新不重算，归一化证据变化才使最终 building
  tile 与 layer cache 失效。

## 轻量采用审计

### 芝加哥 25 km（controller 既有 GDF）

- 建筑：1,421,148；
- 来源：default 862,310、OSM height 855、OSM levels 557,958、Wikidata 25；
- Wikidata：2,081 个带标签行、1,006 个不同 QID，实际高度命中 25 行/13 QID；
- 天际线上限：442.1 m；
- 代表结果：Willis Tower 442.1 m → 4.08 mm，St. Regis 362.9 m → 3.96 mm，
  311 South Wacker 292.913 m → 3.96 mm（均为类别偏移前、已按 0.12 mm 层高量化
  的基础审计值）。

旧 P99.5 初版曾被 55.8 万条普通楼层高度淹没，错误回退到 150 m；本次真实审计
发现并修复了该反例，不能把“数据库命中”当作“视觉身份已保留”。

### 新加坡 25 km（Windows 热 GDF）

- 建筑：206,984；
- 来源：default 154,097、OSM height 4,016、OSM levels 48,867、Wikidata 4；
- 天际线上限：280 m；CapitaSpring 280 m → 4.08 mm，PARKROYAL Pickering
  89 m → 3.72 mm；
- 审计报告：
  `/home/mapworker/map-generator-simple/output/height_audit/singapore-25km.json`。

上海清单已有 4 个无 OSM 高度的可补地标（东方明珠 468 m、时代金融中心 269 m、
交银金融大厦 265 m、浦东清真寺 36 m）；香港已有 3 个（中银大厦 367.4 m、
力宝中心 186 m、重庆大厦 55 m）。它们已在离线库中，正式生成按 QID 采用；清单
命中不替代最终城市 3MF 验收。

## 芝加哥 25 km 正式验收

Windows WSL（32 GB 内存）使用现有 Illinois PBF、离线高度库和既有
`data/print_profiles/chicago_25km_dense_detail.json`，未访问远端高度服务：

```bash
WIKIDATA_HEIGHT_AUTO_FETCH=0 \
OVERTURE_AUTO_DOWNLOAD=0 \
AMAP_WATER_AUTO_FETCH=0 \
.venv/bin/python generate_city_legacy.py \
  --bbox 41.7650535,-87.8587977,41.9911465,-87.5571755 \
  --pbf pbf_cache/illinois-latest.osm.pbf \
  --city height_v18_chicago_25km \
  --params-json data/print_profiles/chicago_25km_dense_detail.json \
  --review-png --base-thickness-mm 0.4
```

实测 597.7 秒仅代表该 Windows WSL 节点本次运行，不外推到 16 GB Mac 或 Linux
云机。关键非零证据：

- source：buildings 704,371、roads 248,039、water 1,663；
- printable：building landmarks 4,756、building blocks 10,997、roads 3,460、
  water caps 17、vegetation 2,238、block base 13,908；
- 高度采用：OSM height 429、OSM levels 276,344、Wikidata 13；
- 高度映射：ceiling 442.1 m，打印地标 Z 2.28–7.20 mm，层高 0.12 mm；
- block-base 最终道路间隙 0.84 mm，post intrusion `3e-09 m²`；
- 3MF 50,691,055 bytes，SHA-256
  `527220d6781e2f3d976721df56ba7f67fce1a9b170659bc550fd9a738dd6c138`。

验证命令：

```bash
.venv/bin/python tools/validate_3mf.py \
  output/height_v18_chicago_25km/full_height_v18_chicago_25km_0826_1729.3mf \
  --json
```

## 保存位置

- Windows 热成品：
  `/home/mapworker/map-generator-simple/output/height_v18_chicago_25km/`；
- F 盘不可覆盖成品归档：
  `/mnt/f/map-generator-vault/artifacts/height_v18_chicago_25km-2971683/`；
- F 盘高度库在线备份：
  `/mnt/f/map-generator-vault/incoming/building_heights_20260826_chicago-v18.sqlite3`；
- 备份完整性：SQLite `integrity=ok`、RTree 缺失 0、OSM 观测 314,198、
  Wikidata 正高度 207、负缓存 12,167；归档 3MF 与热成品 SHA-256 一致。

## 已知限制与下一步

- 当前库尚无 Overture footprint observations；海外批量数据融合继续遵守流量暂停
  决定，不因生成任务隐式下载。
- Top-down PNG 不能证明 Z 高度；高度视觉需在 3MF/切片器或后续 GLB 飞越镜头中
  检查。项目验证器证明结构、材料、间隙和水密，不替代人工构图审美。
- 上海、香港应在各自下一次正式 3MF 中核对 DesignSpec 的实际 Wikidata 采用数；
  清单结果只证明证据可用。
- OSM 高度观测已写入节点本地 SQLite；后续应实现节点增量合并，而不是让多个 worker
  直接并发写同一个网络 SQLite。

## 身份—锚点—背景高度角色验收

同日新增 `identity-anchor-background-v2`：准确高度只服务于有明确身份信号的建筑和
少量可靠天际线锚点，普通无名个体建筑保留 footprint 密度但进入低对比高度带。
本机使用已有 Illinois PBF、离线高度库和芝加哥成品参数档，关闭所有远端高度/高德
自动请求，生成芝加哥中心 5 km（25 km²）真实 3MF：

```bash
WIKIDATA_HEIGHT_AUTO_FETCH=0 \
OVERTURE_AUTO_DOWNLOAD=0 \
AMAP_WATER_AUTO_FETCH=0 \
.venv/bin/python generate_city_legacy.py \
  --bbox 41.8601,-87.6528,41.9051,-87.5924 \
  --pbf pbf_cache/illinois-latest.osm.pbf \
  --city height_roles_v2_chicago_5km \
  --params-json data/print_profiles/chicago_25km_dense_detail.json \
  --review-png --base-thickness-mm 0.4
```

最终 `design_spec.json` 记录源建筑 89,108，打印个体建筑 351；高度角色为
`background_stylized=183`、`identity_exact=166`、`visual_anchor_exact=2`，模型高度
min/P50/max 为 3.12/3.12/7.20 mm。普通背景成为数量最多且统一的中位层，精确高度
没有扩散到整座城市。道路 691、水体 9、block base 1,246，均非零。

成品：
`output/height_roles_v2_chicago_5km/full_height_roles_v2_chicago_5km_0826_1754.3mf`
（6,824,044 bytes，SHA-256
`a63b57e14a5fb4aac0c8300d8739a1ce8d349885b499e8b2b33fb8b6784c502f`）。项目验证器
V1–V14 全部通过，`errors=[]`、`warnings=[]`、`strict_passed=true`。

验收过程中还修复两项被真实生成暴露的问题：关闭 auto-params 时 `_cfg` 局部变量
遮蔽会阻断 DesignSpec 导出；无 DEM 起伏时地形只生成配置厚度的一半。修复后平坦
地形也保持完整 4 mm 结构厚度。上述 31.7 秒仅代表本机热缓存下这次 5 km 运行，
不能外推为 25 km 或其他节点性能。
