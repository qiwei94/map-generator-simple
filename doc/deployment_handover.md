# 部署与交接文档（Deployment & Handover）

> 目标：让任何接手的 agent/人 能立刻理解现状、连上设备、继续干活。
> 最后更新：2026-08-10。Git 分支：`v0.2-with-gemeni-advise`，remote: `origin https://github.com/qiwei94/map-generator-simple.git`

---

## 一·〇、整体架构

```
                    公网 HTTP
  用户手机/PC ─────────────────────────────▶  机器 B（计算主力 / all-in-one）
  http://118.31.184.240                     ┌─────────────────────────────────┐
                                            │ studio.service（FastAPI :80）       │
                                            │  ├─ 静态页 webapp/static/（5步流程） │
                                            │  └─ /api/generate → 子进程          │
                                            │      generate_city.py（单线程）    │
                                            │      draft≈1G内存 / full≈3.4~5.7G │
                                            │ /root/map-cache（75G 本地数据）     │
                                            │  ├─ pbf_cache/  80个PBF (32G)      │
                                            │  └─ dem_cache/  SRTM瓦片 (43G)     │
                                            └───────────────┬─────────────────┘
                                                 内网 rsync（一次性全量，后续增量同步）
                                            ┌───────────────┴─────────────────┐
                                            │ 机器 A（纯数据仓库 / 备份）       │
                                            │ 80个PBF + 43G DEM 完整副本；      │
                                            │ 后续全球新数据先落 A，再 rsync 到 B │
                                            │ NFS 导出仅作备用，生产不走 NFS      │
                                            └─────────────────────────────────┘
```

数据流：用户选区域 → B 本地 PBF(osmium 裁剪) + 本地 DEM(SRTM 瓦片) → 管线生成 draft GLB / full 3MF → 前端轮询 `/api/jobs/{id}` 拿结果。全程无外网依赖（数据已本地化）。

## 一、产品与代码现状

- 产品「旅程回忆 / Journey Relief Studio」：上传旅行照片 → 还原轨迹/停留点 → 选风格 → 生成可 3D 打印浮雕（draft GLB 预览 + full 3MF）。
- Web 前端：`webapp/static/`（index.html / app.js / style.css，纯静态，版本号 `?v=N` 防缓存）。
- Web 后端：`webapp/server.py`（FastAPI，轻后端，不 import 重管线）。两种运行模式：
  - **本地模式（默认/all-in-one）**：`/api/generate` 直接起 `generate_city.py` 子进程在本机算。单机部署用这个。
  - **Worker 模式（`WORKER_MODE=1`）**：只入队，由独立 `tools/cloud_worker.py` 拉取计算（跨机拆分用）。
- 计算管线：`generate_city.py` + `_TEXTURE_STYLE_OF_DEEPSEEK/`（单线程，内存峰值：draft≈1G，full 甜区≈3.4G，full 西湖≈5.7G）。
- 任务令牌：生成后前端显示 job 令牌 + 复制链接（`?job=xxx`），可找回任务（`/api/jobs/{id}` 持久化在 `tmp/webapp_jobs/_jobs.json`）。
- 会话持久化：localStorage + 云端 `/api/session/{id}`（`?s=xxx` 跨设备恢复）。
- 测试：`pytest tests/ -m "not slow"`（约 223 passed）。

### 重叠区域缓存（snap-to-grid 量化，2026-08-10 上线）

痛点：两个用户框选同一区域（如西湖 ~10km）但 bbox 稍有偏移 → 缓存 key 全不同 → 整条管线重算（~5-10 分钟）。

机制：`_TEXTURE_STYLE_OF_DEEPSEEK/_tile_grid.py` 的 `snap_bbox` 把**取数框**量化到 0.05°（≈5.5km）绝对网格（south/west 向下取整、north/east 向上取整），**输出框**仍是用户精确 bbox（area_km2/scale/origin/最终裁剪全部按精确框，输出质量不变）。偏移 < 一个网格的请求量化后取数框相同，全链路命中：
- 图层 GeoJSON：`tmp/osmium_{layer}_{snapbbox}.geojson`（空结果也缓存，避免水体空框重跑 105s 的 osmium smart extract）
- 高程网格：量化框下缓存 key 自动稳定
- preprocess 结果：`cache/pipeline/snap_{snapbbox}/`（在量化框坐标系计算，复用时平移+裁剪回精确框）
- 缓存写入为原子写（tmp + `os.replace`），并发安全

实测（B，西湖 ~10km，draft）：冷启动 ~640s → 偏移 ~550m 的第二请求 **~30s**。

