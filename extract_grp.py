#!/usr/bin/env python3
"""Root entrypoint wrapper for extract+convert+report pipeline."""

from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

from extract_report import main


if __name__ == "__main__":
    main()
