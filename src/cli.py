#!/usr/bin/env python3
"""CLI orchestration stage for converter pipeline."""

from pathlib import Path
from typing import Optional, Set

from exporter import OBJExporter
from grp_converter import GRPResourceParser
from paths import resolve_oodle_from_config


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
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("extracted_dir")
    ap.add_argument(
        "output_dir",
        nargs="?",
        default="",
        help="Output directory. Defaults to <extracted_dir>/output",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Global OBJ export scale multiplier (applied to all vertices)",
    )
    ap.add_argument(
        "--no-auto-scale-fp16",
        action="store_true",
        help="Disable automatic fp16 tiny-mesh upscaling",
    )
    ap.add_argument(
        "--split-variants",
        action="store_true",
        help="Export one OBJ per decoded mesh variant name instead of a single combined OBJ",
    )
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
        help="Enable deprecated file-specific decoders for comparison testing only. "
        "Default decoding is purely structural (class-id + binary headers).",
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

    extracted_dir = Path(args.extracted_dir)
    output_dir = Path(args.output_dir) if args.output_dir else (extracted_dir / "output")
    verbose = args.verbose

    print("GRP Converter")
    print(f"Input:  {extracted_dir.resolve()}")
    print(f"Output: {output_dir.resolve()}")
    print()

    parser = GRPResourceParser(
        verbose=verbose,
        oodle_dll=resolve_oodle_from_config(),
        global_scale=args.scale,
        auto_scale_fp16=(not args.no_auto_scale_fp16),
        comparison_mode=args.compare_file_specific,
        include_mesh_types=mesh_types,
        include_lods=lods,
    )
    meshes = parser.parse_directory(extracted_dir)

    if not meshes:
        print("Error: no valid mesh data decoded from resources.")
        chosen_types = ",".join(sorted(parser.include_mesh_types))
        chosen_lods = "all" if parser.include_lods is None else ",".join(str(x) for x in sorted(parser.include_lods))
        print(f"Active filters: mesh-types={chosen_types} lods={chosen_lods}")
        print("Hint: try --mesh-types all --lods all or --mesh-types rendinst --lods 0")
        print("Rejected resources:")
        for r in parser.rejections[:20]:
            print(f"  - {r}")
        if len(parser.rejections) > 20:
            print(f"  - ... ({len(parser.rejections) - 20} more)")
        raise SystemExit(1)

    split_collections = (not args.no_split_collections) or args.split_variants or args.split_collections

    if split_collections:
        out_files = OBJExporter.export_split_variants(meshes, output_dir)
        print(f"Exported {len(out_files)} collection OBJ files:")
        for p in out_files:
            print(f"  - {p}")
    else:
        basename = extracted_dir.name.replace("__grp_extract", "")
        obj_file = OBJExporter.export(meshes, output_dir, basename)
        print(f"Exported {len(meshes)} mesh objects to: {obj_file}")
    print(f"Total vertices: {sum(int(m['vertex_count']) for m in meshes)}")
    print(f"Total faces: {sum(int(m['face_count']) for m in meshes)}")
    if parser.rejections:
        print(f"Skipped {len(parser.rejections)} resources with invalid/no mesh decode")
        if verbose:
            print("First rejections:")
            for r in parser.rejections[:5]:
                print(f"  - {r}")


if __name__ == "__main__":
    main()
