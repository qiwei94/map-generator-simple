# Spec：扩展建筑地标识别（外部数据 + 热点区放宽）

## 问题

当前建筑地标 = OSM 标签命中 + size + percentile，杭州 25km 共 **3,477 个**。覆盖了 Tier 1（wikidata/historic/tourism）和 Tier 2（特殊建筑类型 stadium/temple）和 Tier 3（命名+非住宅+大体量）。但仍有遗漏：

- **OSM 标注空白**：杭州河坊街、清河坊、北山街民居建筑群——OSM 没标 historic/tourism，也没 wikidata，但实际是知名打卡地
- **商业热点漏标**：钱江新城商圈 / 武林广场附近的写字楼，OSM 多数 `building=commercial` 没 name，被当普通楼
- **低密度郊区被全覆盖识别成 landmark**：当前 percentile（top 1%）在郊区也命中——不算 hotspot 不该提升

需求：**热点区域 → 更多地标（放宽阈值）；郊区 → 不变**。

## 5 种方案对比

### A. Wikipedia geosearch API（推荐 ★）

```
GET https://zh.wikipedia.org/w/api.php?action=query&list=geosearch
    &gscoord=30.245|120.150&gsradius=10000&gslimit=500
```

**优点**：
- 免费、无 API key、JSON 返回
- 中文 wiki 结果直接可用（"雷峰塔"、"西湖"、"灵隐寺"...）
- 有 page_id + title + distance，**title 就是中文标准名**
- 可同时调英文 wiki 取 `name:en`

**做法**：
1. bbox 中心点 + 半径覆盖 → 1-4 个 query 拿到所有 wiki 文章
2. 对每个 wiki 标题，在 OSM 建筑里 `gdf['name'] == title` 找匹配
3. 命中的建筑 → 标记为 landmark（即使 OSM 标签弱）
4. 没命中的（wiki 有词条但 OSM 没对应建筑）→ 当作"虚拟 landmark"画文字标注

**缺点**：
- 网络依赖（之前 sandbox 有过限制）
- wiki 标题与 OSM `name` 可能不完全匹配（"灵隐寺" vs "灵隐禅寺"）—— 需 fuzzy 匹配

**预期增量**：杭州 **+200~500 个**有名建筑被识别。

### B. Wikidata SPARQL bbox 查询

```sparql
SELECT ?item ?itemLabel ?coord WHERE {
  ?item wdt:P31/wdt:P279* wd:Q41176 .  # building or subclass
  ?item wdt:P625 ?coord .
  FILTER(geo:within(?coord, "POLYGON(...)"^^geo:wktLiteral))
  SERVICE wikibase:label { bd:serviceParam wikibase:language "zh,en". }
}
```

**优点**：
- 结构化（Q-id、类型、坐标精确）
- 可过滤"建筑物"类型
- OSM 已有 `wikidata=*` 字段，直接 join

**缺点**：
- SPARQL 学习曲线
- Wikidata 中文条目数 < Wikipedia
- 网络依赖

**预期增量**：杭州 +100~200 个（多为已被 wikidata=* 标记的，与现 Tier 1 重叠）。

### C. 高德 / 百度地图 POI API

```
高德 POI: https://restapi.amap.com/v3/place/polygon?key=KEY&polygon=...
返回字段：name / type / location / tag / business_area / rating
```

**优点**：
- 中国地理覆盖最全
- 有"星级"/"评论数"作 importance 评分
- 包含 OSM 没的"商场名"、"饭店名"、"企业名"

**缺点**：
- 需要 API key (高德 / 百度都要注册)
- 配额限制（高德免费版 5000 query/天）
- 数据格式与 OSM 不一致，需要 schema 映射

**预期增量**：杭州 +1000+ 个（POI 多但杂）。

### D. 用户自维护 CSV（"白名单"）

格式：

```csv
name,name_en,lat,lon,city
雷峰塔,Leifeng Pagoda,30.2310,120.1494,westlake
河坊街,Hefang Street,30.2418,120.1685,westlake
浙江博物馆,Zhejiang Museum,30.2470,120.1568,westlake
...
```

