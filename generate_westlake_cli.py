#!/usr/bin/env python3
"""Shortcut: generate_westlake_cli.py → generate_city.py --preset westlake"""
import subprocess, sys, os
sys.exit(subprocess.call(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_city.py"),
     "--preset", "westlake"] + sys.argv[1:],
))
