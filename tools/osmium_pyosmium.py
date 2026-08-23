#!/usr/bin/env python3
"""osmium CLI replacement using pyosmium 4.x

Works around GeoJSONFactory pybind11 type-casting bug by manually
extracting node coordinates for way/relation geometry construction.
Uses the companion *_area.pbf (from the extract step) to resolve
node locations when the filtered PBF is missing them.
"""
import argparse, json, os, sys
import osmium
import osmium.io
import osmium.geom


# ── helpers ──────────────────────────────────────────────────────────

def parse_bbox(s):
    parts = [float(x) for x in s.split(',')]
    if len(parts) != 4:
        raise ValueError('bbox must be west,south,east,north')
    return parts


def in_bbox(lon, lat, west, south, east, north):
    return west <= lon <= east and south <= lat <= north


def parse_tag_filter(raw_args):
    groups = []
    current = []
    for arg in raw_args:
        # 兼容真实 osmium CLI 语法：无类型前缀的表达式（如 'natural=water'、
        # 'waterway'）默认匹配 node/way/relation 三种对象
        if '/' in arg:
            obj_type, tag_spec = arg.split('/', 1)
        else:
            obj_type, tag_spec = 'nwr', arg
        if '=' in tag_spec:
            key, val_str = tag_spec.split('=', 1)
            values = None if val_str == '*' else set(val_str.split(','))
        else:
            key = tag_spec
            values = None
        current.append((obj_type, key, values))
    if current:
        groups.append(current)
    return groups


def match_tags(tags, exprs):
    for group in exprs:
        for obj_type, key, values in group:
            if key == '*':
                if len(tags) > 0:
                    return True
            elif key in tags:
                if values is None or tags[key] in values:
                    return True
    return False


def _find_area_pbf(filtered_pbf):
    """Find the companion *_area.pbf in the same directory."""
    d = os.path.dirname(filtered_pbf)
    if not d or not os.path.isdir(d):
        return None
    for f in os.listdir(d):
        if f.endswith('_area.pbf'):
            return os.path.join(d, f)
    return None


def _build_node_index(pbf_path):
    """Build node_id -> (lon, lat) dict from a PBF file."""
    node_locs = {}

    class NodeIndex(osmium.SimpleHandler):
        def node(self, n):
            try:
                node_locs[n.id] = (n.location.lon, n.location.lat)
            except Exception:
                pass

    ni = NodeIndex()
    ni.apply_file(pbf_path)
    return node_locs


def _build_way_index(pbf_path):
    """Build way_id -> [node_ref,...] dict from a PBF file.

    供 relation multipolygon 组装时解析 member way 的节点序列。
    """
    way_nodes = {}

    class WayIndex(osmium.SimpleHandler):
        def way(self, w):
            way_nodes[w.id] = [nd.ref for nd in w.nodes]

    wi = WayIndex()
    wi.apply_file(pbf_path)
    return way_nodes


def _index_to_disk(idx):
    """尽量把 NodeLocationStore 落盘，供后续步骤复用（失败返回 None）。"""
    try:
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.nli')
        os.close(fd)
        idx.write_to_file(path)
        return path
    except Exception:
        return None


def _load_node_index(path):
    """从磁盘加载 NodeLocationStore；失败返回 None。"""
    try:
        return osmium.index.load_nested_map(path)
    except Exception:
        return None


def _way_to_coords(w, node_locs=None, idx=None):
    """Extract coordinates from a way, falling back to node index/dict."""
    coords = []
    for nd in w.nodes:
        resolved = None
        try:
            lon, lat = nd.location.lon, nd.location.lat
            if lon != 0.0 or lat != 0.0:
                resolved = (lon, lat)
        except Exception:
            pass
        if resolved is None and idx is not None:
            try:
                loc = idx.get(nd.ref)
                if loc.valid():
                    resolved = (loc.lon, loc.lat)
            except Exception:
                pass
        if resolved is None and node_locs is not None:
            resolved = node_locs.get(nd.ref)
        if resolved is not None:
            coords.append(resolved)
    return coords


