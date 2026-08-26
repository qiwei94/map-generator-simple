# 25 km 城市地标高度预热记录（2026-08-26）

## 范围与结果

本轮覆盖正式样品清单 24 城，以及最近生成的“檀香山 · 钻石头山”，共 25 个
25 km 取景框。只查询 OSM 建筑上已有 `wikidata=Q...` 身份的对象，不按名称猜测
实体，也不触发 Overture 或其他大体量海外数据下载。

- 建筑地标对象：29,602
- 唯一 QID：22,773
- 因 OSM 已有明确高度/楼层而跳过远程查询：10,399
- 实际检查 `P2048`：12,374
- Wikidata 高度命中：207
- 无 `P2048` 的持久负缓存：12,167
- 未决：0
- 紧凑 SPARQL 请求：63 批，0 错误
- 完整实体请求：38 批，最终补全无错误

国内命中示例包括东方明珠广播电视塔 468 m、中银大厦 367.4 m、时代金融中心
269 m、北京国际饭店 104.4 m。命中值只作为高度证据；OSM 明确高度/楼层仍然
优先，模型 Z 压缩、层高和打印稳定性仍由确定性规则控制。

## 数据就近提取

- controller：从本机 13 个 PBF 用原生 osmium 提取。
- Windows WSL：从 10 城及檀香山实际渲染使用的 `gdfs_v1` 缓存提取；无需重跑。
- Intel Mac：香港渲染缓存的 buildings 层为空，因此改用本机香港 PBF 与原生
  osmium，得到 568 个对象、546 个 QID。
- cloud-data：旧 Python osmium 在 1.8 GB 节点处理米兰时被 OOM 终止；已停止
  进程并删除临时文件。该节点不再承担此类计算。
- cloud-api：服务始终 active，未重启；在确认无生成任务后曾验证低优先级筛选，
  随后因已有更准确的 Windows GDF 缓存而停止，并清理临时文件。

## 持久化文件

以下运行数据默认被 Git 忽略，保留在 `data/height_cache/`：

- `building_heights.sqlite3`：正/负结果、原始响应和请求审计；
- `showcase_landmarks_enriched.json`：逐城市、逐建筑的合并证据；
- `showcase_landmark_heights.csv`：便于人工筛选的扁平表；
- `showcase_landmarks_*`：各缓存节点的原始提取清单。

验收命令：

```bash
python tools/building_height_cache.py status
python tools/prefetch_landmark_heights.py \
  data/height_cache/showcase_landmarks_controller.json \
  data/height_cache/showcase_landmarks_windows_gdf.json \
  data/height_cache/showcase_landmarks_intel_pbf.json \
  data/height_cache/showcase_landmarks_honolulu_gdf.json \
  --no-fetch
```

## 已知限制

- “OSM 建筑带 Wikidata”是可靠身份门槛，但不等于每个对象都是视觉主地标。
- `P2048` 可能描述塔尖总高、建筑群或历史高度，使用前仍需异常值与打印高度压缩。
- 没有 `P2048` 的结果只表示本次查询时缺少该属性，不代表其他官方来源没有高度。
- 当前 SQLite 是 controller 的主副本；多节点并发写入和增量合并仍未实现。
