"""Minimal active landscape policy; terrain and source water keep their owners."""

def is_active_landscape(policy):
    return (policy.get('activation') == 'active'
            and policy.get('landscape_strategy', {}).get('enabled', False))


def suppress_urban_fill(layers):
    """Remove synthetic city mass, retaining source landmarks and natural layers."""
    removed = len(layers.block_base) + len(layers.BO)
    layers.block_base = []
    layers.block_base_classes = []
    layers.BO = []
    layers.BO_heights = []
    return {'status': 'active', 'removed_urban_components': removed,
            'geometry_policy': 'terrain_water_with_source_landmarks'}
