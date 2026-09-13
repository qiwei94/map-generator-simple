# 生成执行一致性：四项修复

> 2026-09-07：新增显式 `block-first` 街区先行路径，以及 review-only 的纯平面终止模式。
> 详见 [街区先行执行与边界](block_first_pipeline_20260907.md)。PNG 不再强制构建三维地形贴合分片，
> 此模式不具备三维/切片验收资格；正式模型仍执行严格接地检查。

> 2026-09-06 Z 接地更新：携带 TerrainSurfacePlan 的新 S6 merge 运行现在冻结
> 城市分片的底面与顶面。`block_base` 随地形保持厚度，`BO/BL` 底面贴地、
> 屋顶为最高支撑点加批准高度。S8 和 GLB 只消费同一计划。
> 普通道路及有完整岸点的直桥也已接入冻结计划；复杂桥梁明确阻断。
> 下文旧“平挤出/质心取高”描述仅适用于旧快照。
> 道路更新详见[道路与桥岸接地记录](road_grounding_item3_20260906.md)。
> 详见[第 3 项记录](z_grounding_item3_20260906.md)，不代表最终打印验收。

状态：2026-09-05，本地代码已接线；未部署、未切片验收。
巴黎整城已运行至 S7 出图，S8 精度错误拦截，详见
[paris_canonical_run_20260905.md](paris_canonical_run_20260905.md)；不可称为整城验收通过。
本页是 shared-city-surface-v2 的执行约束；v1 巴黎局部对照属于历史证据。

## 1. 审批平面到实际网格

S6 固定 BO、Block Base、地标轮廓与逐块高度。S8 的 prepared 路径不再进入旧
`_build_textured/_build_flat` 的过滤/随机扰动分支。每一个批准 polygon 都执行挤出，
独立验证水密、绕向、有限坐标、顶面与原 footprint 的对称差/边界距离、Z 范围和体积。
失败终止，不允许跳过。微小块能否满足打印下限仍应在 S6 决定，不能由 S8 偷删。

产物证据包含 approved/verified/lost 数量、最大 footprint 误差、输入指纹和实际
vertices/faces SHA-256。V8 构造和 S9 入口再次核对同一 S6 计划及实际 mesh。
道路、地标的实际物化证据也保存在间隙报告的 `city_materialization`，随 Context/
DesignSpec 进入管理员观测。不把输入的 passed 直接当作建模成功。

新版平面块使用明确的无随机纹理挤出；旧语义纹理和随机 Z jitter 未纳入 S6 计划，
因此在 canonical 路径禁用。地形生成本身不改，植被仍默认关闭。

## 2. 道路/街缝只有一次平面决策

S6 消费 S3 已批准的 local/major cut lines，使用打印 profile 两档宽度得到连续道路
面域，包括原来已有的街缝；不只显示本次新切掉的碎片。该面域裁到 bbox，避让最终
建筑/街块/地标和水面；只有明确 bridge 且位于批准走廊内的段可以跨水。
原始 foreground 路线不能在下游重新加宽、重新入选。

`surface_road_polygons` 与 `road_surface_plan` 的高度、偏移及指纹是共同输入。
道路用低于普通建筑的表层表达：厚度 `min_surface_height_mm`，底部偏移为其负一半。
默认 0.24 mm 厚、-0.12 mm 底部偏移。PNG 画该面域；GLB/正式 builder 使用同一
严格挤出函数，不再调用各自的 buffer/简化道路路径。彩色旧诊断入口在 prepared 模式
也转入同一平面渲染器，不再随机 tessellation；原始语义/数量请看中文测量报告。

GLB 的 fast 仍可降低地形网格密度，但不能改变批准的城市轮廓和高度。
这里统一的是同一 bbox/比例/计划的城市几何，不是把网页中心 5 km 预览冒充完整
15/25 km 成品。材质、光照、地形采样分辨率和水体单独建模仍不保证逐像素相同。

当前块面与道路采用共享地形计划的质心 Z 采样，保证两种输出一致；不宣称长走廊
沿坡面细分、地形接触、对象互相重叠和切片效果已验收。它们需要斜视与切片验证。

## 3. 原始与派生缓存身份

PBF 取完整 SHA-256；原始 osmium 缓存 namespace 绑定内容，而不是仅依赖文件名/
大小/mtime。进程内以路径及 inode/size/mtime/ctime 复用校验结果，跨进程不信任旧
摘要；取数前后再次核对文件身份。首轮读取大型 PBF 有额外磁盘读成本，不新增下载。

S2 对实际投影后的每族 GeoDataFrame 记录 geometry WKB、全部属性列、CRS、数量的
内容摘要。PBF 身份、投影数据身份和执行 profile 全部进入 S3 参数快照及派生缓存键。
因此同数量的轮廓改动或仅高度标签更新也会失效。旧缓存保留，不删除；需要时离线重提取。

## 4. 唯一新任务入口及兼容边界

| 入口 | 身份与范围 |
|---|---|
| `generate_model.py` | 新 full/review/draft 唯一主入口，固定 canonical-v1 |
| `generate_city_legacy.py` | 底层实现及历史兼容入口；默认 legacy，显式 profile 可进入新版 |
| `generate_city.py` / `generate_cli.py` | 保留历史质量/实验流程，不宣称等价于 canonical |
| `tools/generate_gallery_draft.py` | 历史缓存草稿工具；网页新任务不再切换到它 |

canonical-v1 明确启用 auto-params、active scene policy、merge-layers；禁止关闭
Block Base 后丢失合并 BO。`generate_model.py` 不允许通过重复 profile 参数降级。
底层 effects 仍在 legacy 大函数，包装入口没有复制生成实现。

网页普通生成、选风格后的 draft 均排入主入口；style JSON 仍进入 S2，任务仍保存正式
与预览的两套 bbox。历史西湖 quality profiles 明确保留原入口。Worker 白名单识别主
入口；此处只是本地源代码改动，没有向节点传文件或重启服务。

示例（显式本地数据）：

```bash
.venv/bin/python generate_model.py --bbox S,W,N,E --pbf /path/city.osm.pbf \
  --city paris --elevation-file /path/dem.tif --amap-salience cache \
  --png --review-png
```

只做平面检查时追加 `--draft --review-only`；真正快 GLB 使用 `--draft --preview-fast`。
历史 profile 只能用于兼容对照，不据此认定当前方案已生效。

## 回归范围

`test_surface_execution.py` 覆盖微小批准块不丢失、错误 footprint/Z 拒绝、缺失高程
拒绝、已有街缝连续、抑制线不复活、显式桥梁、实际 GLB 与正式 builder 顶点/面一致。
`test_source_identity.py` 覆盖同名同大小同 mtime 的内容变化，以及同数量轮廓/属性变化。
`test_canonical_entry.py` 和 Web profile 测试覆盖主入口、启用参数及禁止旧 draft 旁路。
这些是执行一致性回归，不是巴黎审美达标或可打印性证明。

巴黎整城暴露了 `to_mesh()` 输出 float32 的轮廓误差；共享挤出已改用 `to_mesh64()`，
新增远离原点的小轮廓测试。阈值不变，整城尚未在此修复后重跑。

本轮最新离线回归（包括整城触发的精度修复）：1,057 passed、2 skipped、11 deselected（slow），约 93 秒。
实际小场景 GLB 与生产 exporter 写出的 3MF 重读后，街块/道路/地标顶点在
1e-5 mm 容差内相同，三角面索引相同；不借此冒充整城输出或切片验收。
