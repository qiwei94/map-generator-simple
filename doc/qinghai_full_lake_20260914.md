# 完整青海湖试跑

取景 WGS84：`36.35,99.4,37.45,100.97`，约 138.36 × 123.77 km，
面积 17125.4 km²，模型最长边 196 mm。

根据本地 Geofabrik 青海省 PBF 中 `wikidata=Q201294` 的湖面多边形确定范围。
实际源湖岸边界为 `99.6104733,36.5481062,100.7602018,37.2374097`。
已用几何包含关系确认整个源湖面在取景内，四周保留山体空间。

## 数据

- `pbf_cache/qinghai-260913.osm.pbf`，完整文件经过 osmium 读取检查。
- SRTM：N36E099、N36E100、N37E099、N37E100，3601×3601 原始瓦片。
- 缺少的三块从 AWS elevation-tiles-prod 下载；分段响应范围与长度检查、
  gzip CRC 检查通过，原始瓦片无 nodata，相邻瓦片公共边缘高程差全部为零。
- 拼接为 `dem_cache/qinghai_full_90m.tif`，以平均值重采样到 3 角秒。
  拼接外侧额外边缘的空白像素被裁掉，实际取景内无缺失或填造地形。
- `dem_cache/qinghai_full_input_manifest.json` 保存源哈希、范围与派生信息。

## 本次代码修正

完整内陆湖没有海岸接触，其河网分数也可能较低；补充了主湖面占比较大、
建筑覆盖极低的自然场景识别，保留城市及跨来源城市证据的优先判断。
全湖识别为 `water_landscape / great_lake_shore`，不生成城市填充。

水体布尔裁切可能合并共面的地形三角形，形成超过打印边长限制的长边。
在 active landscape 下按共享边进行符合拓扑的表面细分，保持外形、
体积和封闭性，继续使用原有边长阈值与独立校验。

## 复现

```sh
AMAP_WATER_AUTO_FETCH=0 .venv/bin/python generate_model.py \
  --bbox 36.35,99.4,37.45,100.97 \
  --pbf pbf_cache/qinghai-260913.osm.pbf \
  --elevation-file dem_cache/qinghai_full_90m.tif \
  --city qinghai_full_lake_140km_v2 --amap-salience off \
  --no-snap --no-vegetation --png --review-png
```

回归：1202 passed、2 skipped、11 deselected；之后的完整内陆湖分类与
水体针对性测试为 26 passed。
