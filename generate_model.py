#!/usr/bin/env python3
"""Canonical generation entry: all new full/review/draft tasks start here.

The implementation remains in generate_city_legacy while effects are extracted;
this entry fixes execution policy and does not implement a parallel pipeline.
Historical scripts are explicit compatibility paths, not canonical aliases.
"""
import sys


def canonical_arguments(argv):
    if any(arg == '--pipeline-profile' or arg.startswith('--pipeline-profile=') for arg in argv):
        raise ValueError('generate_model fixes canonical-v1; use generate_city_legacy.py for compatibility')
    return ['--pipeline-profile', 'canonical-v1', *argv]


def main(argv=None):
    from generate_city_legacy import main as execute
    return execute(canonical_arguments(list(sys.argv[1:] if argv is None else argv)))


if __name__ == '__main__':
    main()
