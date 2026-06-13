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
        parts = arg.split('/')
        if len(parts) != 2:
            continue
        obj_type, tag_spec = parts
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


def _way_to_coords(w, node_locs=None):
    """Extract coordinates from a way, falling back to node_locs index."""
    coords = []
    for nd in w.nodes:
        try:
            lon, lat = nd.location.lon, nd.location.lat
            if lon != 0.0 or lat != 0.0:
                coords.append((lon, lat))
        except Exception:
            if node_locs and nd.ref in node_locs:
                coords.append(node_locs[nd.ref])
    return coords


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
    handler.apply_file(args.input)
    writer.close()
    print(f'  [pyosmium tags-filter] wrote {handler.count} elements', file=sys.stderr)


def cmd_export(args):
    print(f'  [pyosmium export] input={args.input}', file=sys.stderr)
    factory = osmium.geom.GeoJSONFactory()
    features = []

    # Try to find area PBF for node location resolution
    area_pbf = _find_area_pbf(args.input)
    node_locs = None
    if area_pbf:
        print(f'  [pyosmium export] loading node locations from area PBF...', file=sys.stderr)
        node_locs = _build_node_index(area_pbf)
        print(f'  [pyosmium export] indexed {len(node_locs)} node locations', file=sys.stderr)

    AREA_KEYS = frozenset([
        'building', 'landuse', 'natural', 'leisure',
        'amenity', 'water', 'boundary', 'shop', 'tourism',
    ])

    class NodeExport(osmium.SimpleHandler):
        def node(self, n):
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
            coords = _way_to_coords(w, node_locs)
            if len(coords) < 2:
                return
            is_area = any(k in AREA_KEYS for k in tags) or tags.get('area') == 'yes'
            if is_area and len(coords) >= 4 and coords[0] == coords[-1]:
                geom = {'type': 'Polygon', 'coordinates': [coords]}
            elif len(coords) >= 2:
                geom = {'type': 'LineString', 'coordinates': coords}
            else:
                return
            features.append({'type': 'Feature', 'geometry': geom,
                             'properties': {**tags, 'osm_type': 'way', 'osm_id': w.id}})

    class RelExport(osmium.SimpleHandler):
        def relation(self, r):
            tags = {t.k: t.v for t in r.tags}
            if not tags:
                return
            # Relation multipolygon assembly needs member ways — try factory first
            try:
                g = json.loads(factory.create_multipolygon(r))
                features.append({'type': 'Feature', 'geometry': g,
                                 'properties': {**tags, 'osm_type': 'relation', 'osm_id': r.id}})
            except Exception:
                pass

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
