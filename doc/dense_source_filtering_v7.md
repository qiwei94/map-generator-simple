# 高质量城市数据的保真过滤（v7）

**2026-09-03 验收结论：实验未通过，默认关闭，未发布。**
可运行的第一切片是过滤损失观测、比例前置检查与真实回退状态显示，
不是“已经改善芝加哥”的新生成方案。

## 目标

旧版芝加哥 `dense_detail` 的优势来自完整数据、较小取景和低对比细纹理，
不能把其二维细节直接当成可打印几何。但高质量数据也不应为了统一的视觉
粒度目标被继续吞并成粗块。

本次切片只涉及 S5/S6 建筑体量候选与诊断，不改变道路选择、水体、地形、全局 Z、
材料或网格布尔运算。S4 已有的局部数据完整性与连续性判定继续是路由依据，
没有城市名称分支。

## 三条路径

下表是**待验证的实验设计**。正式调用仍使用原有聚合路径；只有离线工具显式
传 `--experimental-complete-source-filter` 才启用新行为。

| 已有测量结果 | S5 过滤与聚合职责 |
| --- | --- |
| `complete_source_union`：完整、连续的城市建筑数据 | 达到打印材料条带下限的组件冻结；仅对不足下限的小块做最多 3 轮局部配对救活 |
| `supported_cluster_infill`：有城市证据但建筑不完整 | 保持原来的源数据支撑聚合，向软视觉粒度目标构造中频体量 |
| `sparse_local_preserve`：稀疏或低可信场景 | 保留局部真实聚落，不推断大面积城市填充 |

完整数据的停止宽度取自 `PrinterProfile` 的 `max(extrusion_width_mm,
min_colored_strip_mm)`，再按本次真实模型比例换算。默认配置为 0.63 mm，
不是新的城市专用常量。道路/水体边界、面积增长、轮廓规整与最终物理内核
检查仍然生效。

## 有界救活

完整数据路径不再在每次成功合并后重建整个空间索引：

1. 已达宽度下限的组件移入保留集合，不再作为合并邻居。
2. 每轮对剩余小块建立一次 STRtree。
3. 按稳定顺序配对；同一个组件一轮最多参与一次。
4. 每次合并仍检查道路/水体裁切、最大轴长、面积增长与轮廓规则。
5. 最多 3 轮；仍无可靠物理内核的碎片过滤，禁止无上限搜索。

这是“保留可打印颗粒 + 有限救活小块”，不是把每栋微建筑拉宽。

## 证据与验证

候选与正式 DesignSpec 的建筑证据增加 `source_fidelity_filter`：过滤路径、
打印下限、轮次上限、已冻结组件数以及救活后物理内核拒绝数。
已有组件数量、覆盖面积、抽象新增面积、轮廓和粒度分布继续输出。
`source_clearance_audit` 则独立记录：原始轮廓分配数、未分配数、分块内原始
支撑面积、退让后支撑面积、退让存活轮廓数、生成结果实际覆盖的源面积。
这两阶段保留率不能与“旧 BO 已聚合/填充面积”混用。

测试命令：

```bash
.venv/bin/python -m pytest -q tests/test_building_mass_strategy.py \
  tests/test_scene_policy.py tests/test_block_grammar.py \
  tests/test_evaluate_building_mass_strategy.py tests/test_scale_aware_topology.py \
  tests/test_measurement_report.py tests/test_pipeline_observation.py \
  tests/test_pipeline_domain.py tests/test_pipeline_contract.py \
  tests/test_generator_pipeline_wiring.py
```

真实芝加哥验证使用已有 25 km GDF/layer/topology 缓存，不下载数据，不运行
地形或完整 3MF。输出目录：
`output/building_quality_multi_city_v1/building_mass_complete_fidelity_v24/chicago_25km/`。

注意：`building_mass_mid_frequency_v23` 的历史 362 秒结果仅剩 59 个组件，
属于过度过滤失败样本，不能作为有效性能基线。正式接受必须比较同范围、
同输入、同样满足要素保留要求的结果；PNG 审美通过也不等于 3MF 可打印验收。

