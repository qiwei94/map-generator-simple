"""Compatibility launcher for the former West Lake CLI filename.

The maintained entry point is now ``generate_city.py``. This module keeps old
commands and imports working while there are local scripts or notes that still
refer to the historical filename.
"""

from __future__ import annotations

import runpy

from generate_city import build_parser


if __name__ == "__main__":
    runpy.run_module("generate_city", run_name="__main__")
