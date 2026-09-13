"""Separate visual corridor width from physical feature acceptance.

No geometric floor is a substitute for slicing a particular artifact. In
particular an empty groove is not a positive extrusion of the same width.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RoadSurfaceWidths:
    surface_road_gap_mm: float
    final_block_base_gap_mm: float
    min_surface_height_mm: float


def resolve_road_width_contract(printer_profile, style):
    if style not in {'printer-default', 'negative-space-v1', 'negative-space-fine-v1'}:
        raise ValueError('unknown surface road style')
    negative = style in {'negative-space-v1', 'negative-space-fine-v1'}
    fine = style == 'negative-space-fine-v1'
    widths = RoadSurfaceWidths(
        (.14 if fine else .28) if negative else printer_profile.surface_road_gap_mm,
        (.21 if fine else .42) if negative else printer_profile.final_block_base_gap_mm,
        printer_profile.min_surface_height_mm,
    )
    contract = {
        'version': 'road-width-semantics-v1',
        'style': style,
        'units': 'model_mm_full_width',
        'printer_profile_id': printer_profile.profile_id,
        'resolved_visual_widths': {
            'local': widths.surface_road_gap_mm,
            'major': widths.final_block_base_gap_mm,
        },
        'role_mapping': {
            'local': 'negative_gap' if negative else 'positive_strip',
            'major': 'negative_gap' if negative else 'positive_strip',
            'bridge': 'positive_strip',
            'material_mode': 'must_be_declared_at_export_and_slicing',
        },
        'negative_gap': {
            'representation': 'absence_of_city_material_above_substrate',
            'minimum_is_nozzle_width': False,
            'conservative_profile_gap_mm': printer_profile.min_gap_mm,
            'status': 'requires_layerwise_clearance_evidence',
            'checks': ['actual_extrusion_envelope', 'gap_continuity',
                       'relief_depth', 'substrate_support', 'physical_sample'],
        },
        'positive_strip': {
            'representation': 'deposited_road_or_bridge_material',
            'declared_extrusion_width_mm': printer_profile.extrusion_width_mm,
            'status': 'requires_extrusion_survival_and_support_evidence',
            'checks': ['continuous_extrusion', 'minimum_cross_section',
                       'support_or_validated_bridge_span'],
        },
        'multi_material': {
            'representation': 'adjacent_distinct_material_regions',
            'conservative_colored_strip_mm': printer_profile.min_colored_strip_mm,
            'status': 'requires_material_assignment_and_slicing_evidence',
            'checks': ['object_to_filament_mapping', 'per_material_survival',
                       'boundary_intrusion'],
        },
        'physical_acceptance': 'pending',
        '说明': '视觉全缝宽与打印门槛分开；切出缝隙不等于能挤出同宽色带，多材料另验。',
    }
    return widths, contract
