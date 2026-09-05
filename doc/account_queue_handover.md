# 账号、额度与可靠队列交接

更新日期：2026-08-30
工作分支：以当前开发分支为准

## 已实现

- SQLite WAL 持久化账号库：邮箱验证码、HttpOnly 会话、用户状态、角色和月度额度；
- 身份与用户分表，已为未来微信 UnionID/OpenID 绑定留好结构；
- 任务记录带 `owner_ids`，登录用户只看自己的任务，管理员可看全部；
- 相同请求可跨账号共享正在运行或已完成的结果，不重复扣额度；
- 新任务按类型和最长边收费：快速预览 1、风格图 2、正式模型 5，区域每开始 10 km 乘一档；
- 真正失败自动退额度，重复回调不会重复退款；
- 单 worker 公平队列：账号间轮转、90 秒租约、15 秒心跳、过期回收；
- worker 上传 `.part` 后做 SHA-256 校验，风格产物写入正确的 `style_gallery` 目录，并包含 JSON；
- 产品页增加登录、剩余额度和“我的任务”；管理页增加用户、任务、额度与暂停/恢复控制。

## 游客体验额度

- 首次通过网页服务访问时，服务端签发随机 `studio_guest` HttpOnly Cookie；数据库只保存摘要，不保存明文令牌；
- 每个匿名浏览器会话共有 3 次体验机会。新建风格图、快速预览或正式模型任务各扣 1 次；
- 完全相同的运行中任务或有效缓存直接复用，不扣次数；失败任务和服务重启时无法恢复的本地任务自动退回；
- 第 4 次新计算返回结构化 `guest_quota_exhausted`，前端打开邮箱登录窗口；
- 游客登录后，其任务一次性转入该账号的“我的任务”，游客所有权随即移除；
- 游客与账号任务在任务 API 层按 owner 隔离；历史无 owner 任务仍可凭旧任务令牌找回，但不会出现在“我的任务”中。

环境变量：`GUEST_GENERATION_LIMIT=3`。该限制是匿名浏览器会话级的产品体验限制，不是强抗滥用边界；公网开放前仍应在反向代理增加 IP 速率限制/并发限制，必要时接入人机验证。

## 缓存边界

账号不会隔离地图缓存。OSM、DEM、裁剪结果和无用户隐私的管线瓦片继续跨用户共享。
完整请求键相同则直接共享整个任务结果。照片坐标、用户标注、取景框和画廊区域身份必须进入请求键或私有输入，不能串入公共缓存。

## 上线前置条件

1. 域名与 HTTPS 正常；
2. SMTP 发信已用真实收件箱验证；
3. `/etc/map-generator/studio.env` 已从 `deploy/studio.env.example` 创建，权限为 `600`；
4. `AUTH_SECRET`、`WORKER_TOKEN` 使用独立随机值，仓库内没有真实密钥；
5. 管理员邮箱写入 `ADMIN_EMAILS`；
6. 确认当前没有 `running` 任务，再执行队列服务切换。

初次部署先保持 `AUTH_REQUIRED=0`，验证登录、任务归属、worker 心跳和产物回传。全部通过后再设为 `1`。启用 HTTPS 时必须保持 `AUTH_COOKIE_SECURE=1`。

## 安装与激活

```bash
sudo mkdir -p /etc/map-generator
sudo cp deploy/studio.env.example /etc/map-generator/studio.env
sudo chmod 600 /etc/map-generator/studio.env
sudo editor /etc/map-generator/studio.env

# 只安装 unit，不重启当前服务
sudo bash deploy/setup_queued_studio.sh

# 确认没有正在运行的任务后显式激活
sudo bash deploy/setup_queued_studio.sh --activate
```

脚本的激活路径会再次查询运行任务；只要存在 `running` 就拒绝重启。

## 验证

```bash
pytest tests/ -m "not slow"
curl -fsS http://127.0.0.1/api/auth/config
systemctl status studio.service worker.service
journalctl -u worker.service -n 100 --no-pager
```

验收至少包含：两个账号提交相同区域只产生一个任务；第二个账号不重复扣额度；不同账号的待处理任务轮转；worker 断开超过租约后任务可恢复；失败任务额度退回；正式任务同时得到 3MF、PNG 与 `design_spec.json`。

## 当前上线限制

- 微信扫码登录尚未接入开放平台，只完成身份数据模型；
- 邮箱服务没有真实 SMTP 凭据时不会伪装发送成功；
- 清除全部站点数据可获得新的匿名会话；若公开流量上升，必须增加代理层速率限制与验证码，不能把 Cookie 配额当作唯一防滥用机制；
- SQLite 方案适用于当前单主机 API，未来多 API 实例需迁移 PostgreSQL；
- 队列当前仍以持久化 JSON 保存任务，以 SQLite 保存账号与额度。多 worker 横向扩容前应将任务租约迁入数据库；
- Linux 云机的耗时只能代表当前低配云环境，不能外推为 16GB Mac 性能。