**瓦片级缓存（Phase 2，同批上线）**：偏移跨过网格线时量化框会整体变大并 miss，瓦片级缓存解决部分复用：
- 图层瓦片：`cache/tiles/{layer}/{ix}_{iy}.geojson`（每瓦片 0.05°，提取时带 ~200m buffer 保证跨界要素完整）；合并时按 `osm_type+osm_id` 去重（pyosmium shim 的 export 已输出 osm_id；无 id 时降级几何指纹）。
- 高程瓦片：`cache/grids/tiles/elevtile_{ix}_{iy}_61.npy`，查询时拼接（共享边界去重）后重采样裁剪到精确框；平滑在拼接后整框做，无瓦片接缝。
- 取数策略：全框缓存命中→直接返回；瓦片全缺→全框提取一次（单次全量 PBF 读，最快）+ 拆入瓦片；部分缺失→只提取缺失瓦片（缺 ≥2 块时合并成一次提取再拆，避免 relation-first 水体逐瓦片重复全量扫 PBF）。
- 写入均原子（tmp + os.replace），并发安全。
- 实测（B，跨双网格线框 ~8km）：首次全框提取后，另一量化框共享瓦片的偏移请求 **~53s**（图层瓦片全 HIT，仅 preprocess 首算）。

运维要点：
- 逃生门：`generate_city.py --no-snap` 回退旧行为；`--no-cache` 关闭 preprocess 缓存。
- 清理缓存：`tmp/osmium_*.geojson`、`cache/pipeline/`、`cache/grids/`、`cache/tiles/`（按需手动删，无自动过期）。
- 跨 UTM 分区的框自动回退精确模式（snap 前后 UTM zone 不一致时）。

### 热门区域预热（tools/prewarm_tiles.py）

读 `cities.json` 热门取景框 → 按量化格去重 → 逐个跑 `generate_city.py --draft` 填满三层缓存。新城市上线 / PBF 数据更新后跑一次：

```bash
# 在 B 上（PATH 需含 tools/osmium）
cd /root/map-generator-simple
export PATH=/root/map-generator-simple/tools:$PATH
/usr/local/python3.9/bin/python3.9 tools/prewarm_tiles.py --list      # 先看计划
/usr/local/python3.9/bin/python3.9 tools/prewarm_tiles.py --only hangzhou_westlake
/usr/local/python3.9/bin/python3.9 tools/prewarm_tiles.py             # 全量（2 核下每格约 5~10 分钟）
```

---

## 一·二、怎么用（使用指南）

### 用户视角（产品使用）
1. 手机/浏览器打开 **http://118.31.184.240**（无需安装，无需登录）。
2. 五步流程：**选位置 → 定取景 → 挑风格 → 3D预览 → 打印文件**；也可上传旅行照片走旅程模式（自动还原轨迹/停留点）。
3. draft 预览约 4.5 分钟（2 核算力上限）；生成后页面给出 **job 令牌 + 分享链接**（`?job=xxx`），换设备可凭令牌/链接找回任务；会话自动保存（`?s=xxx`）。

### 运维视角（日常管理）
```bash
# 看服务 / 重启 / 看日志（在 B 上）
systemctl status studio ; systemctl restart studio ; tail -f /var/log/studio.log
# 看任务队列与结果
ls /root/map-generator-simple/tmp/webapp_jobs/
cat /root/map-generator-simple/tmp/webapp_jobs/_jobs.json
# 机器健康（CPU/内存/磁盘）
uptime ; free -h ; df -h /
```

### 新数据接入流程（全球扩容）
1. 在 A 上下载新区域的 PBF/DEM 到 `/root/map-cache/`（A 有外网）。
2. **走内网**同步到 B：`rsync -a /root/map-cache/ root@172.16.164.54:/root/map-cache/`。
3. B 无需重启即可用（数据按路径查找）。

### 资源画像（为什么 CPU 高、内存低）
- 管线是 **CPU 密集、基本单线程**：跑任务时 2 核接近满载，所以 CPU 利用率高。
- 内存峰值仅 draft≈1G / full≈3.4~5.7G，16G 内存大量闲置 → 内存利用率低是**正常的、符合预期**。
- 结论：**瓶颈是核数不是内存**。优化方向：升核，或用空闲内存做热门区域预生成缓存；也可以考虑同时跑 2 个任务（内存够，但会互抢 CPU，吞吐提升有限）。

## 二、设备清单（重要）

### 机器 A — 纯数据仓库（2核 1.8G，磁盘大 ~233G）
- 公网 `8.136.0.235`，私网 `172.16.164.53`
- 内存仅 1.8G，**跑不动 full 管线**，只做数据/存储，不参与计算。
- 数据：`/root/map-cache/pbf_cache/`（80 个 PBF，约 32G）+ `/root/map-cache/dem_cache/`（约 43G）。
- 注：A 的 `/data` 盘(99G)有 **I/O 错误不可写**，勿用；数据保持在系统盘 `/root/map-cache`。
- **NFS 服务端**：`/etc/exports` 导出 `/root/map-cache` 给 `172.16.164.0/24(ro)`（仅作备份/备用，生产不走 NFS）。
- 有外网（可 pip、可 git）。