### 真实实验记录（controller Mac，本地缓存）

```bash
.venv/bin/python -u tools/evaluate_building_mass_strategy.py \
  --gdf-cache cache/pipeline/showcase_chicago_25km_aesthetic/gdfs_v1_4bfeedffe027.pkl \
  --layer-cache cache/pipeline/showcase_chicago_25km_aesthetic/preprocess_v5_16e3d0162617.pkl \
  --bbox 41.7650535,-87.8587977,41.9911465,-87.5571755 \
  --output-dir output/building_quality_multi_city_v1/building_mass_complete_fidelity_v24/chicago_25km \
  --tag chicago_25km \
  --topology-cache output/building_quality_multi_city_v1/topology_cache/chicago_25km_v2.pkl \
  --block-source topology --model-span-mm 196 \
  --experimental-complete-source-filter
```

实际运行时该实验尚未加开关、默认开启；上面的最后一个开关是当前代码重现实验
所需。历史 JSON 保持原样，没有把新字段伪写回旧运行。

- 整个建筑 A/B：1046.840 秒（17分27秒）；未运行地形或完整 3MF。
- 候选：943 个组件（809 urban + 134 quiet）；角色过滤前 1183 个聚类输出。
- 候选支撑面积（已退让）32758206.168 ㎡；输出面积9376342.25 ㎡。
- 组合后面积43962927.177 ㎡，小于旧层118032578.409 ㎡，触发 `guarded_fallback`。
- 左/中俯视与高度图实际相同，原评估状态 `human_review_required` 太宽松；
  当前工具改为 `rerun`，图标题明确标注“未采用候选：显示原图回退”。
- 本次实际比例0.0077671466 mm/m，喷嘴换算51.4989639 m；读回确认缓存匹配。
  另一份历史正式报告的29.277 km不能用于指控本次25.234 km诊断存在比例错配。
- 相关10组回归文件：**188 passed in 6.67s**。

### 快速定位源损失

```bash
.venv/bin/python -u tools/audit_building_source_clearance.py \
  --evidence output/building_quality_multi_city_v1/building_mass_complete_fidelity_v24/chicago_25km/chicago_25km_building_mass_evidence.json \
  --output output/building_quality_multi_city_v1/building_mass_complete_fidelity_v24/chicago_25km/source_clearance_audit.json
```

只重放原始建筑分配及退让，复用 S5 的同一裁切函数，验证缓存身份、bbox、比例
与退让值；输出 JSON 和中文 Markdown。不会下载、聚合、生成网格或上线。

同一输入的全量审计已完成（186.731秒）：

| 测量 | 实测 |
| --- | ---: |
| 原始有效建筑轮廓 | 702066 |
| 未分配到分块的轮廓 | 0 |
| 有建筑的分块 | 2632 |
| 每侧退让（模型单位） | 0.46 mm |
| 退让后仍有几何的源轮廓 | 265583 |
| 分块内原始建筑并集面积 | 110679558.423 ㎡ |
| 退让后建筑支撑面积 | 32758206.182 ㎡ |
| 退让面积保留率 | 29.5973% |

**可以确认的原因**：本次不是漏分配建筑，而是分块内退让在聚合前移除了约
70.4%的原始建筑支撑面积。最终聚合/物理内核还有进一步损失；两者不能混为一谈。
这是固定打印缝隙在25 km比例下与街边建筑分布共同产生的冲突，不能仅靠
“完整数据停止聚合”解决，也不能直接缩窄打印缝隙来掩盖。

### 下一步接受条件

1. 分别定位“分块退让损失”和“聚合/物理内核过滤损失”，不能继续只调最终阈值。
2. 打印尺寸、bbox、源缓存一致；不把旧15 km画廊与25 km模型当控制变量实验。
3. 候选实际被采用，水/路/地标未退化，再比较颗粒、覆盖和缝隙。
4. 只有建筑诊断通过后才跑完整3MF与严格验证；当前没有新的打印通过结论。
