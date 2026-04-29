#!/usr/bin/env python3
"""Extract a GRP, decode meshes, and write a single flat OBJ.

Output layout:
  <root>/test_example/output/<stem>.obj

The dumpGrp temp extraction is performed in a TemporaryDirectory and
discarded after parsing. Raw class-id files (.B4B7D9C4, .03FB59C4, ...),
companion `.land/` directories, and texture archives are not retained.

Textures are not extracted here. Use `extract_all_textures.py` to populate
the project-root `<root>/texture/` pool once.
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

from exporter import OBJExporter
from grp_converter import GRPResourceParser
from paths import resolve_dumpgrp_from_config, resolve_oodle_from_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("grp_file", help="Input .grp file")
    ap.add_argument("output_dir", nargs="?", default="", help="OBJ output dir (default: <repo>/test_example/output)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--split-variants", action="store_true")
    ap.add_argument(
        "--compare-file-specific",
        action="store_true",
        help="Enable deprecated filename-gated decoders for parity comparison.",
    )
    args = ap.parse_args()

    grp_path = Path(args.grp_file).resolve()
    if not grp_path.exists():
        raise FileNotFoundError(f"GRP file not found: {grp_path}")

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    elif grp_path.parent.name.lower() == "grp":
        output_dir = (grp_path.parent.parent / "output").resolve()
    else:
        output_dir = grp_path.parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    dumpgrp = resolve_dumpgrp_from_config()
    if not dumpgrp:
        raise RuntimeError("dumpGrp path missing in config.json (key: dumpGrp)")
    dumpgrp_path = Path(dumpgrp)
    if not dumpgrp_path.exists():
        raise FileNotFoundError(f"dumpGrp executable not found: {dumpgrp_path}")

    oodle_dll_path = resolve_oodle_from_config()

    with tempfile.TemporaryDirectory(prefix=f"grp_{grp_path.stem}_") as tmp:
        extract_dir = Path(tmp)
        dumpgrp_cmd = [str(dumpgrp_path), str(grp_path), f"-exp:{extract_dir}"]
        subprocess.run(
            dumpgrp_cmd,
            check=True,
            stdout=subprocess.DEVNULL if not args.verbose else None,
            stderr=subprocess.DEVNULL if not args.verbose else None,
        )

        parser = GRPResourceParser(
            verbose=args.verbose,
            oodle_dll=oodle_dll_path,
            comparison_mode=args.compare_file_specific,
        )
        meshes = parser.parse_directory(extract_dir)

    if not meshes:
        print(f"[{grp_path.stem}] no meshes decoded.")
        for r in parser.rejections[:20]:
            print(f"  - {r}")
        raise SystemExit(1)

    if args.split_variants:
        target_dir = output_dir / grp_path.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        OBJExporter.export_split_variants(meshes, target_dir)
        print(f"[{grp_path.stem}] {len(meshes)} variants -> {target_dir}")
    else:
        OBJExporter.export(meshes, output_dir, grp_path.stem)
        print(f"[{grp_path.stem}] {len(meshes)} meshes -> {output_dir / (grp_path.stem + '.obj')}")


if __name__ == "__main__":
    main()