def _assemble_rings(way_list):
    """把 relation 的 outer member ways 拼接成闭合环。

    way_list: [(way_id, coords)]；返回闭合坐标环列表。
    贪心头尾拼接；接不上则丢弃当前链（部分几何好过全部丢失）。
    """
    pieces = [(wid, list(c)) for wid, c in way_list if len(c) >= 2]
    rings = []
    while pieces:
        chain = pieces.pop(0)[1]
        progressed = True
        while progressed and chain[0] != chain[-1]:
            progressed = False
            for i, (_, pc) in enumerate(pieces):
                if pc[0] == chain[-1]:
                    chain.extend(pc[1:]); pieces.pop(i); progressed = True; break
                if pc[-1] == chain[-1]:
                    chain.extend(pc[-2::-1]); pieces.pop(i); progressed = True; break
                if pc[-1] == chain[0]:
                    chain = pc[:-1] + chain; pieces.pop(i); progressed = True; break
                if pc[0] == chain[0]:
                    chain = pc[::-1] + chain[1:]; pieces.pop(i); progressed = True; break
        if len(chain) >= 4 and chain[0] == chain[-1]:
            rings.append(chain)
    return rings


def _build_multipolygon(r, way_index=None, node_locs=None, idx=None):
    """手工组装 relation multipolygon 的 GeoJSON 几何。

    规避 GeoJSONFactory.create_multipolygon 的 pybind11 类型转换 bug。
    relation 回调只给 member ref id，需通过 way_index 拿到节点序列，
    再用 node_locs/idx 解析坐标。outer 环拼接后，inner 环按包含关系
    分配到宿主 outer。
    """
    from shapely.geometry import Polygon

    def _member_coords(m):
        if way_index is None:
            return []
        node_refs = way_index.get(m.ref)
        if not node_refs:
            return []
        coords = []
        for nref in node_refs:
            pt = None
            if idx is not None:
                try:
                    loc = idx.get(nref)
                    if loc.valid():
                        pt = (loc.lon, loc.lat)
                except Exception:
                    pt = None
            if pt is None and node_locs is not None:
                pt = node_locs.get(nref)
            if pt is not None:
                coords.append(pt)
        return coords

    outer_ways, inner_ways = [], []
    for m in r.members:
        if m.type != 'w':
            continue
        coords = _member_coords(m)
        if len(coords) < 2:
            continue
        if m.role == 'inner':
            inner_ways.append((m.ref, coords))
        else:
            outer_ways.append((m.ref, coords))
    outer_rings = _assemble_rings(outer_ways)
    if not outer_rings:
        return None
    inner_rings = _assemble_rings(inner_ways)
    # shapely 分配 inner 到宿主 outer
    out_polys = []
    for ring in outer_rings:
        out_polys.append({'shell': ring, 'holes': [],
                          'poly': Polygon(ring)})
    for hole in inner_rings:
        hp = Polygon(hole)
        for op in out_polys:
            try:
                if op['poly'].contains(hp.representative_point()):
                    op['holes'].append(hole)
                    break
            except Exception:
                continue
    polys = [[op['shell']] + op['holes'] for op in out_polys]
    if len(polys) == 1:
        return {'type': 'Polygon', 'coordinates': polys[0]}
    return {'type': 'MultiPolygon', 'coordinates': polys}


# ── subcommands ──────────────────────────────────────────────────────

def cmd_extract(args):
    west, south, east, north = parse_bbox(args.bbox)
    print(f'  [pyosmium extract] bbox=({west},{south},{east},{north})', file=sys.stderr)

    class ExtractHandler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.count = 0

        def node(self, n):
            try:
                lon, lat = n.location.lon, n.location.lat
                if in_bbox(lon, lat, west, south, east, north):
                    self.writer.add(n)
                    self.count += 1
            except osmium.InvalidLocationError:
                pass

        def way(self, w):
            try:
                for nd in w.nodes:
                    lon, lat = nd.location.lon, nd.location.lat
                    if in_bbox(lon, lat, west, south, east, north):
                        self.writer.add(w)
                        self.count += 1
                        return
            except osmium.InvalidLocationError:
                pass

        def relation(self, r):
            self.writer.add(r)
            self.count += 1

    header = osmium.io.Reader(args.input).header()
    writer = osmium.SimpleWriter(args.output, header=header, overwrite=True)
    handler = ExtractHandler(writer)
    handler.apply_file(args.input, locations=True)
    writer.close()
    print(f'  [pyosmium extract] wrote {handler.count} elements', file=sys.stderr)


