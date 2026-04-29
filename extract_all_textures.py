#!/usr/bin/env python3
"""One-shot extractor: decode every *.dxp.bin under test_example/dxp/
into the project-root `texture/` pool.

Run once (or whenever the dxp pool changes). Per-GRP runs no longer
extract textures; they assume `<repo>/texture/` is already populated.

Usage:
  python extract_all_textures.py [--source DIR] [--target DIR] [--verbose]

Defaults:
  --source <repo>/test_example/dxp
  --target <repo>/texture
"""

import argparse
import sys
from pathlib import Path

# Allow running as `python extract_all_textures.py` from the repo root.
_THIS = Path(__file__).resolve()
_SRC = _THIS.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dxp_textures import extract_dxp  # noqa: E402
from grp_converter import OodleDecompressor  # noqa: E402
from paths import resolve_oodle_from_config  # noqa: E402


def main() -> None:
    repo_root = _THIS.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(repo_root / "test_example" / "dxp"))
    ap.add_argument("--target", default=str(repo_root / "texture"))
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--force", action="store_true", help="Re-extract even if target exists")
    args = ap.parse_args()

    source = Path(args.source).resolve()
    target = Path(args.target).resolve()

    if not source.is_dir():
        raise FileNotFoundError(f"Source dir not found: {source}")

    target.mkdir(parents=True, exist_ok=True)

    oodle = OodleDecompressor(resolve_oodle_from_config(), args.verbose)

    dxp_files = sorted(source.glob("*.dxp.bin"))
    if not dxp_files:
        print(f"No *.dxp.bin found under {source}")
        return

    total_written = 0
    failed: list[tuple[str, str]] = []
    for i, dxp in enumerate(dxp_files, start=1):
        marker = target / f".{dxp.stem}.done"
        if marker.exists() and not args.force:
            if args.verbose:
                print(f"[{i}/{len(dxp_files)}] skip {dxp.name} (cached)")
            continue
        try:
            written = extract_dxp(
                dxp,
                output_dir=target,
                oodle_decompressor=oodle,
                verbose=args.verbose,
            )
            total_written += len(written)
            marker.write_text("ok", encoding="utf-8")
            if not args.verbose:
                print(f"[{i}/{len(dxp_files)}] {dxp.name}: +{len(written)}")
        except Exception as exc:
            failed.append((dxp.name, str(exc)))
            print(f"[{i}/{len(dxp_files)}] FAILED {dxp.name}: {exc}")

    print(f"\nDone. Wrote {total_written} new texture(s) to {target}")
    print(f"Total files in pool: {sum(1 for _ in target.iterdir() if _.is_file() and not _.name.startswith('.'))}")
    if failed:
        print(f"Failures: {len(failed)}")
        for name, err in failed[:10]:
            print(f"  - {name}: {err}")


if __name__ == "__main__":
    main()
