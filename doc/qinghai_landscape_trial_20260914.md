# 青海湖自然景观阶段试跑

本次验证现有 canonical pipeline 对超 25 km、建筑稀疏区域的兼容性。
先采用本地完整 SRTM 瓦片覆盖的青海湖东南岸；不是完整青海湖。

- 取景：WGS84 `36.25,100.02,36.95,100.98`，84.657 × 78.997 km，6687.7 km²。
- OSM：Geofabrik `qinghai-260913.osm.pbf`，33,076,034 字节，osmium 全文件读取通过。
- DEM：`dem_cache/srtm/N36/N36E100.hgt`。未使用平地降级。
- 模型：196 × 182.9 mm，保留 DEM 地形、水体与来源道路。
- 自动识别：`water_landscape / characteristic_shore_relief`。
- 源建筑 431 个，打印尺寸过滤后为零；自然策略清除了前处理的 57 个城市底块。

## 实现

S5 的 active landscape 分支跳过 block-first 和城市 B+C 微纹理。
S6 清理自己拥有的图层副本中的城市底块、聚合建筑及其平行属性，
保留来源地标及自然图层。S9 仅在 active landscape 下允许记录建筑省略；
水体、道路、城市模式的非零来源检查保持有效。
DesignSpec 如实记录自然模式的城市底块关闭状态。

同时修复历史冻结摘要兼容问题：MappingProxy 按字典递归序列化，
恢复 S6/S8 表面一致性检查，不再跳过摘要不一致。

## 结果与边界

v2 成功走完 S10，约 3.99 MB 3MF，耗时 117.7 秒（本地数据、部分缓存）。
地形 241,364 面、水体 18,318 面、道路 106,800 面，城市底块和建筑为零。
实际网格斜视图可见湖岸、山体起伏和环湖道路。

独立打印校验发现：v2 的城市底块声明错误（已修复并重跑 v3）；
V15 检测到水体布尔操作之后的地形长边（最大约 53.7 mm），
尚未解决。因此当前成果仅为观感/兼容性样张，不代表打印验收通过。
全湖所需的其他高程瓦片下载未成功，停止下载；不宣称全湖已验证。

回归：1201 passed、2 skipped、11 deselected；之后的说明文件修正相关测试 9 passed。

最终 v3 同样完成 S10，耗时 125.0 秒，3MF 3.99 MB。
独立校验确认 V14 声明问题已消除，只剩 V15 地形长边，零警告。
当前诊断结论为 `rerun`（打印目的）；可作为大范围自然景观的观感样张查看。
最终产物位于 `output/qinghai_southeast_85km_v3/`，包括
`actual_oblique.png`、`actual_topdown.png`、`actual_render.json`、
`validation.json`、`design_spec.json` 和实际 3MF。

## 复现

```sh
AMAP_WATER_AUTO_FETCH=0 .venv/bin/python generate_model.py \
  --bbox 36.25,100.02,36.95,100.98 \
  --pbf pbf_cache/qinghai-260913.osm.pbf \
  --elevation-file dem_cache/srtm/N36/N36E100.hgt \
  --city qinghai_southeast_85km_v3 --amap-salience off \
  --no-snap --no-vegetation --png --review-png
```
