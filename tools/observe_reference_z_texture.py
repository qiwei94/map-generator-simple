"""Read-only Z morphology from transformed reference meshes; not generation.

White relief is not necessarily a building. Component Z span is NOT local
height above ground. Roof variation and face slopes are measured separately.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.render_reference_actual_mesh import meshes_from_file
from tools.analyze_reference_3mf_shapes import _component_labels


def quantiles(a):
    a = np.asarray(a)
    a = a[np.isfinite(a)]
    return dict(zip(('p10', 'p50', 'p90'), map(float, np.percentile(a, [10, 50, 90])))) if len(a) else None


def observe(path):
    records = []
    for name, vertices, faces, role in meshes_from_file(path, reference=True):
        if role != 'urban_relief':
            continue
        labels = _component_labels(len(vertices), faces)
        n = int(labels.max()) + 1
        low, high = np.full((n, 3), np.inf), np.full((n, 3), -np.inf)
        for axis in range(3):
            np.minimum.at(low[:, axis], labels, vertices[:, axis])
            np.maximum.at(high[:, axis], labels, vertices[:, axis])
        span = high - low
        pts = vertices[faces]
        normal = np.cross(pts[:, 1] - pts[:, 0], pts[:, 2] - pts[:, 0])
        upward = normal[:, 2] > 1e-10
        face_owner = labels[faces[:, 0]]
        area = np.bincount(face_owner[upward], weights=normal[upward, 2] / 2, minlength=n)
        short, long = span[:, :2].min(1), span[:, :2].max(1)
        eligible = ((short >= .21) & (short <= 16) & (long <= 24)
                    & (long / np.maximum(short, 1e-9) <= 4)
                    & (span[:, 2] >= .01) & (area >= .04))
        top_low, top_high = np.full(n, np.inf), np.full(n, -np.inf)
        np.minimum.at(top_low, face_owner[upward], pts[upward, :, 2].min(1))
        np.maximum.at(top_high, face_owner[upward], pts[upward, :, 2].max(1))
        selected = upward & eligible[face_owner]
        # An almost vertical wall can have positive projected area due to
        # float roundoff. Raw vertex extrema then falsely report full wall
        # height as roof roughness. Use the central 90% of projected area.
        ids = np.flatnonzero(selected)
        ids = ids[np.argsort(face_owner[ids], kind='stable')]
        groups = np.split(ids, np.flatnonzero(np.diff(face_owner[ids])) + 1) if len(ids) else []
        central_ranges = []
        for group in groups:
            z = pts[group, :, 2].mean(1)
            w = normal[group, 2] / 2
            order = np.argsort(z)
            cumulative = np.cumsum(w[order])
            at = np.searchsorted(cumulative, cumulative[-1] * np.array([.05, .95]))
            central_ranges.append(float(z[order[at[1]]] - z[order[at[0]]]))
        slope = np.degrees(np.arctan2(np.linalg.norm(normal[selected, :2], axis=1), normal[selected, 2]))
        weights = normal[selected, 2] / 2
        records.append(dict(object=name, compact_white_components=int(eligible.sum()),
            component_z_span_mm=quantiles(span[eligible, 2]),
            within_component_upward_surface_z_range_mm=quantiles((top_high-top_low)[eligible]),
            top_area_under_one_degree_fraction=float(weights[slope < 1].sum()/weights.sum()) if weights.sum() else None,
            top_area_under_five_degrees_fraction=float(weights[slope < 5].sum()/weights.sum()) if weights.sum() else None,
            projected_area_weighted_upward_z_p95_p05_mm=quantiles(central_ranges),
            near_level_central_area_component_fraction=float(np.mean(np.asarray(central_ranges) <= .01)) if central_ranges else None))
    return dict(source=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        units='transformed assembly millimetres', observations=records,
        purpose='diagnostic', verdict='human_review',
        limits=['White material does not establish building/park semantics.',
                'Component Z span includes ground variation and is not building height.',
                'Upward triangles can include overlapping or stepped surfaces, not only roofs.',
                'Morphology cannot prove whether DEM or procedural noise produced it.',
                'Existing compact-relief filter reused: short axis .21–16 mm, long axis <=24 mm, aspect <=4, Z span >=.01 mm, top area >=.04 mm2.'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('inputs', type=Path, nargs='+')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    reports = []
    for path in a.inputs:
        row = observe(path)
        reports.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