**优点**：
- 100% 可控，质量保证
- 离线
- 适合**做产品**时 PM/设计师人工 curate

**缺点**：
- 手动维护成本（每城市一份）
- 不可扩展

### E. OSM-内建密度驱动放宽阈值

不引入外部，仅在 OSM 数据层做：

```
1. 算每个 polygonize block 的建筑数 / 建筑总面积 / 道路密度 → 热点 score
2. score 前 X%（10%）的 block 标记为"热点 block"
3. 在热点 block 内，建筑 landmark 阈值降低：
   - Tier 3b 面积阈值 1500 → 1000 m²
   - Tier 4 percentile 1% → 5%
   - 任何 building 类型 + name 都算 landmark（不限 NON_LANDMARK_BUILDING_TYPES）
```

**优点**：
- 完全离线
- 与现有架构一致
- 立刻可做

**缺点**：
- 仍受限 OSM 标签
- 命名建筑较少的区域（如 OSM 标注稀疏的城市）效果有限

**预期增量**：杭州 +500~800 个（现有 size_landmarks 的"次级大建筑" + 热点区命名建筑）。

## 推荐组合：A + E

**第一阶段**（立刻做，离线安全）：方案 E（密度驱动放宽）

- 不依赖外部，立等可见
- 杭州 / 重庆 / Chicago 都受益

**第二阶段**（可选，需网络）：方案 A（Wikipedia geosearch）

- 仅在用户确认网络可用时启用
- 提供 `--with-wiki-landmarks` flag
- 结果缓存到 `tmp/wiki_landmarks_{lat1}_{lon1}_{lat2}_{lon2}.json`，避免重复请求

## 详细方案 E：密度驱动放宽

### 1. 计算热点 block

```python
def compute_hotspot_block_ids(blocks, all_buildings, top_percent=10.0) -> set[int]:
    """按建筑面积总和 / block 面积，取 top X% block 为热点。"""
    btree = STRtree(blocks)
    # 每 block 内建筑总面积
    block_bldg_area = [0.0] * len(blocks)
    for b in all_buildings:
        c = b.centroid
        for ci in btree.query(c):
            if blocks[ci].contains(c):
                block_bldg_area[ci] += b.area
                break
    # density 排序，取前 X%
    densities = [a / max(blocks[i].area, 1.0) for i, a in enumerate(block_bldg_area)]
    threshold = sorted(densities, reverse=True)[int(len(densities) * top_percent / 100)]
    return {i for i, d in enumerate(densities) if d >= threshold}
```

### 2. 修改 `is_tag_landmark` 接受热点 flag

```python
def is_tag_landmark(row, area_m2=None, hotspot=False) -> bool:
    # 现有 Tier 1/2/3 不变
    ...
    # 新增：热点区下调 Tier 3b 阈值
    if pd.notna(name) and (pd.isna(bldg) or bldg == "yes"):
        threshold = 1000.0 if hotspot else 1500.0
        if area_m2 is not None and area_m2 >= threshold:
            return True
    # 热点区允许部分原本被列入 NON_LANDMARK 的类型有 name 也算
    if hotspot and pd.notna(name) and pd.notna(bldg):
        if bldg in {"commercial", "retail", "office", "hotel"}:
            return True
    return False
```

### 3. 在 `tune_buildings_v2.py:run_one` 应用

```python
# 计算热点 block ids（一次性，与 city_blocks 同步缓存）
hotspot_ids = compute_hotspot_block_ids(city_blocks, polys, top_percent=10.0)

# 重新分类 polys（覆盖原 landmark_flags）
for i, p in enumerate(polys):
    centroid = p.centroid
    in_hotspot = False
    for ci in btree.query(centroid):
        if blocks[ci].contains(centroid):
            in_hotspot = ci in hotspot_ids
            break
    if is_tag_landmark(row_i, area_m2=p.area, hotspot=in_hotspot):
        ...
```

### CLI

