"""Follow existing source road vertices to dry bridge abutments in S6.

No straight-line endpoint extension or nearest-road snapping. A path may only
follow connected, above-ground highway source segments. The bounded search is
an approach recovery, not a new city-wide road selection.
"""
import hashlib
import heapq
import numpy as np
from shapely.geometry import Point, LineString
from shapely.ops import unary_union
from shapely.strtree import STRtree
from aesthetic.bridge_sources import ROAD_CLASSES, _tag, _positive

MAX_APPROACH_M = 500.


def recover_bridge_approaches(bridges, roads, water, scale, gap_mm):
    rows = (r._asdict() for r in roads.itertuples(index=False)) if hasattr(roads, 'itertuples') else iter(roads)
    source = []
    for row in rows:
        g = row.get('geometry')
        if g is None or g.is_empty or _tag(row.get('highway')) not in ROAD_CLASSES or _positive(row.get('tunnel')):
            continue
        try:
            if float(row.get('layer') or 0) < 0: continue
        except (TypeError, ValueError): pass
        source.extend(p for p in getattr(g, 'geoms', [g]) if p.geom_type == 'LineString')
    tree = STRtree(source)
    radius = gap_mm / scale / 2
    # Keep an actual dry landing inside the source corridor after water cuts.
    clearance = radius * 1.25
    wet_guard = water.buffer(clearance)
    from shapely import prepare
    prepare(wet_guard)
    recovered, evidence, seen = [], [], set()
    for bridge in bridges:
        if bridge.intersection(water).length <= 1e-7: continue
        for endpoint in (bridge.coords[0], bridge.coords[-1]):
            start = tuple(endpoint[:2])
            if not wet_guard.intersects(Point(start)): continue
            # Restrict to source segments reachable within the declared budget.
            hits = tree.query(Point(start).buffer(MAX_APPROACH_M), predicate='intersects')
            graph = {}
            for index in hits:
                g = source[int(index)]
                if g.equals(bridge): continue  # do not turn back along this span
                coords = list(g.coords)
                for a, b in zip(coords, coords[1:]):
                    a, b = tuple(a[:2]), tuple(b[:2])
                    length = float(np.linalg.norm(np.array(a)-b))
                    if length <= 0: continue
                    graph.setdefault(a, []).append((b, length))
                    graph.setdefault(b, []).append((a, length))
            queue = [(0., start, (start,))]; distances = {start: 0.}; chosen = None
            while queue:
                cost, node, path = heapq.heappop(queue)
                if cost != distances.get(node): continue
                if chosen is not None and cost >= chosen[0]: continue
                for other, length in graph.get(node, ()):
                    if other in path: continue
                    segment = LineString([node, other])
                    # First dry sample along the real segment, never beyond it.
                    n = max(1, int(np.ceil(length / max(radius/4, .5))))
                    for t in np.linspace(0., 1., n+1)[1:]:
                        distance = length*t
                        if cost+distance > MAX_APPROACH_M: break
                        point = segment.interpolate(distance)
                        if not wet_guard.intersects(point):
                            candidate = (cost+distance, (*path, tuple(point.coords[0])))
                            if chosen is None or candidate[0] < chosen[0]: chosen = candidate
                            break
                    new_cost = cost+length
                    if new_cost <= MAX_APPROACH_M and new_cost < distances.get(other, float('inf')) and (chosen is None or new_cost < chosen[0]):
                        distances[other] = new_cost
                        heapq.heappush(queue, (new_cost, other, (*path, other)))
            record = dict(source_sha256=hashlib.sha256(bridge.wkb).hexdigest(), endpoint_m=list(start),
                          search_limit_m=MAX_APPROACH_M, invented_connectors=0)
            if chosen:
                line = LineString(chosen[1]); key = line.normalize().wkb
                if key not in seen: recovered.append(line); seen.add(key)
                record.update(status='source_path_recovered', length_m=line.length,
                              path_sha256=hashlib.sha256(line.wkb).hexdigest(), bank_m=list(line.coords[-1]))
            else:
                record.update(status='no_connected_dry_source_path')
            evidence.append(record)
    return recovered, dict(policy='connected-source-bank-approaches-v1', recovered_parts=len(recovered),
                           max_search_m=MAX_APPROACH_M, source_paths=evidence, invented_connectors=0)
