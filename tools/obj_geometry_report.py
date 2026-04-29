#!/usr/bin/env python3
"""Generate a markdown listing of vertices and edges for converted OBJ files.

Usage:
    python tools/obj_geometry_report.py <out.md> <obj_path> [<obj_path> ...]

Each OBJ file is parsed with object-scope (`o name`) tracking; per-object
vertex coordinates are listed and edges are derived from `f` (faces) and
`l` (lines), de-duplicated and ordered by vertex index.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


def _parse_obj(path: Path) -> List[Dict[str, object]]:
    """Parse an OBJ file into a list of {name, vertices, edges} per object."""
    global_verts: List[Tuple[float, float, float]] = []
    objects: List[Dict[str, object]] = []
    cur_name = path.stem

    def _resolve(idx_str: str) -> int:
        base = idx_str.split("/")[0]
        idx = int(base)
        if idx > 0:
            return idx - 1
        return len(global_verts) + idx

    def _start_obj(name: str) -> Dict[str, object]:
        return {
            "name": name,
            "v_global_indices": [],  # mapping local index -> global
            "edges": set(),  # set[Tuple[int,int]] of GLOBAL indices
        }

    cur = _start_obj(cur_name)

    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("o "):
            if cur["edges"] or cur["v_global_indices"]:
                objects.append(cur)
            cur_name = line[2:].strip() or path.stem
            cur = _start_obj(cur_name)
            continue
        if line.startswith("v "):
            parts = line.split()
            if len(parts) >= 4:
                global_verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            continue
        if line.startswith("f "):
            toks = line.split()[1:]
            if len(toks) < 3:
                continue
            verts = [_resolve(t) for t in toks]
            n = len(verts)
            for i in range(n):
                a, b = verts[i], verts[(i + 1) % n]
                if a == b:
                    continue
                e = (a, b) if a < b else (b, a)
                cur["edges"].add(e)
            continue
        if line.startswith("l "):
            toks = line.split()[1:]
            verts = [_resolve(t) for t in toks]
            for i in range(len(verts) - 1):
                a, b = verts[i], verts[i + 1]
                if a == b:
                    continue
                e = (a, b) if a < b else (b, a)
                cur["edges"].add(e)
            continue

    if cur["edges"] or cur["v_global_indices"]:
        objects.append(cur)
    if not objects:
        objects.append(cur)

    # Resolve unique vertices used per object (keep ordering by global index).
    for obj in objects:
        used: Set[int] = set()
        for a, b in obj["edges"]:
            used.add(a)
            used.add(b)
        ordered = sorted(used)
        local_of: Dict[int, int] = {g: i for i, g in enumerate(ordered)}
        obj["vertices"] = [global_verts[g] for g in ordered]
        obj["edges_local"] = sorted(
            (
                (local_of[a], local_of[b]) if local_of[a] < local_of[b] else (local_of[b], local_of[a])
            )
            for a, b in obj["edges"]
        )
    return objects


def _format_obj_section(path: Path) -> List[str]:
    objects = _parse_obj(path)
    lines: List[str] = []
    lines.append(f"## {path.name}")
    lines.append("")
    lines.append(f"- Source: `{path.as_posix()}`")
    lines.append(f"- Objects: `{len(objects)}`")
    lines.append("")
    for obj in objects:
        verts: List[Tuple[float, float, float]] = obj["vertices"]
        edges: List[Tuple[int, int]] = obj["edges_local"]
        lines.append(f"### {obj['name']}")
        lines.append("")
        lines.append(f"- Vertices: `{len(verts)}`")
        lines.append(f"- Edges: `{len(edges)}`")
        lines.append("")
        if verts:
            lines.append("#### Vertex locations")
            lines.append("")
            lines.append("| # | x | y | z |")
            lines.append("|---:|---:|---:|---:|")
            for i, (x, y, z) in enumerate(verts):
                lines.append(f"| {i} | {x:.6f} | {y:.6f} | {z:.6f} |")
            lines.append("")
        if edges:
            lines.append("#### Edge connectivity (vertex pairs, 0-based)")
            lines.append("")
            lines.append("| # | a | b | length |")
            lines.append("|---:|---:|---:|---:|")
            for i, (a, b) in enumerate(edges):
                ax, ay, az = verts[a]
                bx, by, bz = verts[b]
                length = ((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5
                lines.append(f"| {i} | {a} | {b} | {length:.6f} |")
            lines.append("")
    return lines


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)

    out_path = Path(sys.argv[1])
    obj_paths = [Path(p) for p in sys.argv[2:]]

    md: List[str] = []
    md.append("# Geometry Report: vertices and edges")
    md.append("")
    md.append("Generated by `tools/obj_geometry_report.py` from OBJ files produced")
    md.append("by the GRP converter running in **default (structural) mode** —")
    md.append("`comparison_mode=False`, no file-specific decoder calls.")
    md.append("")
    md.append("## Files included")
    md.append("")
    for p in obj_paths:
        md.append(f"- `{p.as_posix()}`")
    md.append("")
    for p in obj_paths:
        if not p.exists():
            md.append(f"## {p.name}")
            md.append("")
            md.append(f"- Source: `{p.as_posix()}`")
            md.append("- **MISSING**")
            md.append("")
            continue
        md.extend(_format_obj_section(p))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {out_path} ({sum(1 for _ in md)} lines)")


if __name__ == "__main__":
    main()
