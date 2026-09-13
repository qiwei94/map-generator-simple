"""Opt-in source-identity recovery for PNG experiments and shared S6 plans.

No guessed endpoint bridges: additions are whole, connected source identities
with meaningful existing selection support. S6 may opt in for cross-format
validation; physical acceptance is not implied by recovery.
"""
from collections import defaultdict

from shapely.geometry import GeometryCollection
from shapely.ops import unary_union
from shapely.strtree import STRtree


SURFACE_CLASSES = frozenset(('motorway','trunk','primary','secondary','tertiary',
    'residential','unclassified','living_street','pedestrian',
    'motorway_link','trunk_link','primary_link','secondary_link','tertiary_link'))


def _value(v):
    return '' if v is None or str(v).lower() in ('nan','none','') else str(v).strip()


def is_covered(row):
    flags = [_value(row.get(k)).lower() for k in ('tunnel','covered')]
    if any(x not in ('','no','false','0') for x in flags):
        return True
    try:
        return float(row.get('layer') or 0) < 0
    except (ValueError,TypeError):
        return False


def recover_identities(rows, approved, *, support_fraction=.15,
                       support_length_m=30., tolerance_m=.05, progress=None):
    """Return source-only surface additions and explicit audit, no source IO.

    rows are mappings with geometry and source attributes, already ROI-filtered.
    Anonymous source ways remain independent: never group all unnamed streets.
    Exact geometric contacts define connected components; no distance snapping.
    """
    if not (0 < support_fraction <= 1) or support_length_m < 0 or tolerance_m <= 0:
        raise ValueError('invalid identity support policy')
    # Query nearby approved source segments before buffering/intersection.
    # Buffering a whole-city noded network is extremely expensive and then
    # repeating overlays against it for every identity scales poorly.
    approved_parts=list(getattr(approved,'geoms',[approved]))
    approved_tree=STRtree(approved_parts)
    groups=defaultdict(list); skipped=0
    for index,row in enumerate(rows):
        geom=row['geometry']
        if geom is None or geom.is_empty or geom.geom_type not in ('LineString','MultiLineString'):
            continue
        if _value(row.get('highway')) not in SURFACE_CLASSES or is_covered(row):
            skipped+=1; continue
        name=_value(row.get('name')) or _value(row.get('ref'))
        key=(name or f'__anonymous_source_{index}',_value(row.get('layer')) or '0')
        groups[key].append(geom)
    accepted=[]; added_parts=[]; audit=[]
    for group_index,((identity,layer),geoms) in enumerate(groups.items()):
        if progress and group_index % 250 == 0:
            progress(f'源道路身份 {group_index}/{len(groups)}')
        tree=STRtree(geoms); parent=list(range(len(geoms)))
        def root(i):
            while parent[i]!=i:
                parent[i]=parent[parent[i]]; i=parent[i]
            return i
        for i,g in enumerate(geoms):
            for j in tree.query(g,predicate='intersects'):
                parent[root(int(j))]=root(i)
        components=defaultdict(list)
        for i,g in enumerate(geoms):components[root(i)].append(g)
        for parts in components.values():
            route=unary_union(parts); length=route.length
            neighbors=approved_tree.query(route,predicate='dwithin',distance=tolerance_m)
            if not len(neighbors):
                continue
            support=unary_union([approved_parts[int(i)] for i in neighbors]).buffer(tolerance_m)
            matched=route.intersection(support).length
            if length<=0 or matched < support_length_m or matched/length < support_fraction:
                continue
            accepted.append(route)
            added_parts.append(route.difference(support))
            audit.append({'identity':identity,'layer':layer,'source_features':len(parts),
                'route_length_m':length,'selected_support_m':matched,'support_fraction':matched/length})
    recovery=unary_union(accepted) if accepted else GeometryCollection()
    added=unary_union(added_parts) if added_parts else GeometryCollection()
    return recovery, {'policy':'source-connected-identity-recovery-v1','opt_in_only':True,
        'support_fraction_threshold':support_fraction,'support_length_threshold_m':support_length_m,
        'matching_tolerance_m':tolerance_m,'skipped_non_surface_features':skipped,
        'matching_engine':'STRtree dwithin; same tolerance/support thresholds',
        'identities':audit,'recovered_route_length_m':recovery.length,
        'added_beyond_existing_tolerance_m':added.length,'invented_connectors':0}