def cmd_tags_filter(args):
    exprs = parse_tag_filter(args.expressions)
    print(f'  [pyosmium tags-filter] input={args.input}', file=sys.stderr)

    class FilterHandler(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.count = 0

        def _check(self, item):
            tags = {t.k: t.v for t in item.tags}
            if match_tags(tags, exprs):
                self.writer.add(item)
                self.count += 1

        def node(self, n):
            self._check(n)

        def way(self, w):
            self._check(w)

        def relation(self, r):
            self._check(r)

    header = osmium.io.Reader(args.input).header()
    writer = osmium.SimpleWriter(args.output, header=header, overwrite=True)
    handler = FilterHandler(writer)
    # 带 way 节点坐标索引过滤：否则输出 PBF 的 way 节点无 location，
    # 后续 export 无法组装几何（水体 relation/way 全部丢失）。
    # 索引同时落盘为同名 .nli 文件，export 可复用作兜底。
    try:
        idx = osmium.index.map_flex('flex_mem')
        locs = osmium.index.NodeLocationsForWays(idx)
        handler.apply_file(args.input, locations=True, idx=locs)
        disk = _index_to_disk(idx)
        if disk:
            try:
                os.replace(disk, args.output + '.nli')
            except Exception:
                pass
    except Exception:
        handler.apply_file(args.input)
    writer.close()
    print(f'  [pyosmium tags-filter] wrote {handler.count} elements', file=sys.stderr)


def cmd_export(args):
    print(f'  [pyosmium export] input={args.input}', file=sys.stderr)
    factory = osmium.geom.GeoJSONFactory()
    features = []
    geometry_types = {
        item.strip().casefold()
        for item in args.geometry_types.split(',')
        if item.strip()
    }

    # Try to find area PBF for node location resolution
    area_pbf = _find_area_pbf(args.input)
    node_locs = None
    way_index = None
    if area_pbf:
        print(f'  [pyosmium export] loading node locations from area PBF...', file=sys.stderr)
        node_locs = _build_node_index(area_pbf)
        print(f'  [pyosmium export] indexed {len(node_locs)} node locations', file=sys.stderr)
        # relation multipolygon 的 member way 常无自身标签、不在过滤后
        # PBF 里，须从 area PBF 建 way_id -> 节点序列 索引来解析几何
        way_index = _build_way_index(area_pbf)
        print(f'  [pyosmium export] indexed {len(way_index)} ways for relations', file=sys.stderr)
    else:
        way_index = _build_way_index(args.input)

    # 兜底：tags-filter 落盘的同名 .nli 节点索引（无 *_area.pbf 时唯一来源）
    nli_idx = _load_node_index(args.input + '.nli')
    if nli_idx is not None:
        print(f'  [pyosmium export] loaded .nli node index fallback', file=sys.stderr)

    AREA_KEYS = frozenset([
        'building', 'landuse', 'natural', 'leisure',
        'amenity', 'water', 'boundary', 'shop', 'tourism',
    ])

    class NodeExport(osmium.SimpleHandler):
        def node(self, n):
            if 'point' not in geometry_types:
                return
            tags = {t.k: t.v for t in n.tags}
            if not tags:
                return
            try:
                g = json.loads(factory.create_point(n.location))
                features.append({'type': 'Feature', 'geometry': g,
                                 'properties': {**tags, 'osm_type': 'node', 'osm_id': n.id}})
            except Exception:
                pass

    class WayExport(osmium.SimpleHandler):
        def way(self, w):
            tags = {t.k: t.v for t in w.tags}
            if not tags:
                return
            coords = _way_to_coords(w, node_locs, idx=nli_idx)
            if len(coords) < 2:
                return
            is_area = any(k in AREA_KEYS for k in tags) or tags.get('area') == 'yes'
            if is_area and len(coords) >= 4 and coords[0] == coords[-1]:
                if 'polygon' not in geometry_types:
                    return
                geom = {'type': 'Polygon', 'coordinates': [coords]}
            elif len(coords) >= 2:
                if 'linestring' not in geometry_types:
                    return
                geom = {'type': 'LineString', 'coordinates': coords}
            else:
                return
            features.append({'type': 'Feature', 'geometry': geom,
                             'properties': {**tags, 'osm_type': 'way', 'osm_id': w.id}})

    class RelExport(osmium.SimpleHandler):
        def relation(self, r):
            if 'polygon' not in geometry_types:
                return
            tags = {t.k: t.v for t in r.tags}
            if not tags:
                return
            # pybind11 bug 使 factory.create_multipolygon 必崩；手工组装。
            # 优先试 factory（万一环境可用），失败则回退手工。
            g = None
            try:
                g = json.loads(factory.create_multipolygon(r))
            except Exception:
                g = _build_multipolygon(
                    r, way_index=way_index, node_locs=node_locs, idx=nli_idx)
            if g is None:
                return
            features.append({'type': 'Feature', 'geometry': g,
                             'properties': {**tags, 'osm_type': 'relation', 'osm_id': r.id}})

    print('  [pyosmium export] processing nodes...', file=sys.stderr)
    h_n = NodeExport()
    h_n.apply_file(args.input)
    print('  [pyosmium export] processing ways...', file=sys.stderr)
    h_w = WayExport()
    h_w.apply_file(args.input, locations=True)
    print('  [pyosmium export] processing relations...', file=sys.stderr)
    h_r = RelExport()
    h_r.apply_file(args.input, locations=True)

    geojson = {'type': 'FeatureCollection', 'features': features}
    with open(args.output, 'w') as f:
        json.dump(geojson, f)
    sz = os.path.getsize(args.output) / 1024
    n_poly = sum(1 for f in features if f['geometry']['type'] == 'Polygon')
    n_line = sum(1 for f in features if f['geometry']['type'] == 'LineString')
    n_point = sum(1 for f in features if f['geometry']['type'] == 'Point')
    print(f'  [pyosmium export] {len(features)} features (Point:{n_point} Line:{n_line} Poly:{n_poly}) ({sz:.1f} KB)', file=sys.stderr)


# ── main ─────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('version', '--version'):
        print('osmium version 1.16.0 (pyosmium)')
        sys.exit(0)

    parser = argparse.ArgumentParser(description='osmium CLI (pyosmium)')
    sub = parser.add_subparsers(dest='command')

    p_ext = sub.add_parser('extract')
    p_ext.add_argument('-b', '--bbox', required=True)
    p_ext.add_argument('-s', '--strategy', default=None)
    p_ext.add_argument('input')
    p_ext.add_argument('-o', '--output', required=True)
    p_ext.add_argument('--overwrite', action='store_true')

    p_tag = sub.add_parser('tags-filter')
    p_tag.add_argument('input')
    p_tag.add_argument('expressions', nargs='+')
    p_tag.add_argument('-o', '--output', required=True)
    p_tag.add_argument('--overwrite', action='store_true')

    p_exp = sub.add_parser('export')
    p_exp.add_argument('input')
    p_exp.add_argument('-o', '--output', required=True)
    p_exp.add_argument('-f', '--format', default='geojson')
    p_exp.add_argument(
        '--geometry-types', default='point,linestring,polygon',
        help='comma-separated point,linestring,polygon families')
    p_exp.add_argument('--overwrite', action='store_true')

    args = parser.parse_args()
    if args.command == 'extract':
        cmd_extract(args)
    elif args.command == 'tags-filter':
        cmd_tags_filter(args)
    elif args.command == 'export':
        cmd_export(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
