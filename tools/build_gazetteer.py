# -*- coding: utf-8 -*-
"""从本地 PBF 提取命名地标 POI，构建离线地名表（gazetteer）。

实现：pyosmium 两遍扫描（不依赖 osmium CLI 的 tags-filter 引用保留行为，
tools/ 下的 pyosmium wrapper 不会带上 way 的引用节点，面要素会丢）：
    Pass 1: 收集匹配的命名 node POI + 匹配的命名 way 的节点引用
    Pass 2: 解析引用节点坐标 → way 中心取节点均值

输出条目: {"name", "lat", "lon", "prio"}，prio 越小越优先
（tourism=1 > historic=2 > place_of_worship=3 > man_made=4 > natural=5 > leisure=6）。

用法:
    python tools/build_gazetteer.py              # 处理 pbf_cache 下全部 PBF
    python tools/build_gazetteer.py zhejiang     # 只处理文件名含关键字的
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "gazetteer"

# 地标类标签（克制取舍：只要"值得被刻在模型上"的类别）
MATCH_VALUES = {
    "tourism": {"attraction", "viewpoint", "museum", "zoo", "theme_park",
                "artwork", "monument"},
    "historic": None,                      # None = 任意值
    "leisure": {"park", "garden", "nature_reserve"},
    "natural": {"peak", "beach", "bay", "waterfall"},
    "amenity": {"place_of_worship"},
    "man_made": {"tower", "lighthouse"},
}

# 命名优先级：类别越"地标"，越该成为停留点的名字
PRIO = [
    ("tourism", 1),
    ("historic", 2),
    ("amenity", 3),
    ("man_made", 4),
    ("natural", 5),
    ("leisure", 6),
]


def poi_prio(props: dict) -> int:
    for tag, prio in PRIO:
        if props.get(tag):
            return prio
    return 9


def tags_match(tags: dict) -> bool:
    for key, allowed in MATCH_VALUES.items():
        v = tags.get(key)
        if v and (allowed is None or v in allowed):
            return True
    return False


def reduce_features(features) -> list:
    """GeoJSON 风格 features → 去重后的 gazetteer 条目列表。

    （保留该入口供测试与 geojson 数据源复用。）
    去重规则：同名条目保留优先级最高（prio 最小）的一条。
    """
    best = {}
    for f in features:
        props = f.get("properties") or {}
        name = (props.get("name") or "").strip()
        if not name or len(name) > 30:
            continue
        ll = _feature_centroid(f.get("geometry") or {})
        if ll is None:
            continue
        _keep_best(best, name, ll[0], ll[1], poi_prio(props))
    return sorted(best.values(), key=lambda p: (p["prio"], p["name"]))


def _feature_centroid(geom: dict):
    if geom.get("type") == "Point":
        lon, lat = geom["coordinates"][:2]
        return lat, lon
    try:
        from shapely.geometry import shape
        c = shape(geom).centroid
        return c.y, c.x
    except Exception:
        return None


def _keep_best(best: dict, name, lat, lon, prio):
    cur = best.get(name)
    if cur is None or prio < cur["prio"]:
        best[name] = {"name": name, "lat": round(lat, 5),
                      "lon": round(lon, 5), "prio": prio}


def scan_pbf(pbf: Path) -> list:
    """pyosmium 两遍扫描 → gazetteer 条目。"""
    import osmium

    best: dict = {}
    pending_ways = []          # (name, prio, [node_refs])
    needed_nodes: set = set()

    class Pass1(osmium.SimpleHandler):
        def node(self, n):
            tags = dict((t.k, t.v) for t in n.tags)
            name = (tags.get("name") or "").strip()
            if not name or len(name) > 30 or not tags_match(tags):
                return
            _keep_best(best, name, n.location.lat, n.location.lon,
                       poi_prio(tags))

        def way(self, w):
            tags = dict((t.k, t.v) for t in w.tags)
            name = (tags.get("name") or "").strip()
            if not name or len(name) > 30 or not tags_match(tags):
                return
            refs = [nd.ref for nd in w.nodes]
            if refs:
                pending_ways.append((name, poi_prio(tags), refs))
                needed_nodes.update(refs)

    print("  pass 1: scan nodes/ways ...")
    Pass1().apply_file(str(pbf))
    print(f"  pass 1: {len(best)} node POIs, {len(pending_ways)} named ways")

    if pending_ways:
        locs: dict = {}

        class Pass2(osmium.SimpleHandler):
            def node(self, n):
                if n.id in needed_nodes:
                    locs[n.id] = (n.location.lat, n.location.lon)

        print("  pass 2: resolve way node locations ...")
        Pass2().apply_file(str(pbf))
        for name, prio, refs in pending_ways:
            pts = [locs[r] for r in refs if r in locs]
            if not pts:
                continue
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            _keep_best(best, name, lat, lon, prio)

    return sorted(best.values(), key=lambda p: (p["prio"], p["name"]))


def build_one(pbf: Path) -> Path:
    stem = pbf.name.replace(".osm.pbf", "").replace(".pbf", "")
    out_path = OUT_DIR / f"{stem}.json"
    print(f"[{stem}]")
    entries = scan_pbf(pbf)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, ensure_ascii=False),
                        encoding="utf-8")
    print(f"[{stem}] {len(entries)} named POIs -> {out_path}")
    return out_path


def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    pbfs = [p for p in sorted((ROOT / "pbf_cache").glob("*-latest.osm.pbf"))
            if keyword in p.name]
    if not pbfs:
        print(f"no PBF matched '{keyword}' in pbf_cache/")
        sys.exit(1)
    for pbf in pbfs:
        build_one(pbf)


if __name__ == "__main__":
    main()