### 机器 B — 计算主力 / all-in-one（2核 16G，磁盘已扩到 120G）
- 公网 `118.31.184.240`，私网 `172.16.164.54`
- 磁盘已在线扩容到 120G（`growpart /dev/vda 3` + `resize2fs /dev/vda3`）。**全量 75G 数据已 rsync 到 B 本地盘**（走内网），不再走 NFS。
- Python 环境：`/usr/local/python3.9/`（从 A 整体拷贝）。`/opt/pyshim/python3` → 软链到 python3.9；`tools/osmium` 必须 `chmod +x`。
- 项目：`/root/map-generator-simple`。
  - `pbf_cache` 软链 → `/root/map-cache/pbf_cache`（本地）
  - **`dem_cache` 软链 → `/root/map-cache/dem_cache`（本地）** ← **极其关键，见已知问题#1**
- systemd：
  - `studio.service`：all-in-one 本地模式，`STUDIO_PORT=80`，`MAP_GEN_CACHE_DIR=/root/map-cache`，PATH 含 `/opt/pyshim`。← **当前主入口 active，公网 `http://118.31.184.240` 可访问**
  - `worker.service`：已 disable（本地模式不需要）。
- 系统库已补齐到 `/usr/lib64/`：`libssl.so.1.1`、`libcrypto.so.1.1`、`libffi.so.6.0.2`。

---

## 三、SSH 连接方式

```bash
# 本地 → A（密钥已配）
ssh root@8.136.0.235

# A → B（A 的密钥已加到 B）
ssh root@8.136.0.235 "ssh root@172.16.164.54 '<cmd>'"

# 本地 → B（跳板）
ssh -J root@8.136.0.235 root@172.16.164.54
# 或 B 已有公网 118.31.184.240，但本地密钥未加到 B，需先 ssh-copy-id 或继续走跳板
```

> 注意：本机是 Windows PowerShell，不支持 `&&`，用 `;`。嵌套 ssh 的引号极易出错，**复杂命令一律写成 .sh 脚本 scp 上去再 `bash xxx.sh`**（见 `deploy/` 目录）。

---

## 四、当前进度 / 已完成

1. 前端 5 步流程 + 照片定位/旅程轨迹/缺口追问/地名命名/风格画廊/任务令牌/会话恢复，全部完成。
2. 数据源优先级链（水体：卫星>OSM Polygon>width 标签>自适应 buffer）+ 建筑动态高度覆盖率，完成。
3. 计算节点 B 环境搭建：python3.9 拷贝、pyosmium shim、NFS 挂载、osmium chmod、studio.service(all-in-one)、worker.service。
4. 任务令牌 + 会话持久化，完成并部署。

## 五、待办 / 已知问题（接手先看这里）

1. **【最重要】DEM 瓦片路径黑洞卡死**。elevation 的离线瓦片只查 `项目目录/dem_cache/srtm/Nxx/xxx.hgt`；若项目里没有 `dem_cache` 目录，会去 AWS（`elevation-tiles-prod.s3.amazonaws.com`）下载，该源在国内云被黑洞 → 进程 0% CPU 死等、任务无限卡死。**必须**保证 `ln -sfn /root/map-cache/dem_cache /root/map-generator-simple/dem_cache`（`setup_allinone.sh` 已含此步）。修好前 draft 曾 0% CPU 卡死；修后 draft 实测 267s 完成。
2. **2 核 CPU 是性能上限**。draft 冷启动约 5~10 分钟、full 更久。非 bug，是算力。缓解：重叠区域量化缓存 + 热门区域预热（见「一、产品与代码现状」），命中后 draft ~30s；要更快：升级 B 核数。
3. ~~styles aesthetic import 报错~~ 已修：`aesthetic/review_agent.py` 加 `from __future__ import annotations`（py3.9 PEP604）。
4. 主入口已切到 B（118.31.184.240）。A 旧 studio 已停，A 只留 NFS+数据（备份）。
5. **两台机器间一切传输走内网（私网 IP），勿走公网**（公网费钱且慢）。
6. ~~水体 relation 全丢（西湖等消失、水体图层空）~~ 已修（2026-08-10）：`tools/osmium_pyosmium.py` 三连缺陷——① tags-filter 只认 `nwr/xxx` 前缀、跳过 `natural=water` 裸表达式；② tags-filter 未挂节点坐标索引导致 way 无几何；③ pybind11 bug 使 `create_multipolygon` 对所有 relation 抛异常被吞。已改为裸表达式按 nwr 解析、挂 locations 索引落盘 `.nli`、relation 几何手工组装（area PBF 建 way 索引→拼环→inner 按包含分配）。**修后需清一次脏水体缓存**：`rm -rf cache/tiles/water cache/pipeline/snap_*`。修后实测：西湖冷框 ~407s（含水体首提），偏移框 **~51s**。

## 六、常用运维命令（在 B 上）

```bash
systemctl status/restart studio        # web+计算
tail -f /var/log/studio.log
systemctl status worker                # 应 disabled
mount | grep map-cache                 # NFS 挂载
```

## 七、deploy/ 目录脚本说明

- `setup_allinone.sh`：在 B 上配置 studio.service(本地模式,80) + disable worker。
- `fix_osmium.sh`：建 /opt/pyshim/python3 软链 + worker PATH。
- `mount_nfs.sh`：B 挂载 A 的 /root/map-cache。
- `setup_worker.sh`：（旧）worker.service 配置，现已被 all-inone 取代。
