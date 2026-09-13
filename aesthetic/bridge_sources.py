"""Physical bridge provenance, independent of foreground/landmark selection."""
from shapely.ops import unary_union

POLICY_VERSION = "source-tagged-water-bridges-v1"
ROAD_CLASSES = frozenset((
    "motorway", "trunk", "primary", "secondary", "tertiary", "residential",
    "unclassified", "living_street", "service", "road", "pedestrian",
    "footway", "cycleway", "path", "steps", "track", "bridleway",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
))


def _tag(value):
    return str(value).strip().lower() if value is not None else ""


def _positive(value):
    return _tag(value) not in ("", "no", "0", "false", "nan", "none", "<na>", "unknown")


def extract_bridge_sources(roads):
    """Keep explicit physical highway bridges, including unnamed/pedestrian ones.

    Input geometry is already in the pipeline's local metric frame. Counts are
    source features/line parts, NOT distinct named bridges. No guessed joins.
    """
    # Stream records: a full-city road table can contain hundreds of thousands
    # of rows; retaining a second giant dictionary list is unnecessary.
    rows = (row._asdict() for row in roads.itertuples(index=False)) if hasattr(roads, "itertuples") else (roads or [])
    lines, seen = [], set()
    tagged = rejected = 0
    for row in rows:
        if not _positive(row.get("bridge")):
            continue
        tagged += 1
        try:
            below_ground = float(row.get("layer") or 0) < 0
        except (ValueError, TypeError):
            below_ground = False
        # Covered bridges are valid; tunnel=yes is a conflicting source claim.
        if (_tag(row.get("highway")) not in ROAD_CLASSES
                or _positive(row.get("tunnel")) or below_ground):
            rejected += 1
            continue
        geom = row.get("geometry")
        if geom is None or geom.is_empty:
            continue
        for part in getattr(geom, "geoms", [geom]):
            if part.geom_type != "LineString" or part.length <= 0:
                continue
            key = part.normalize().wkb
            if key not in seen:
                seen.add(key)
                lines.append(part)
    return lines, {"policy_version": POLICY_VERSION, "tagged_source_features": tagged,
                   "rejected_nonroad_or_conflicting_features": rejected,
                   "retained_line_parts": len(lines), "invented_connectors": 0,
                   "selection": "explicit_bridge_tag_not_landmark_or_name"}


def water_bridge_lines(layers):
    """Only water-crossing source parts become protected bridge corridors.

    Retain their full source span (including bank ends); do not enable all
    city viaducts as extra foreground roads. Water contact must have length,
    so a single tangent point does not qualify.
    """
    water = unary_union(list(layers.WL) + list(layers.WO))
    return [line for line in getattr(layers, "bridge_lines", ())
            if not line.is_empty and line.intersection(water).length > 1e-7]


def bridge_corridor(lines, scale, gap_mm, clip):
    from shapely.geometry import LineString
    radius = gap_mm / scale / 2
    corridors = []
    for line in lines:
        corridor = line.buffer(radius, cap_style=2, join_style=2)
        # A very short hairpin can make GEOS's flat-capped whole-line buffer
        # exclude part of its OWN centreline. Preserve the original miter
        # shape, but union segment rectangles for that exceptional source.
        # This never invents a connector or extends either source endpoint.
        if line.difference(corridor.buffer(1e-7)).length > 1e-7:
            coords = list(line.coords)
            rectangles = [LineString([a, b]).buffer(radius, cap_style=2)
                          for a, b in zip(coords, coords[1:]) if a != b]
            corridor = unary_union([corridor] + rectangles)
        corridors.append(corridor)
    return unary_union(corridors).intersection(clip)
