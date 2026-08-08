# 部署与交接文档（Deployment & Handover）

> 目标：让任何接手的 agent/人 能立刻理解现状、连上设备、继续干活。
> 最后更新：2026-08-09。Git 分支：`v0.2-with-gemeni-advise`，remote: `origin https://github.com/qiwei94/map-generator-simple.git`

---

## 一、产品与代码现状

- 产品「旅程回忆 / Journey Relief Studio」：上传旅行照片 → 还原轨迹/停留点 → 选风格 → 生成可 3D 打印浮雕（draft GLB 预览 + full 3MF）。
- Web 前端：`webapp/static/`（index.html / app.js / style.css，纯静态，版本号 `?v=N` 防缓存）。
- Web 后端：`webapp/server.py`（FastAPI，轻后端，不 import 重管线）。两种运行模式：
  - **本地模式（默认/all-in-one）**：`/api/generate` 直接起 `generate_city.py` 子进程在本机算。单机部署用这个。
  - **Worker 模式（`WORKER_MODE=1`）**：只入队，由独立 `tools/cloud_worker.py` 拉取计算（跨机拆分用）。
- 计算管线：`generate_city.py` + `_TEXTURE_STYLE_OF_DEEPSEEK/`（单线程，内存峰值：draft≈1G，full 甜区≈3.4G，full 西湖≈5.7G）。
- 任务令牌：生成后前端显示 job 令牌 + 复制链接（`?job=xxx`），可找回任务（`/api/jobs/{id}` 持久化在 `tmp/webapp_jobs/_jobs.json`）。
- 会话持久化：localStorage + 云端 `/api/session/{id}`（`?s=xxx` 跨设备恢复）。
- 测试：`pytest tests/ -m "not slow"`（约 213 passed）。

---

## 二、设备清单（重要）

### 机器 A — 数据源 / 原 Web（2核 1.8G，磁盘大）
- 公网 `8.136.0.235`，私网 `172.16.164.53`
- 内存仅 1.8G，**跑不动 full 管线**，定位为数据仓库。
- 数据：`/root/map-cache/pbf_cache/`（80 个 PBF，约 32G）+ `/root/map-cache/dem_cache/`（约 43G）。
- 项目：`/root/map-generator-simple`（git）。
- **NFS 服务端**：`/etc/exports` 导出 `/root/map-cache` 给 `172.16.164.0/24(ro)`。
- 有外网（可 pip、可 git）。

### 机器 B — 计算 / all-in-one（2核 16G，磁盘 40G 偏小）
- 公网 `118.31.184.240`，私网 `172.16.164.54`
- Python 环境：`/usr/local/python3.9/`（从 A 整体拷贝，含 numpy/shapely/geopandas/trimesh/fastapi/uvicorn/pyosmium 等全部依赖）。
- `/opt/pyshim/python3` → 软链到 python3.9（让 `tools/osmium` 的 `#!/usr/bin/env python3` shebang 跑在带 pyosmium 的环境；`tools/osmium` 必须 `chmod +x`）。
- **NFS 客户端**：`/root/map-cache` 以 ro 挂载自 `172.16.164.53:/root/map-cache`。
- 项目：`/root/map-generator-simple`；`pbf_cache` 软链 → `/root/map-cache/pbf_cache`。
- systemd：
  - `studio.service`：all-in-one 本地模式，`STUDIO_PORT=80`，`MAP_GEN_CACHE_DIR=/root/map-cache`，PATH 含 `/opt/pyshim`。← **当前主入口，已 active，公网 `http://118.31.184.240` 可访问（55 城市）**
  - `worker.service`：已 disable（本地模式不需要）。
- 已从 A 补齐系统库到 `/usr/lib64/`：`libssl.so.1.1`、`libcrypto.so.1.1`、`libffi.so.6.0.2`（python3.9 依赖，B 原本缺失）。

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

1. **B 磁盘 40G 装不下全部数据（PBF32G+DEM43G=75G）**。目前数据走 NFS（ro）。**DEM 走 NFS 随机读很慢**，draft 曾超时。→ 建议把 B 磁盘在线扩容到 ≥100G，然后 `rsync` PBF+DEM 到 B 本地盘，改 `MAP_GEN_CACHE_DIR` 与 `pbf_cache` 软链指向本地，速度才能上来。
2. **styles 任务（gen_area_gallery）在 B 上有 aesthetic import 报错**，未修。draft 不受影响。需查 `aesthetic/loop.py` 缺什么依赖。
3. ~~B 的 studio 状态 activating~~ 已解决：补 libssl/libcrypto 后 active，公网可访问。
4. 主入口已切到 B（118.31.184.240）。A 上的旧 studio 可停，A 只留 NFS+数据。

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
