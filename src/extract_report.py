#!/usr/bin/env python3
"""Extract a GRP, decode meshes, and export OBJ output.

Output layout:
  <grp_dir>/<stem>/  — OBJ files exported next to the input GRP file

The dumpGrp temp extraction is performed in a TemporaryDirectory and
discarded after parsing. Raw class-id files (.B4B7D9C4, .03FB59C4, ...),
companion `.land/` directories, and texture archives are not retained.
"""

import argparse
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Set

from exporter import OBJExporter
from grp_converter import GRPResourceParser
from paths import project_root, resolve_dumpgrp_from_config, resolve_oodle_from_config


TOOLS_PREBUILD_URL = "https://github.com/GaijinEntertainment/DagorEngine/releases"


def _print_missing_tools_hint(dumpgrp_path: Optional[Path] = None) -> None:
    root = project_root()
    expected_dumpgrp = root / "lib" / "dumpGrp-dev.exe"

    print("[setup] Missing Dagor extraction tools.")
    if dumpgrp_path is not None:
        print(f"[setup] Configured dumpGrp path not found: {dumpgrp_path}")
    print(f"[setup] Expected dumpGrp path:   {expected_dumpgrp}")
    print(f"[setup] Download tools-prebuild.windows-x86_64.7z from: {TOOLS_PREBUILD_URL}")
    print(f"[setup] Extract and copy dumpGrp-dev.exe into: {root / 'lib'}")
    print("[setup] Result should include: lib/dumpGrp-dev.exe")


def _parse_mesh_types(raw: str) -> Set[str]:
    toks = {t.strip().lower() for t in raw.split(",") if t.strip()}
    if not toks:
        return {"dynmodel"}
    if "all" in toks:
        return {"all"}
    allowed = {"rendinst", "dynmodel", "skeleton", "collision", "phobj", "generic"}
    bad = sorted(t for t in toks if t not in allowed)
    if bad:
        raise ValueError(f"Unknown --mesh-types values: {', '.join(bad)}")
    return toks


def _parse_lods(raw: str) -> Optional[Set[int]]:
    txt = raw.strip().lower()
    if txt == "all":
        return None
    out: Set[int] = set()
    for part in txt.split(","):
        p = part.strip()
        if not p:
            continue
        if not p.isdigit():
            raise ValueError(f"Invalid LOD value: {p}")
        out.add(int(p))
    if not out:
        return {0}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("grp_file", help="Input .grp file")
    ap.add_argument("output_dir", nargs="?", default="", help="OBJ output dir (default: <grp_dir>/<stem>/)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--split-variants", action="store_true")
    ap.add_argument(
        "--split-collections",
        action="store_true",
        help="Alias of --split-variants. Export one OBJ per collection/variant key.",
    )
    ap.add_argument(
        "--no-split-collections",
        action="store_true",
        help="Disable default split-by-collection export and write a single combined OBJ.",
    )
    ap.add_argument(
        "--compare-file-specific",
        action="store_true",
        help="Enable deprecated filename-gated decoders for parity comparison.",
    )
    ap.add_argument(
        "--mesh-types",
        default="dynmodel",
        help=(
            "Comma-separated mesh categories to export: "
            "rendinst,dynmodel,skeleton,collision,phobj,generic,all "
            "(default: dynmodel)"
        ),
    )
    ap.add_argument(
        "--lods",
        default="0",
        help="Comma-separated LOD indices for rendinst/dynmodel (e.g. 0,1,2) or 'all' (default: 0)",
    )
    args = ap.parse_args()

    try:
        mesh_types = _parse_mesh_types(args.mesh_types)
        lods = _parse_lods(args.lods)
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}")

    grp_path = Path(args.grp_file).resolve()
    if not grp_path.exists():
        raise FileNotFoundError(f"GRP file not found: {grp_path}")

    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        output_dir = grp_path.parent / grp_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    dumpgrp = resolve_dumpgrp_from_config()
    dumpgrp_path = Path(dumpgrp)
    if not dumpgrp_path.exists():
        _print_missing_tools_hint(dumpgrp_path)
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
            include_mesh_types=mesh_types,
            include_lods=lods,
        )
        meshes = parser.parse_directory(extract_dir)

    if not meshes:
        print(f"[{grp_path.stem}] no meshes decoded.")
        chosen_types = ",".join(sorted(parser.include_mesh_types))
        chosen_lods = "all" if parser.include_lods is None else ",".join(str(x) for x in sorted(parser.include_lods))
        print(f"  active filters: mesh-types={chosen_types} lods={chosen_lods}")
        print("  hint: try --mesh-types all --lods all or --mesh-types rendinst --lods 0")
        for r in parser.rejections[:20]:
            print(f"  - {r}")
        raise SystemExit(1)

    split_collections = (not args.no_split_collections) or args.split_variants or args.split_collections

    if split_collections:
        out_files = OBJExporter.export_split_variants(meshes, output_dir)
        print(f"[{grp_path.stem}] {len(out_files)} collections -> {output_dir}")
    else:
        OBJExporter.export(meshes, output_dir, grp_path.stem)
        print(f"[{grp_path.stem}] {len(meshes)} meshes -> {output_dir / (grp_path.stem + '.obj')}")


if __name__ == "__main__":
    main()
