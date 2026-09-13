import pytest
from generate_model import canonical_arguments
from generate_city_legacy import parse_args


ARGS = ['--bbox', '48.8,2.2,48.9,2.4', '--pbf', 'paris.pbf', '--city', 'paris']


def test_canonical_entry_resolves_required_profile_and_preserves_legacy():
    args = parse_args(canonical_arguments(ARGS))
    assert args.merge_layers and args.auto_params
    assert args.scene_policy_mode == 'active'
    assert args.pipeline_profile == 'canonical-v1'
    assert parse_args(ARGS).pipeline_profile == 'legacy'
    assert not parse_args(ARGS).merge_layers


def test_canonical_profile_cannot_silently_downgrade():
    with pytest.raises(ValueError):
        canonical_arguments([*ARGS, '--pipeline-profile=legacy'])
    with pytest.raises(SystemExit):
        parse_args(canonical_arguments([*ARGS, '--no-block-base']))
