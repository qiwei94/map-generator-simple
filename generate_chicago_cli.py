#!/usr/bin/env python3
"""Shortcut: generate_chicago_cli.py → generate_city.py --preset chicago"""
import subprocess, sys, os
sys.exit(subprocess.call(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_city.py"),
     "--preset", "chicago"] + sys.argv[1:],
))
