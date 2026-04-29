#!/usr/bin/env python3
"""Root entrypoint wrapper for extract+convert+report pipeline."""

from pathlib import Path
import runpy
import sys

_SCRIPT = Path(__file__).resolve().parent / "src" / "extract_report.py"

if not _SCRIPT.exists():
    raise FileNotFoundError(f"Missing source entry: {_SCRIPT}")

sys.path.insert(0, str(_SCRIPT.parent))
runpy.run_path(str(_SCRIPT), run_name="__main__")