```python
ap.add_argument("--hotspot-relax", type=float, default=10.0,
                help="热点 block top X%% 内放宽 landmark 阈值 (0=关闭)")
```

### 预期效果

杭州 25km：

| 区 | 现命中 | 加 hotspot 后 |
| --- | --- | --- |
| 西湖 / 灵隐 / 钱江新城 / 武林 | 多 | **更多**（+30~50%）|
| 西溪 / 富阳 / 萧山郊区 | 少 | 基本不变 |

## 详细方案 A：Wikipedia geosearch（可选）

### 1. fetch + cache

```python
def fetch_wiki_landmarks(lat1, lon1, lat2, lon2, cache_dir):
    """对 bbox 内查 Wikipedia (zh + en) 文章，返回 [(title, lat, lon, snippet, source)]."""
    cache = cache_dir / f"wiki_lm_{lat1:.4f}_{lon1:.4f}_{lat2:.4f}_{lon2:.4f}.json"
    if cache.exists():
        return json.load(open(cache))
    # 中心点 + 多个分块查询（10km 半径覆盖 25km 需 4-9 块）
    results = []
    for cy, cx in subdivide_bbox(lat1, lon1, lat2, lon2, tile_km=10):
        url = f"https://zh.wikipedia.org/w/api.php"
        params = dict(action="query", list="geosearch",
                       gscoord=f"{cy}|{cx}", gsradius=10000, gslimit=500,
                       format="json")
        r = requests.get(url, params=params, timeout=10)
        for hit in r.json().get("query", {}).get("geosearch", []):
            results.append((hit["title"], hit["lat"], hit["lon"], None, "wiki_zh"))
    json.dump(results, open(cache, "w"), ensure_ascii=False, indent=2)
    return results
```

### 2. 与 OSM 建筑 fuzzy 匹配

```python
def match_wiki_to_buildings(wiki_landmarks, buildings_gdf, max_dist_m=80):
    """对每个 wiki 文章坐标，找最近 OSM 建筑（< 80m）。命中即标 landmark。"""
    btree = STRtree(buildings_gdf.geometry.tolist())
    matched_ids = set()
    for title, lat, lon, _, _ in wiki_landmarks:
        # 投影到 UTM local
        x, y = project_point(lat, lon)
        pt = Point(x, y)
        for idx in btree.query(pt.buffer(max_dist_m)):
            geom = buildings_gdf.geometry.iloc[idx]
            if geom.distance(pt) < max_dist_m:
                matched_ids.add(idx)
                break
    return matched_ids
```

### CLI

```python
ap.add_argument("--with-wiki-landmarks", action="store_true",
                help="启用 Wikipedia geosearch 补充建筑 landmark（需联网）")
```

### 预期效果

杭州 +200~500 个 wiki 命中的建筑被认 landmark。

## 优先级总览

| # | 方案 | 工作量 | 离线 | 预期增量（杭州） | 推荐度 |
| - | --- | ---- | --- | ---------- | --- |
| E | 热点放宽 | 低（~80 行）| ✅ | +500~800 | ★★★ |
| A | Wiki geosearch | 中（~150 行）| ✗ | +200~500 | ★★ |
| B | Wikidata SPARQL | 中 | ✗ | +100~200 | ★ |
| D | 自维护 CSV | 低 | ✅ | 取决于人工 | ★（产品化时）|
| C | 高德 POI | 高（API key + 配额） | ✗ | +1000+ | 暂不推荐 |

## 不在范围

- ❌ 不改 PNG 标注样式（spec_landmark_annotation.md 已定）
- ❌ 不引入 GIS 数据库
- ❌ 不实现方案 C（API key + 配额复杂度）

## 审批节点

1. **第一阶段**先做方案 E（热点放宽，无外部依赖）？
2. **第二阶段**做方案 A（Wikipedia geosearch）？还是先用 E 看看效果再决定？
3. 方案 D（CSV）作为长期产品级运营手段，**不在本 spec 实现范围**，但保留接口（CLI 可读 CSV 文件）。
