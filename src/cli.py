#!/usr/bin/env python3
"""CLI orchestration stage for converter pipeline."""

from pathlib import Path

from exporter import OBJExporter
from grp_converter import GRPResourceParser
from paths import resolve_oodle_from_config


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
        "--compare-file-specific",
        action="store_true",
        help="Enable deprecated file-specific decoders for comparison testing only. "
        "Default decoding is purely structural (class-id + binary headers).",
    )
    args = ap.parse_args()

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
    )
    meshes = parser.parse_directory(extracted_dir)

    if not meshes:
        print("Error: no valid mesh data decoded from resources.")
        print("Rejected resources:")
        for r in parser.rejections[:20]:
            print(f"  - {r}")
        if len(parser.rejections) > 20:
            print(f"  - ... ({len(parser.rejections) - 20} more)")
        raise SystemExit(1)

    if args.split_variants:
        out_files = OBJExporter.export_split_variants(meshes, output_dir)
        print(f"Exported {len(meshes)} mesh objects into {len(out_files)} OBJ files:")
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
