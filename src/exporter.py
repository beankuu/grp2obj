#!/usr/bin/env python3
"""OBJ export stage for converter pipeline."""

from pathlib import Path
from typing import Dict, List, Sequence

Mesh = Dict[str, object]


class OBJExporter:
    @staticmethod
    def export(meshes: Sequence[Mesh], output_dir: Path, basename: str) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        obj_path = output_dir / f"{basename}.obj"
        with obj_path.open("w", encoding="utf-8") as fh:
            fh.write("# Converted from Dagor GRP resources\n")
            fh.write(f"# {len(meshes)} mesh objects\n\n")
            v_offset = 1
            for i, mesh in enumerate(meshes, start=1):
                # Allow per-mesh override of the OBJ object name (used by the
                # skeleton decoder so each bone gets its own ``o BONE_NAME``
                # block while still grouping under the parent stem for
                # split-variants export).
                override = mesh.get("obj_object_name")
                if override:
                    name = str(override)
                else:
                    name = str(mesh.get("filename", f"Object_{i}")).split(".")[0]
                vertices = mesh["vertices"]  # type: ignore[index]
                normals = mesh.get("normals")
                faces = mesh["faces"]  # type: ignore[index]
                lines = mesh.get("lines", [])  # type: ignore[index]

                fh.write(f"o {name}\n")
                fh.write(
                    f"# Vertices: {mesh['vertex_count']}, Faces: {mesh['face_count']}, Lines: {mesh.get('line_count', 0)}\n"
                )
                for x, y, z in vertices:  # type: ignore[misc]
                    fh.write(f"v {x} {y} {z}\n")

                if normals:
                    for nx, ny, nz in normals:  # type: ignore[misc]
                        fh.write(f"vn {nx} {ny} {nz}\n")

                for a, b, c in faces:  # type: ignore[misc]
                    va, vb, vc = a + v_offset, b + v_offset, c + v_offset
                    if normals:
                        fh.write(f"f {va}//{va} {vb}//{vb} {vc}//{vc}\n")
                    else:
                        fh.write(f"f {va} {vb} {vc}\n")

                for a, b in lines:  # type: ignore[misc]
                    va, vb = a + v_offset, b + v_offset
                    fh.write(f"l {va} {vb}\n")

                v_offset += int(mesh["vertex_count"])  # type: ignore[index]
                fh.write("\n")
        return obj_path

    @staticmethod
    def _variant_key(mesh: Mesh) -> str:
        # Allow per-mesh override of the split-variant grouping key (used so
        # all named bone meshes of one skeleton land in a single .obj file).
        override = mesh.get("obj_variant_key")
        if override:
            return str(override)
        filename = str(mesh.get("filename", "mesh"))
        stem = filename.split(".")[0]
        return stem if stem else "mesh"

    @staticmethod
    def export_split_variants(meshes: Sequence[Mesh], output_dir: Path) -> List[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        for old in output_dir.glob("*.obj"):
            try:
                old.unlink()
            except Exception:
                pass
        groups: Dict[str, List[Mesh]] = {}
        for mesh in meshes:
            key = OBJExporter._variant_key(mesh)
            groups.setdefault(key, []).append(mesh)

        out_files: List[Path] = []
        for key in sorted(groups.keys()):
            out_files.append(OBJExporter.export(groups[key], output_dir, key))
        return out_files
