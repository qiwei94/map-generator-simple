# 巴黎 25 km：canonical 整城验证

状态：`rerun`。S7 巴黎整图和中文报告已生成；S8 地标物化失败，没有正式 3MF。
不是画廊发布，也不是切片验收。未继续重跑相同视觉方案。

## 复现身份

- 节点：controller，M1 Pro，10 核，16 GiB；原生 osmium + 项目 `.venv`。
- 分支：`agent/final-block-base-clearance-v17`。
- 基础提交：`3915c543ff957f6f58147049454e1dc02af3bc26`，加本地未提交的 shared-city-surface-v2 修复。
- 启动时 324 个受版本控制或未忽略的 Python 文件，以相对路径→内容 SHA-256 的排序 JSON 再散列：
  `a3666fcc9b6b2e0b5ca73567cb1e800e3eb15ec0e34450a621c2d80db63ef0b2`。
- PBF：`pbf_cache/ile-de-france-260902.osm.pbf`，SHA-256
  `c8aa9657070dcb0a283e1e33d58d9b9d97232bc88b0afe09afefb4a5685bdded`。
- DEM：`cache/srtm/N48E002.hgt`，SHA-256
  `b6f8aaf1fae9820a1dd8eaa1b3c6c4b0102340c27e2bdcda32921c995b7f63a4`。
- bbox：`48.74355353,2.18153414,48.96964647,2.52286586`。
- 实际投影范围：约 25.251 × 24.920 km，成品城市范围 196 × 193.426 mm。
- 原始缓存：新内容身份首次离线提取；派生缓存：新身份首次计算。不能当作热缓存速度。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -u generate_model.py \
  --bbox 48.74355353,2.18153414,48.96964647,2.52286586 \
  --pbf pbf_cache/ile-de-france-260902.osm.pbf \
  --city paris_25km_canonical_20260905_v2 \
  --elevation-file cache/srtm/N48E002.hgt \
  --no-snap --amap-salience cache --png --review-png
```

输出目录：`output/paris_25km_canonical_20260905_v2/`。
日志：`tmp/paris_25km_canonical_20260905_v2.log`。
不启用网络视觉引导、AI 评审、nDSM 或植被几何；不改线上服务。

## 先行地形检查

`output/paris_25km_canonical_20260905_terrain/`，同 bbox、同本地 DEM、1024 请求分辨率。
地形单独生成约 14.6 秒：341,932 面、水密、QEM 关闭，最大表面 XY 边长 0.671 mm，
超过 2 / 5 mm 的表面边均为零。实体 Z 跨度 1.795 mm，地形计划指纹与旧巴黎一致。
独立项目验证器 `strict_passed=true`，零错误、零警告。诊断斜视图放大 Z 仅供检查；
整城对比必须使用真实比例斜视图。

## 比较边界

- 旧候选：`output/paris_25km_current_20260904_v2/`，相同 bbox/比例，可比较执行改动。
  但不是单变量 A/B：本次高度库 fingerprint 为 `ba6f9f2104e60004`、628,727 条观测；
  旧日志为 `4d1329435c94f92d`、597,327 条。PBF 提取建筑/道路/水体数量一致，
  高度库已更新，因此高度差异不能全部归因于执行修复。
- City demo：`output/paris_reference_review_20260904/actual_mesh/`；参考缺少地理配准，
  只做构图、粒度、道路层级等定性比较，不宣称逐像素同区域误差。
- S7 PNG 与正式 3MF 的实际俯视/斜视必须分别检查；导出成功不等于审美或可打印通过。
- S11 仍需真实、哈希绑定的切片证据，不能用地形检查或小场景单测代替。

## 本次暴露的观测债务

S6 聚合阶段仍缺少细分进度与耗时；ledger 能证明 Stage 正在运行，却不能显示已完成
街块数。一秒本机进程采样显示 Python/GEOS 多边形合并等计算仍在推进，不能把长时间
没有新日志等同于卡死。后续应在 S6 内部添加有界批次计数和子步骤耗时，不改变 Stage
输入输出，也不因观测而跨 Stage 修改策略。

## 结果与耗时

run：`65897fd390f64ff5a1c83946f26aaa84`，attempt：`19dcedb698514a49abb1a23c2338b62f`。
总计约 30 分 36 秒后在 S8 失败；S6 约 23 分 7 秒，S3 约 199 秒，S7 约 12.5 秒。
旧巴黎 S6 约 55 分 55 秒；两个运行的输入高度库和执行方式不同，不能把耗时差全部
归因于某一个优化，也不能将本次失败前耗时当作成功模型的生成时间。

### 平面结果：仍不符合参考风格

- 全图已生成：`paris_25km_canonical_20260905_v2_topdown.png`，是 S7 平面图，非实际 mesh 渲染。
- 塞纳河大轮廓存在，较旧乱线图少了一些交叉干扰；但道路仍偏粗，局部交汇抢眼，
  建筑/街块的连续覆盖不足。参考 demo 的街块密度与道路层级仍明显更协调。
- S6 裁切前 25,545 块，裁切后 28,494 块；确定性抽样短轴中位数从 0.912 降到
  0.789 mm，P10 从 0.556 降到 0.115 mm。仍有切碎/小块问题，不能把几何一致当作可打印。
- 图层面积相加从约 576.52 降到 454.26 km²，约保留 78.8%；这是含重叠的面积和，
  不是城市真实覆盖率。日志的 `area_gain=286.92x` 是与 merge 路径中只剩 429 个
  demoted baseline 组件比较，不表示把原始建筑面积放大 287 倍，不宜当作质量指标。
- 结论：`rerun`。本轮没有发布到线上；先讨论街缝宽度、切碎后粒度约束及密度表达，
  不在用户尚未审图时继续修改审美策略。

### S8 数值精度缺陷

地标挤出误差 `2.0444691677095567e-06 mm²` 触发严格轮廓门禁。已用位于局部坐标
10 km 处的小矩形、相同巴黎比例独立复现：旧 `to_mesh()` 输出 float32，造成约
`1.91746e-06 mm²` 误差；内部几何并未主动重切。

修复仅将 `_TEXTURE_STYLE_OF_DEEPSEEK/prepared_surface.py` 改为 `to_mesh64()`，
不放宽 footprint/Z/体积阈值，不修改 S6 轮廓。补充远离原点的小轮廓回归；包括实际
GLB/3MF 重读一致性在内的定向测试 52 项通过。该修复发生在本次失败之后，
**不意味着这份巴黎已经生成正式模型**。失败 ledger、日志及 S7 图保持原样。

修复后全量非 slow 回归：1,057 passed、2 skipped、11 deselected、54 条依赖弃用警告，
92.93 秒；`git diff --check` 通过。所有改动仍在本地，未提交、未推送、未部署。
