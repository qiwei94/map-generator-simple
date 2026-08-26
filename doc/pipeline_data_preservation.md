# Pipeline 与交叉验证数据保存规范

项目壁垒由可复现的处理链和证据数据共同组成。仅保存最终 PNG/3MF 不足以恢复
城市骨架选择、道路/水体补全、建筑高度判定和可打印性裁切。因此保存对象分为：

1. **代码与规则**：完整 Git refs、DesignSpec、参数与验证器；
2. **原始证据**：PBF、DEM、数据源版本、下载地址、许可和原始校验和；
3. **交叉验证证据**：高德/OSM/Wikidata 等原始响应、负缓存、匹配结果；
4. **中间产物**：实际渲染使用的 GDF/pipeline cache，而非事后重算的近似结果；
5. **成品与验收**：PNG、GLB、3MF、`design_spec.json`、要素计数和验证报告。

## Windows 存储职责

Windows 是主计算节点兼大容量冷归档节点，但两种用途必须分开：

- WSL SSD：`/home/mapworker/map-generator-simple`，放当前代码和热 pipeline/PBF
  缓存；正式生成从这里读取，避免机械盘拖慢随机 I/O。
- F 盘冷归档：`/mnt/f/map-generator-vault`，保存不可变里程碑快照、完整 Git
  bundle、数据库备份、校验清单和恢复说明。
- 既有数据源：`/mnt/f/map_gen_cache`，保持原位置；金库只登记文件目录和规模，
  不在同一块盘上重复复制 167 GB。

推荐目录：

```text
/mnt/f/map-generator-vault/
  LATEST
  snapshots/<date>-<commit>-<purpose>/
    METADATA.tsv
    code/*.bundle
    evidence/height_cache*.tar.*
    runtime/pipeline_cache/
    runtime/pbf_cache/
    manifests/SHA256SUMS
    manifests/SHA256SUMS.verify.txt
    manifests/existing-map-gen-cache.tsv
```

`snapshots/` 中已有名字禁止覆盖。发生失败时保留 `staging/*.partial` 供人工检查，
脚本不会自行删除数据。修复失败原因后可设置 `MAP_GENERATOR_VAULT_RESUME=1`
继续同一 partial；脚本会拒绝内容不同的输入文件，并用 `rsync` 校验已有副本。

## 创建里程碑快照

先在 controller 生成包含所有 refs 的 Git bundle，并用项目缓存工具备份 SQLite；
再将 bundle 和高度缓存压缩包放到 Windows F 盘 `incoming/`。确认 Windows 没有生成
任务后，在 WSL 执行：

```bash
bash tools/snapshot_windows_pipeline_vault.sh \
  20260826-<commit>-landmark-height \
  /mnt/f/map-generator-vault/incoming/map-generator-simple-<commit>.bundle \
  /mnt/f/map-generator-vault/incoming/height-cache-20260826.tar.gz
```

脚本会保存 Windows 当前工作副本的所有 refs、复制当前 pipeline/PBF 热数据、登记
既有 F 盘数据，并对快照中的每个文件执行 SHA-256 回读验证。正式快照完成后才从
`staging` 原子移动到 `snapshots`。

## 恢复验收

代码恢复：

```bash
git clone /mnt/f/map-generator-vault/snapshots/<id>/code/<bundle> restored-repo
git -C restored-repo branch -a
```

数据恢复前先验签：

```bash
cd /mnt/f/map-generator-vault/snapshots/<id>
sha256sum -c manifests/SHA256SUMS
```

恢复不能只检查文件存在，还必须运行非慢速测试、对代表城市核对道路/水体/建筑
数量，并生成真实 3MF 通过项目验证器。缓存只用于加速，不能代替来源和参数记录。

高度证据恢复到 Windows WSL 热工作区时使用仓库脚本，避免把 WAL 状态中的运行库
直接复制成损坏文件：

```bash
bash tools/install_height_cache_snapshot.sh \
  /mnt/f/map-generator-vault/incoming/height-cache-20260826.tar.gz \
  /home/mapworker/map-generator-simple
python tools/building_height_cache.py status
```

恢复后必须核对 SQLite `integrity=ok`、正高度地标数和证据指纹；正式生成的
`design_spec.json` schema 1.3 会携带同一指纹及模型 Z 映射证据。

## 后续约束

- 每个新数据源必须记录 provider、版本/日期、空间范围、许可、原始 URL、校验和；
- API “无结果”也要负缓存，防止昂贵重复查询；
- 重要人工选择和城市地标匹配应进入结构化配置，不只存在聊天或图片标题中；
- 金库不能成为唯一副本：代码继续推 Git，关键 SQLite/清单同步到 `cloud-data`；
- 不自动将 Windows 注册为无限并发 worker，直到队列具备能力匹配与事务租约。

## 首份验收快照（2026-08-26）

- 路径：`/mnt/f/map-generator-vault/snapshots/20260826-54cbd15-pipeline-evidence`
- 大小：9.6 GB；SHA-256 清单 119 项，回读通过 119 项；
- 代码：controller 全量 Git bundle 包含 45 refs，目标提交 `54cbd15`；
- 热数据备份：17 个 PBF、92 个 pipeline cache 文件；
- 高度证据：地标 SQLite、正/负缓存、JSON/CSV 清单及安全备份；
- 既有 F 盘数据：约 167 GB，已生成逐文件目录和两级规模清单，未重复复制；
- Windows 工作区随后快进到 `337bb1f`，非慢速测试结果为
  `552 passed, 2 skipped, 11 deselected`；该提交已推送 GitHub，增量 bundle 保存在
  金库 `incoming/`。
