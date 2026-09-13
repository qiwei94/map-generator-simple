"""Opt-in planar corner treatment. Never grows a block into a road seam."""
from hashlib import sha256
import math
from shapely.ops import unary_union
from _TEXTURE_STYLE_OF_DEEPSEEK.block_base import _polygon_parts


def soften_block_edges(urban, *, scale, radius_mm=.04, max_area_loss=.02):
    """Bounded bevel opening, with repeatable per-block radius variation.

    Reject candidates that split/disappear, alter hole count or remove too
    much support. Intersect with the original so existing seams cannot close.
    Experimental PNG geometry, not a printer-profile override.
    """
    if not all(math.isfinite(x) for x in (scale, radius_mm, max_area_loss)):
        raise ValueError('finite edge parameters required')
    if scale <= 0 or radius_mm < 0 or not 0 <= max_area_loss < 1:
        raise ValueError('invalid edge parameters')
    pieces = list(_polygon_parts(urban))
    result, changed, rejected = [], 0, 0
    for original in pieces:
        key = sha256(original.normalize().wkb).digest()
        factor = .75 + int.from_bytes(key[:4], 'big') / (2**32-1) * .5
        radius = radius_mm / scale * factor
        if radius == 0:
            result.append(original)
            continue
        candidate = original.buffer(-radius, join_style=2).buffer(radius, join_style=3).intersection(original)
        if (candidate.is_empty or not candidate.is_valid or candidate.geom_type != 'Polygon'
                or len(candidate.interiors) != len(original.interiors)
                or candidate.area < original.area * (1-max_area_loss)):
            result.append(original)
            rejected += 1
        else:
            result.append(candidate)
            changed += int(not candidate.equals(original))
    combined = unary_union(result)
    return combined, {'policy': 'bounded-inward-bevel-v1', 'radius_mm': radius_mm,
        'radius_variation': [.75, 1.25], 'max_area_loss_per_block': max_area_loss,
        'input_blocks': len(pieces), 'output_blocks': len(list(_polygon_parts(combined))),
        'changed_blocks': changed, 'fallback_blocks': rejected,
        'removed_area_m2': urban.area-combined.area,
        'added_area_m2': combined.difference(urban).area,
        'scope': 'planar-only; no print acceptance'}
