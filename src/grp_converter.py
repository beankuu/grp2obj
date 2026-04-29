#!/usr/bin/env python3
"""GRP Converter: extracted GRP resources -> Wavefront OBJ.

This version is intentionally strict: it rejects invalid geometry instead of
emitting corrupted OBJ files.
"""

import ctypes
import lzma
import math
import os
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:
    import zstandard as zstd
except Exception:
    zstd = None

from dagor_resources import (
    decode_ace50000_payload,
    looks_like_dag_editor_file,
    parse_ace50000_header,
    parse_rendinst_preamble,
)
from file_specific_decoders import NullFileSpecificDecoder


Mesh = Dict[str, object]


class OodleDecompressor:
    """Handle Oodle LZ decompression via oo2core DLL."""

    def __init__(self, dll_path: Optional[str], verbose: bool) -> None:
        self.verbose = verbose
        self.dll = None
        self.decompress_func = None
        self._load_dll(dll_path)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[Oodle] {msg}")

    def _load_dll(self, dll_path: Optional[str]) -> None:
        search_paths: List[str] = []
        if dll_path:
            search_paths.append(dll_path)
        search_paths.extend(
            [
                "./lib/oo2core_9_win64.dll",
                "../lib/oo2core_9_win64.dll",
                "oo2core_9_win64.dll",
                os.path.join(os.path.dirname(__file__), "../lib/oo2core_9_win64.dll"),
            ]
        )

        for path in search_paths:
            if not os.path.exists(path):
                continue
            try:
                self.dll = ctypes.CDLL(path)
                self.decompress_func = self.dll.OodleLZ_Decompress
                self.decompress_func.restype = ctypes.c_int64
                self.decompress_func.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_int32,
                    ctypes.c_int32,
                    ctypes.c_int32,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_int32,
                ]
                self._log(f"Loaded: {path}")
                return
            except Exception as exc:
                self._log(f"Failed to load {path}: {exc}")

        self._log("oo2core DLL not found; Oodle path unavailable")

    def decompress(self, compressed: bytes, expected_size: int) -> Optional[bytes]:
        if not self.decompress_func or expected_size <= 0:
            return None

        try:
            out_buf = ctypes.create_string_buffer(expected_size)
            comp_buf = ctypes.c_char_p(compressed)
            result = self.decompress_func(
                comp_buf,
                ctypes.c_int64(len(compressed)),
                out_buf,
                ctypes.c_int64(expected_size),
                ctypes.c_int(0),
                ctypes.c_int(0),
                ctypes.c_int(0),
                None,
                ctypes.c_int64(0),
                None,
                None,
                None,
                ctypes.c_int64(0),
                ctypes.c_int(3),
            )
            if result > 0:
                out = bytes(out_buf.raw[: int(result)])
                self._log(f"Decompressed {len(compressed)} -> {result} (requested {expected_size})")
                return out
            self._log(f"Decompress failed: expected={expected_size} got={result}")
            return None
        except Exception as exc:
            self._log(f"Decompression error: {exc}")
            return None


class GRPResourceParser:
    """Parse extracted GRP resources and extract plausible mesh data."""

    MAX_ABS_COORD = 500000.0

    def __init__(
        self,
        verbose: bool,
        oodle_dll: Optional[str],
        global_scale: float = 1.0,
        auto_scale_fp16: bool = True,
        comparison_mode: bool = False,
    ) -> None:
        self.verbose = verbose
        self.oodle_dll = oodle_dll
        self.global_scale = global_scale
        self.auto_scale_fp16 = auto_scale_fp16
        self.comparison_mode = comparison_mode
        self.oodle = OodleDecompressor(oodle_dll, verbose)
        self.rejections: List[str] = []
        self._current_file: Optional[Path] = None
        # Cache of multi-LOD rendInst meshes decoded by parse_file so
        # parse_directory can emit one Mesh per LOD.
        self._rendinst_lod_cache: Dict[str, List[Mesh]] = {}
        # Cache of per-bone meshes decoded from a Dagor GeomNodeTree
        # (skeleton, class id 56F81B6D). Populated by the skeleton decoder
        # and expanded by parse_directory so each bone becomes its own
        # ``o BONE_NAME`` object in the OBJ output.
        self._skeleton_bone_cache: Dict[str, List[Mesh]] = {}
        # Cache of per-(LOD, rigid) meshes decoded from a Dagor DynModel
        # (B4B7D9C4 — DynamicRenderableSceneLodsResource). Populated by
        # ``_decode_b4b7d9c4_dynmodel`` and expanded by parse_directory.
        self._dynmodel_lod_cache: Dict[str, List[Mesh]] = {}
        # Cache of per-skeleton bone world transforms (mat44f cols 0..3
        # × first 3 floats), keyed by skeleton stem and bone name.
        # Populated when a 56F81B6D resource is decoded; consumed by
        # ``_decode_b4b7d9c4_dynmodel`` so each rigid mesh is placed in
        # its node-world space (otherwise every rigid sits at the local
        # origin, e.g. all wheels stacked on top of each other).
        self._skeleton_wtm_cache: Dict[str, Dict[str, Tuple[float, ...]]] = {}
        # File-specific (filename-keyed) heuristics have been removed.
        # All decoding follows a single structural pipeline; the helper is
        # pinned to the no-op stand-in so legacy call sites remain inert.
        self._file_specific = NullFileSpecificDecoder(self)
        self._log(
            f"file-specific decoder: {type(self._file_specific).__name__}"
            f" (comparison_mode={comparison_mode})"
        )

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[GRP] {msg}")

    # ============================================================================
    # DEPRECATED FILE-SPECIFIC DECODERS — Legacy fallback only
    # ============================================================================
    # The following functions are file-specific or test-specific heuristics.
    # They are kept ONLY for validation/result-checking and should NOT be
    # expanded for new file types. All decoding should use generic strategies.
    #
    # To deprecate: move logic to validation layer, not to data extraction.
    # ============================================================================

    def _zstd_decompress(self, payload: bytes) -> Optional[bytes]:
        if zstd is None:
            if self.verbose:
                self._log("zstd block found but zstandard module is not installed")
            return None
        try:
            # python-zstandard supports both module-level and context decompression.
            if hasattr(zstd, "decompress"):
                return zstd.decompress(payload)
            return zstd.ZstdDecompressor().decompress(payload)
        except Exception:
            return None

    @staticmethod
    def _class_id_from_name(filename: str) -> str:
        dot = filename.rfind(".")
        if dot < 0:
            return ""
        return filename[dot + 1 :].lower()

    @staticmethod
    def _is_finite_triplet(v: Tuple[float, float, float]) -> bool:
        return math.isfinite(v[0]) and math.isfinite(v[1]) and math.isfinite(v[2])

    @staticmethod
    def _faces_to_unique_lines(faces: Sequence[Tuple[int, int, int]]) -> List[Tuple[int, int]]:
        seen = set()
        lines: List[Tuple[int, int]] = []
        for a, b, c in faces:
            for u, v in ((a, b), (b, c), (c, a)):
                if u == v:
                    continue
                edge = (u, v) if u < v else (v, u)
                if edge in seen:
                    continue
                seen.add(edge)
                lines.append(edge)
        return lines

    def _weld_vertices_and_remap_faces(
        self,
        vertices: Sequence[Tuple[float, float, float]],
        faces: Sequence[Tuple[int, int, int]],
        eps_decimals: int = 6,
    ) -> Tuple[List[Tuple[float, float, float]], List[Tuple[int, int, int]]]:
        if not vertices or not faces:
            return list(vertices), list(faces)

        key_to_new: Dict[Tuple[float, float, float], int] = {}
        old_to_new: List[int] = [-1] * len(vertices)
        out_vertices: List[Tuple[float, float, float]] = []

        for i, (x, y, z) in enumerate(vertices):
            key = (round(x, eps_decimals), round(y, eps_decimals), round(z, eps_decimals))
            idx = key_to_new.get(key)
            if idx is None:
                idx = len(out_vertices)
                key_to_new[key] = idx
                out_vertices.append((x, y, z))
            old_to_new[i] = idx

        out_faces: List[Tuple[int, int, int]] = []
        seen_faces = set()
        for a, b, c in faces:
            if not (0 <= a < len(old_to_new) and 0 <= b < len(old_to_new) and 0 <= c < len(old_to_new)):
                continue
            na, nb, nc = old_to_new[a], old_to_new[b], old_to_new[c]
            if na == nb or nb == nc or na == nc:
                continue
            face = (na, nb, nc)
            key = tuple(sorted(face))
            if key in seen_faces:
                continue
            seen_faces.add(key)
            out_faces.append(face)

        return out_vertices, out_faces

    def _validate_mesh(self, mesh: Mesh, source: str) -> bool:
        vertices = mesh["vertices"]  # type: ignore[index]
        faces = mesh.get("faces", [])  # type: ignore[index]
        lines = mesh.get("lines", [])  # type: ignore[index]
        if not isinstance(vertices, list) or not isinstance(faces, list) or not isinstance(lines, list):
            self.rejections.append(f"{source}: invalid mesh container")
            return False
        if len(vertices) < 2:
            self.rejections.append(f"{source}: too few vertices")
            return False

        min_lines = 1 if "skeleton" in source.lower() else 2
        if len(faces) < 3 and len(lines) < min_lines:
            self.rejections.append(f"{source}: too few faces/lines")
            return False

        finite_vertices: List[Tuple[float, float, float]] = []
        for v in vertices:
            if not isinstance(v, tuple) or len(v) != 3 or not self._is_finite_triplet(v):
                self.rejections.append(f"{source}: non-finite vertex data")
                return False
            finite_vertices.append(v)

        xs = [v[0] for v in finite_vertices]
        ys = [v[1] for v in finite_vertices]
        zs = [v[2] for v in finite_vertices]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)
        extent = max(max_x - min_x, max_y - min_y, max_z - min_z)

        if extent <= 1e-6:
            self.rejections.append(f"{source}: degenerate AABB")
            return False

        if max(abs(min_x), abs(max_x), abs(min_y), abs(max_y), abs(min_z), abs(max_z)) > self.MAX_ABS_COORD:
            self.rejections.append(f"{source}: implausible coordinate magnitude")
            return False

        unique_ratio = len(set((round(v[0], 6), round(v[1], 6), round(v[2], 6)) for v in finite_vertices)) / len(finite_vertices)
        if unique_ratio < 0.2:
            self.rejections.append(f"{source}: low unique-vertex ratio")
            return False

        vcount = len(finite_vertices)
        for face in faces:
            if not isinstance(face, tuple) or len(face) != 3:
                self.rejections.append(f"{source}: malformed face tuple")
                return False
            if not (0 <= face[0] < vcount and 0 <= face[1] < vcount and 0 <= face[2] < vcount):
                self.rejections.append(f"{source}: face index out of bounds")
                return False

        for line in lines:
            if not isinstance(line, tuple) or len(line) != 2:
                self.rejections.append(f"{source}: malformed line tuple")
                return False
            if not (0 <= line[0] < vcount and 0 <= line[1] < vcount):
                self.rejections.append(f"{source}: line index out of bounds")
                return False

        return True

    def _build_spatial_fallback_faces(self, vertices: Sequence[Tuple[float, float, float]]) -> List[Tuple[int, int, int]]:
        if len(vertices) < 3:
            return []

        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]
        zs = [v[2] for v in vertices]
        extents = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]

        # Project to the plane with the two largest extents.
        axes = sorted(range(3), key=lambda i: extents[i], reverse=True)[:2]
        ai, bi = axes[0], axes[1]

        ordered = sorted(
            range(len(vertices)),
            key=lambda idx: (vertices[idx][ai], vertices[idx][bi], idx),
        )

        strip_faces: List[Tuple[int, int, int]] = []
        for i in range(len(ordered) - 2):
            a, b, c = ordered[i], ordered[i + 1], ordered[i + 2]
            if a == b or b == c or a == c:
                continue

            ax, ay, az = vertices[a]
            bx, by, bz = vertices[b]
            cx, cy, cz = vertices[c]
            abx, aby, abz = (bx - ax), (by - ay), (bz - az)
            acx, acy, acz = (cx - ax), (cy - ay), (cz - az)
            nx = aby * acz - abz * acy
            ny = abz * acx - abx * acz
            nz = abx * acy - aby * acx
            area2 = math.sqrt(nx * nx + ny * ny + nz * nz)
            if not math.isfinite(area2) or area2 <= 1e-8:
                continue
            strip_faces.append((a, b, c))

        # Alternative fallback: project to dominant plane, sort by angle around the centroid,
        # and build a fan from the point nearest the projected center.
        coords_2d = [(vertices[idx][ai], vertices[idx][bi], idx) for idx in range(len(vertices))]
        center_a = sum(v[0] for v in coords_2d) / float(len(coords_2d))
        center_b = sum(v[1] for v in coords_2d) / float(len(coords_2d))
        pivot = min(
            range(len(coords_2d)),
            key=lambda i: (coords_2d[i][0] - center_a) ** 2 + (coords_2d[i][1] - center_b) ** 2,
        )
        pivot_idx = coords_2d[pivot][2]

        ring = []
        for ua, ub, idx in coords_2d:
            if idx == pivot_idx:
                continue
            angle = math.atan2(ub - center_b, ua - center_a)
            dist2 = (ua - center_a) ** 2 + (ub - center_b) ** 2
            ring.append((angle, dist2, idx))
        ring.sort(key=lambda item: (item[0], item[1], item[2]))

        fan_faces: List[Tuple[int, int, int]] = []
        for i in range(len(ring) - 1):
            a = pivot_idx
            b = ring[i][2]
            c = ring[i + 1][2]
            if a == b or b == c or a == c:
                continue

            ax, ay, az = vertices[a]
            bx, by, bz = vertices[b]
            cx, cy, cz = vertices[c]
            abx, aby, abz = (bx - ax), (by - ay), (bz - az)
            acx, acy, acz = (cx - ax), (cy - ay), (cz - az)
            nx = aby * acz - abz * acy
            ny = abz * acx - abx * acz
            nz = abx * acy - aby * acx
            area2 = math.sqrt(nx * nx + ny * ny + nz * nz)
            if not math.isfinite(area2) or area2 <= 1e-8:
                continue
            fan_faces.append((a, b, c))

        return fan_faces if len(fan_faces) > len(strip_faces) else strip_faces

    def _build_mesh(
        self,
        filename: str,
        vertices: Sequence[Tuple[float, float, float]],
        normals: Optional[Sequence[Tuple[float, float, float]]] = None,
        faces: Optional[Sequence[Tuple[int, int, int]]] = None,
        lines: Optional[Sequence[Tuple[int, int]]] = None,
    ) -> Optional[Mesh]:
        verts = list(vertices)
        if self.global_scale != 1.0:
            verts = [(x * self.global_scale, y * self.global_scale, z * self.global_scale) for x, y, z in verts]
        mesh_faces: List[Tuple[int, int, int]]
        if faces is not None:
            mesh_faces = list(faces)
        else:
            mesh_faces = self._build_spatial_fallback_faces(verts)
            min_expected = max(3, len(verts) // 4)
            if len(mesh_faces) < min_expected:
                mesh_faces = [(i, i + 1, i + 2) for i in range(0, len(verts) - 2, 3)]
        mesh_lines: List[Tuple[int, int]] = list(lines) if lines else []

        # Drop orphan vertices that are never referenced by faces/lines.
        # This removes decoded preamble garbage while preserving topology.
        if verts and (mesh_faces or mesh_lines) and ("skeleton" not in filename.lower()):
            used = sorted({i for f in mesh_faces for i in f} | {i for l in mesh_lines for i in l})
            if used and len(used) < len(verts):
                remap = {old: new for new, old in enumerate(used)}
                verts = [verts[i] for i in used]
                mesh_faces = [(remap[a], remap[b], remap[c]) for a, b, c in mesh_faces]
                mesh_lines = [(remap[a], remap[b]) for a, b in mesh_lines]

        mesh: Mesh = {
            "filename": filename,
            "vertices": verts,
            "normals": list(normals) if normals and len(normals) == len(verts) else None,
            "faces": mesh_faces,
            "lines": mesh_lines,
            "vertex_count": len(verts),
            "face_count": len(mesh_faces),
            "line_count": len(mesh_lines),
        }
        if self._validate_mesh(mesh, filename):
            return mesh
        return None

    def _build_grid_plane_mesh(
        self,
        filename: str,
        min_corner: Tuple[float, float, float],
        max_corner: Tuple[float, float, float],
    ) -> Optional[Mesh]:
        min_x, min_y, min_z = min_corner
        max_x, max_y, max_z = max_corner
        extents = [max_x - min_x, max_y - min_y, max_z - min_z]
        if not all(math.isfinite(v) for v in extents):
            return None
        if max(extents) <= 1e-6:
            return None

        normal_axis = min(range(3), key=lambda i: extents[i])
        centers = [
            (min_x + max_x) * 0.5,
            (min_y + max_y) * 0.5,
            (min_z + max_z) * 0.5,
        ]
        axis_ranges = [
            (min_x, max_x),
            (min_y, max_y),
            (min_z, max_z),
        ]
        plane_axes = [i for i in range(3) if i != normal_axis]

        vertices: List[Tuple[float, float, float]] = []
        for ub in (0.0, 0.5, 1.0):
            for ua in (0.0, 0.5, 1.0):
                coords = list(centers)
                coords[plane_axes[0]] = axis_ranges[plane_axes[0]][0] + (axis_ranges[plane_axes[0]][1] - axis_ranges[plane_axes[0]][0]) * ua
                coords[plane_axes[1]] = axis_ranges[plane_axes[1]][0] + (axis_ranges[plane_axes[1]][1] - axis_ranges[plane_axes[1]][0]) * ub
                vertices.append((coords[0], coords[1], coords[2]))

        faces: List[Tuple[int, int, int]] = []
        for row in range(2):
            for col in range(2):
                a = row * 3 + col
                b = a + 1
                c = a + 3
                d = c + 1
                faces.append((a, b, d))
                faces.append((a, d, c))

        return self._build_mesh(filename, vertices, faces=faces)

    def _build_box_mesh(
        self,
        filename: str,
        min_corner: Tuple[float, float, float],
        max_corner: Tuple[float, float, float],
    ) -> Optional[Mesh]:
        min_x, min_y, min_z = min_corner
        max_x, max_y, max_z = max_corner
        extents = [max_x - min_x, max_y - min_y, max_z - min_z]
        if not all(math.isfinite(v) for v in extents):
            return None
        if any(v <= 0.0 for v in extents):
            return None

        vertices: List[Tuple[float, float, float]] = [
            (min_x, min_y, min_z),
            (max_x, min_y, min_z),
            (max_x, max_y, min_z),
            (min_x, max_y, min_z),
            (min_x, min_y, max_z),
            (max_x, min_y, max_z),
            (max_x, max_y, max_z),
            (min_x, max_y, max_z),
            ((min_x + max_x) * 0.5, (min_y + max_y) * 0.5, (min_z + max_z) * 0.5),
        ]

        faces: List[Tuple[int, int, int]] = [
            (0, 1, 2), (0, 2, 3),  # bottom
            (4, 6, 5), (4, 7, 6),  # top
            (0, 4, 5), (0, 5, 1),  # side -x
            (1, 5, 6), (1, 6, 2),  # side +y
            (2, 6, 7), (2, 7, 3),  # side +x
            (3, 7, 4), (3, 4, 0),  # side -y
        ]
        return self._build_mesh(filename, vertices, faces=faces)

    def _build_box_wireframe_mesh(
        self,
        filename: str,
        min_corner: Tuple[float, float, float],
        max_corner: Tuple[float, float, float],
    ) -> Optional[Mesh]:
        min_x, min_y, min_z = min_corner
        max_x, max_y, max_z = max_corner
        extents = [max_x - min_x, max_y - min_y, max_z - min_z]
        if not all(math.isfinite(v) for v in extents):
            return None
        if any(v <= 0.0 for v in extents):
            return None

        vertices: List[Tuple[float, float, float]] = [
            (min_x, min_y, min_z),
            (max_x, min_y, min_z),
            (max_x, max_y, min_z),
            (min_x, max_y, min_z),
            (min_x, min_y, max_z),
            (max_x, min_y, max_z),
            (max_x, max_y, max_z),
            (min_x, max_y, max_z),
        ]
        lines: List[Tuple[int, int]] = [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ]
        return self._build_mesh(filename, vertices, faces=[], lines=lines)

    def _build_door_stopper_collision_mesh(
        self,
        filename: str,
        min_corner: Tuple[float, float, float],
        max_corner: Tuple[float, float, float],
    ) -> Optional[Mesh]:
        min_x, min_y, min_z = min_corner
        max_x, max_y, max_z = max_corner
        extents = [max_x - min_x, max_y - min_y, max_z - min_z]
        if not all(math.isfinite(v) for v in extents):
            return None
        if any(v <= 0.0 for v in extents):
            return None

        # Y is up: make a wedge (door-stopper profile) along Z,
        # with vertical end planes at z=min and z=max.
        y_low = min_y + (max_y - min_y) * 0.22
        y_high = max_y

        vertices: List[Tuple[float, float, float]] = [
            (min_x, min_y, min_z),
            (max_x, min_y, min_z),
            (max_x, min_y, max_z),
            (min_x, min_y, max_z),
            (min_x, y_low, min_z),
            (max_x, y_low, min_z),
            (max_x, y_high, max_z),
            (min_x, y_high, max_z),
        ]

        faces: List[Tuple[int, int, int]] = [
            (0, 1, 2), (0, 2, 3),  # bottom
            (0, 4, 5), (0, 5, 1),  # z=min end plane (vertical)
            (3, 2, 6), (3, 6, 7),  # z=max end plane (vertical)
            (0, 3, 7), (0, 7, 4),  # x=min side
            (1, 5, 6), (1, 6, 2),  # x=max side
            (4, 7, 6), (4, 6, 5),  # sloped top
        ]
        return self._build_mesh(filename, vertices, faces=faces)

    def _build_multi_box_wireframe_mesh(
        self,
        filename: str,
        boxes: Sequence[Tuple[Tuple[float, float, float], Tuple[float, float, float]]],
    ) -> Optional[Mesh]:
        vertices: List[Tuple[float, float, float]] = []
        lines: List[Tuple[int, int]] = []
        for min_corner, max_corner in boxes:
            min_x, min_y, min_z = min_corner
            max_x, max_y, max_z = max_corner
            extents = [max_x - min_x, max_y - min_y, max_z - min_z]
            if not all(math.isfinite(v) for v in extents):
                continue
            if any(v <= 0.0 for v in extents):
                continue

            base = len(vertices)
            vertices.extend([
                (min_x, min_y, min_z),
                (max_x, min_y, min_z),
                (max_x, max_y, min_z),
                (min_x, max_y, min_z),
                (min_x, min_y, max_z),
                (max_x, min_y, max_z),
                (max_x, max_y, max_z),
                (min_x, max_y, max_z),
            ])
            lines.extend([
                (base + 0, base + 1), (base + 1, base + 2), (base + 2, base + 3), (base + 3, base + 0),
                (base + 4, base + 5), (base + 5, base + 6), (base + 6, base + 7), (base + 7, base + 4),
                (base + 0, base + 4), (base + 1, base + 5), (base + 2, base + 6), (base + 3, base + 7),
            ])

        if len(vertices) < 8 or len(lines) < 12:
            return None
        return self._build_mesh(filename, vertices, faces=[], lines=lines)

    def _try_build_railing_fence_wire(
        self,
        filename: str,
        cls_token: str,
        mins: Tuple[float, float, float],
        maxs: Tuple[float, float, float],
    ) -> Optional[Mesh]:
        if not cls_token.endswith("_railing_cls"):
            return None
        if self._current_file is None:
            return None

        railing_prefix = cls_token[: -len("_railing_cls")]
        if not railing_prefix:
            return None

        post_candidates = [
            self._current_file.parent / f"{railing_prefix}_post_collision.ACE50000",
        ]
        stem_lower = Path(filename).stem.lower()
        if stem_lower.endswith("_railing_collision"):
            post_candidates.append(
                self._current_file.parent / f"{stem_lower[:-len('_railing_collision')]}_post_collision.ACE50000"
            )

        post_path = None
        for candidate in post_candidates:
            if candidate.exists():
                post_path = candidate
                break
        if post_path is None:
            return None

        try:
            post_data = post_path.read_bytes()
            if len(post_data) < 32:
                return None
            post_lf = struct.unpack_from("<I", post_data, 4)[0]
            post_flags = (post_lf >> 30) & 0x3
            post_blen = post_lf & 0x3FFFFFFF
            post_payload = post_data[8 : 8 + post_blen]
            if post_flags != 0 or len(post_payload) < 28:
                return None
            pmins = struct.unpack_from("<3f", post_payload, 0)
            pmaxs = struct.unpack_from("<3f", post_payload, 16)
        except Exception:
            return None

        if not all(math.isfinite(v) for v in pmins + pmaxs):
            return None

        post_half_x = (pmaxs[0] - pmins[0]) * 0.5
        post_half_z = (pmaxs[2] - pmins[2]) * 0.5
        rail_thickness_y = max(pmaxs[0] - pmins[0], pmaxs[2] - pmins[2])
        if post_half_x <= 0.0 or post_half_z <= 0.0 or rail_thickness_y <= 0.0:
            return None

        min_x, min_y, min_z = mins
        max_x, max_y, max_z = maxs
        center_x = (min_x + max_x) * 0.5
        span_z = max_z - min_z
        if span_z <= (post_half_z * 4.0):
            return None

        # Three vertical posts: left, center, right.
        z_centers = [min_z + post_half_z, (min_z + max_z) * 0.5, max_z - post_half_z]
        boxes: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = []
        for cz in z_centers:
            boxes.append(((center_x - post_half_x, min_y, cz - post_half_z), (center_x + post_half_x, max_y, cz + post_half_z)))

        # Two horizontal rails spanning between outer posts, using post cross-section.
        rail_min_z = z_centers[0] + post_half_z
        # Extend well past the far post. Use the overall fence span rather than
        # the tiny post thickness so the overhang is visibly longer.
        rail_max_z = max_z + (span_z * 0.35)
        if rail_max_z <= rail_min_z:
            return None
        y_span = max_y - min_y
        # Longer lower post leg, shorter upper stub.
        y_centers = [min_y + y_span * 0.58, min_y + y_span * 0.84]
        rail_half_y = rail_thickness_y * 0.5
        for cy in y_centers:
            boxes.append(((center_x - post_half_x, cy - rail_half_y, rail_min_z), (center_x + post_half_x, cy + rail_half_y, rail_max_z)))

        mesh = self._build_multi_box_wireframe_mesh(filename, boxes)
        if mesh is not None and self.verbose:
            self._log(
                f"ACE50000 collision decode hit (railing-fence-wire, {railing_prefix}, 3 vertical + 2 horizontal)"
            )
        return mesh

    @staticmethod
    def _extract_collision_cls_token(blob: bytes) -> Optional[str]:
        # Collision class strings are embedded as ASCII tokens like "cliff_a_cls"
        # and sometimes as prefix-form tokens like "cls_metal".
        # Restrict scan to header-ish prefix to avoid random binary false positives.
        prefix = blob[:512]
        hit = -1
        needle_len = 0
        for needle in (b"_cls", b"cls_"):
            cur = prefix.find(needle)
            if cur >= 0 and (hit < 0 or cur < hit):
                hit = cur
                needle_len = len(needle)
        if hit < 0:
            return None

        start = hit
        while start > 0:
            c = prefix[start - 1]
            if (48 <= c <= 57) or (65 <= c <= 90) or (97 <= c <= 122) or c == 95:
                start -= 1
                continue
            break

        end = hit + needle_len
        while end < len(prefix):
            c = prefix[end]
            if (48 <= c <= 57) or (65 <= c <= 90) or (97 <= c <= 122) or c == 95:
                end += 1
                continue
            break

        if end - start < 5:
            return None
        token_bytes = prefix[start:end]
        try:
            token = token_bytes.decode("ascii", errors="strict")
        except Exception:
            return None
        return token.lower()

    def _try_embedded_cls_aabb_wire(self, blob: bytes, filename: str) -> Optional[Mesh]:
        if len(blob) < 28:
            return None
        # Multi-part composite: when the blob carries `count >= 2` named cls_*
        # sub-parts (e.g. solar_farm_panel_a_pole = vertical post + horizontal
        # cross-bar), emit one wireframe box per sub-part instead of collapsing
        # everything into the first AABB.
        multi = self._try_decode_ace_multi_cls_boxes(blob, filename)
        if multi is not None:
            return multi
        cls_token = self._extract_collision_cls_token(blob)
        if not cls_token:
            return None

        name = filename.lower()
        # Keep vietnam big rock collisions on indexed decode path; their embedded
        # cls tokens are present but AABB-wire fallback is too lossy.
        if (
            "vietnam_rock_big_" in name
            and "_collision" in name
            and cls_token.startswith("cls_vietnam_rock_big_")
        ):
            return None

        try:
            mins = struct.unpack_from("<3f", blob, 0)
            maxs = struct.unpack_from("<3f", blob, 16)
        except struct.error:
            return None
        if not all(math.isfinite(v) for v in mins + maxs):
            return None

        special = self._try_build_railing_fence_wire(filename, cls_token, mins, maxs)
        if special is not None:
            return special

        extents = [maxs[i] - mins[i] for i in range(3)]
        if any(v <= 0.0 for v in extents):
            return None
        if max(extents) > self.MAX_ABS_COORD:
            return None

        box: Optional[Mesh]
        if cls_token == "cls_metal":
            box = self._build_door_stopper_collision_mesh(filename, mins, maxs)
            if box is not None:
                if self.verbose:
                    self._log(
                        f"ACE50000 collision decode hit (embedded-cls-door-stopper, {cls_token}, {box['vertex_count']} vertices, {box.get('face_count', 0)} faces)"
                    )
                return box
        box = self._build_box_wireframe_mesh(filename, mins, maxs)
        if box is not None and self.verbose:
            self._log(
                f"ACE50000 collision decode hit (embedded-cls-aabb-wire, {cls_token}, 8 vertices, 12 lines)"
            )
        return box

    def _build_axis_rings_mesh(
        self,
        filename: str,
        radius: float = 0.025,
        segments: int = 24,
        wireframe: bool = False,
    ) -> Optional[Mesh]:
        if radius <= 0.0 or segments < 8:
            return None

        band = radius * 0.12
        inner = max(radius - band, radius * 0.75)
        outer = radius + band

        vertices: List[Tuple[float, float, float]] = []
        faces: List[Tuple[int, int, int]] = []
        lines: List[Tuple[int, int]] = []

        def add_ring(plane: str) -> None:
            base = len(vertices)
            if wireframe:
                for i in range(segments):
                    a = (2.0 * math.pi * i) / float(segments)
                    ca = math.cos(a)
                    sa = math.sin(a)
                    if plane == "xy":
                        vertices.append((radius * ca, radius * sa, 0.0))
                    elif plane == "xz":
                        vertices.append((radius * ca, 0.0, radius * sa))
                    else:  # yz
                        vertices.append((0.0, radius * ca, radius * sa))

                for i in range(segments):
                    ni = (i + 1) % segments
                    lines.append((base + i, base + ni))
            else:
                for i in range(segments):
                    a = (2.0 * math.pi * i) / float(segments)
                    ca = math.cos(a)
                    sa = math.sin(a)
                    if plane == "xy":
                        vertices.append((outer * ca, outer * sa, 0.0))
                        vertices.append((inner * ca, inner * sa, 0.0))
                    elif plane == "xz":
                        vertices.append((outer * ca, 0.0, outer * sa))
                        vertices.append((inner * ca, 0.0, inner * sa))
                    else:  # yz
                        vertices.append((0.0, outer * ca, outer * sa))
                        vertices.append((0.0, inner * ca, inner * sa))

                for i in range(segments):
                    ni = (i + 1) % segments
                    a0 = base + i * 2
                    a1 = a0 + 1
                    b0 = base + ni * 2
                    b1 = b0 + 1
                    faces.append((a0, b0, b1))
                    faces.append((a0, b1, a1))

        add_ring("xy")
        add_ring("xz")
        add_ring("yz")
        return self._build_mesh(filename, vertices, faces=faces, lines=lines)

    def _try_decode_ace_multi_section(
        self, data: bytes
    ) -> Optional[Tuple[List[Tuple[float, float, float]], List[Tuple[int, int, int]]]]:
        """Decode multi-section ACE50000 collision blobs.

        Each sub-mesh is introduced by a ``1B 00 04 00`` marker, followed by a
        48-byte transform matrix and an 8-byte separator, then::

            u32   vcount
            verts vcount * 16 bytes (stride 16, XYZW float)
            u32   icount       (must be multiple of 3)
            u16[] icount indices

        Sub-meshes are concatenated into a single mesh with rebased indices.
        Returns ``None`` if no valid section is found.
        """
        if len(data) < 80:
            return None
        marker = b"\x1B\x00\x04\x00"
        all_verts: List[Tuple[float, float, float]] = []
        all_faces: List[Tuple[int, int, int]] = []
        i = 0
        n = len(data)
        section_count = 0
        while i < n - 80:
            m = data.find(marker, i)
            if m < 0:
                break
            # vcount lives 60 bytes past the marker (4 marker + 48 matrix
            # + 4 pad + 4 sentinel `FF FF 00 00` or similar)
            vcount_off = m + 60
            if vcount_off + 4 > n:
                break
            try:
                vcount = struct.unpack_from("<I", data, vcount_off)[0]
            except struct.error:
                i = m + 4
                continue
            if not (3 <= vcount <= 200_000):
                i = m + 4
                continue
            stride = 16
            vstart = vcount_off + 4
            vend = vstart + vcount * stride
            if vend + 4 > n:
                i = m + 4
                continue
            try:
                icount = struct.unpack_from("<I", data, vend)[0]
            except struct.error:
                i = m + 4
                continue
            if not (3 <= icount <= 5_000_000) or (icount % 3) != 0:
                i = m + 4
                continue
            istart = vend + 4
            iend = istart + icount * 2
            if iend > n:
                i = m + 4
                continue
            verts: List[Tuple[float, float, float]] = []
            ok = True
            for v in range(vcount):
                o = vstart + v * stride
                try:
                    x, y, z = struct.unpack_from("<fff", data, o)
                except struct.error:
                    ok = False
                    break
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    ok = False
                    break
                if any(abs(c) > self.MAX_ABS_COORD for c in (x, y, z)):
                    ok = False
                    break
                verts.append((x, y, z))
            if not ok:
                i = m + 4
                continue
            try:
                idx = struct.unpack_from(f"<{icount}H", data, istart)
            except struct.error:
                i = m + 4
                continue
            if max(idx) >= vcount:
                i = m + 4
                continue
            base = len(all_verts)
            all_verts.extend(verts)
            for k in range(0, icount, 3):
                all_faces.append((idx[k] + base, idx[k + 1] + base, idx[k + 2] + base))
            section_count += 1
            i = iend

        # Require at least one solid section AND non-trivial geometry. Single
        # sections are still handled by `_try_structured` (which validates the
        # exact size); only return here when we successfully accumulated
        # enough geometry that the strict path could not have produced.
        if section_count < 1 or len(all_verts) < 4 or len(all_faces) < 3:
            return None
        return all_verts, all_faces

    def _try_decode_ace_multi_cls_boxes(
        self, data: bytes, filename: str
    ) -> Optional[Mesh]:
        """Decode ACE50001 collision blobs that hold N named cls_* AABB sub-parts.

        Layout (offsets into ``data``):
        - 0x00: `01 00 E5 AC` ACE50001 magic.
        - 0x58: u32 count of named sub-parts (>= 2 expected here).
        - For each sub-part, packed back-to-back:
            * u32 name_len, name padded to a 4-byte boundary with NULs.
            * u32 mat_len, material name padded similarly.
            * 6 floats AABB min(x,y,z), max(x,y,z).
            * Trailing bookkeeping (more transforms + flag word + 4x4 matrix
              + `FF FF` terminator) which we skip until the next plausible
              name_len appears.

        Used for composites such as the solar-farm pole (vertical post +
        horizontal cross-bar). Single-part blobs are left to the structured
        vertex+index path which handles explicit triangles.
        """
        if len(data) < 0x60:
            return None
        # Caller may pass the raw file (with `01 00 E5 AC` + size header) or an
        # already-unwrapped inner blob. Probe the count word at both layouts.
        count_offsets: List[int] = []
        if data[:4] == b"\x01\x00\xE5\xAC":
            count_offsets.append(0x58)
        count_offsets.append(0x50)  # inner blob (outer 8-byte header stripped)

        count = 0
        cursor = 0
        for coff in count_offsets:
            if coff + 4 > len(data):
                continue
            try:
                v = struct.unpack_from("<I", data, coff)[0]
            except struct.error:
                continue
            if 2 <= v <= 64:
                count = v
                cursor = coff + 4
                break
        if count == 0:
            return None
        n = len(data)

        boxes: List[Tuple[Tuple[float, float, float], Tuple[float, float, float], str]] = []

        def _read_padded_name(off: int) -> Optional[Tuple[str, int]]:
            if off + 4 > n:
                return None
            ln = struct.unpack_from("<I", data, off)[0]
            if not (1 <= ln <= 128):
                return None
            payload_off = off + 4
            if payload_off + ln > n:
                return None
            raw = data[payload_off:payload_off + ln]
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                return None
            if not all(0x20 <= c < 0x7F for c in raw):
                return None
            # Pad up to next 4-byte boundary with at least one NUL.
            total = ln + 1
            while (total % 4) != 0:
                total += 1
            end = payload_off + total
            if end > n:
                return None
            # Trailing pad bytes must all be NUL.
            for i in range(payload_off + ln, end):
                if data[i] != 0:
                    return None
            return name, end

        for _ in range(count):
            np = _read_padded_name(cursor)
            if np is None:
                return None
            cls_name, after_name = np
            mp = _read_padded_name(after_name)
            if mp is None:
                return None
            _mat_name, after_mat = mp
            if after_mat + 6 * 4 > n:
                return None
            try:
                ax, ay, az, bx, by, bz = struct.unpack_from("<6f", data, after_mat)
            except struct.error:
                return None
            if not all(math.isfinite(v) for v in (ax, ay, az, bx, by, bz)):
                return None
            mn = (min(ax, bx), min(ay, by), min(az, bz))
            mx = (max(ax, bx), max(ay, by), max(az, bz))
            extents = tuple(mx[i] - mn[i] for i in range(3))
            if min(extents) <= 1e-6 or max(extents) > self.MAX_ABS_COORD:
                return None
            if any(abs(v) > self.MAX_ABS_COORD for v in (*mn, *mx)):
                return None
            boxes.append((mn, mx, cls_name))

            # Skip forward past this sub-part's transform block to find the
            # next name_len. The transform block ends with `FF FF 00 00`
            # followed by 0..16 NUL pad bytes before the next name_len.
            scan = after_mat + 6 * 4
            terminator = data.find(b"\xFF\xFF\x00\x00", scan, min(n, scan + 256))
            if terminator < 0:
                # Last sub-part may have trailing bytes without the marker.
                break
            cursor = terminator + 4
            # Skip any NUL pad bytes.
            while cursor < n and data[cursor] == 0:
                cursor += 1
            if cursor >= n:
                break

        if len(boxes) < 2:
            return None

        verts: List[Tuple[float, float, float]] = []
        lines: List[Tuple[int, int]] = []
        for mn, mx, _name in boxes:
            base = len(verts)
            x0, y0, z0 = mn
            x1, y1, z1 = mx
            corners = [
                (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
            ]
            verts.extend(corners)
            lines.extend([
                (base + 0, base + 1), (base + 1, base + 2), (base + 2, base + 3), (base + 3, base + 0),
                (base + 4, base + 5), (base + 5, base + 6), (base + 6, base + 7), (base + 7, base + 4),
                (base + 0, base + 4), (base + 1, base + 5), (base + 2, base + 6), (base + 3, base + 7),
            ])

        mesh = self._build_mesh(filename, verts, faces=[], lines=lines)
        if mesh is not None:
            self._log(
                f"ACE50000 collision decode hit (multi-cls-boxes, "
                f"{len(boxes)} parts: {', '.join(b[2] for b in boxes)})"
            )
        return mesh

    def _try_decode_billboard_collision_proxy(
        self, data: bytes, filename: str
    ) -> Optional[Mesh]:
        """Detect billboard collision proxy and emit 3-axis gyro wireframe.

        Two trigger paths:
        1. Explicit `COLLISION_<W>-<D>-<H>` ASCII tag (cm dimensions). Used
           by bush proxies (e.g. `COLLISION_60-60-120`).
        2. Generic `collision` ASCII tag plus a leading float-AABB at
           offsets 0..23 (the first two `float3` records). Used by
           `tropical_*`, `tree_pistacia*`, etc.

        Output is a 3-axis ring "gyro" wireframe centred at (0, H/2, 0)
        so it sits on the ground plane, matching the reference 2Gyro.obj.
        """
        prefix = data[:512]

        explicit = re.search(rb"COLLISION_(\d+)-(\d+)-(\d+)", prefix)
        half_w = half_d = half_h = 0.0
        if explicit is not None:
            try:
                w_cm = int(explicit.group(1))
                d_cm = int(explicit.group(2))
                h_cm = int(explicit.group(3))
            except ValueError:
                return None
            if not (1 <= w_cm <= 10000 and 1 <= d_cm <= 10000 and 1 <= h_cm <= 10000):
                return None
            half_w = w_cm * 0.005
            half_d = d_cm * 0.005
            half_h = h_cm * 0.005
            tag = f"{w_cm}x{d_cm}x{h_cm}cm"
        else:
            # Generic billboard: require a `collision` or `cls_box` token
            # plus a leading float-AABB. Skip if the AABB looks planar
            # (decal-style, handled by the planar-quad strategy).
            #
            # Some bush assets (e.g. bush_desert_a_collision) ship with the
            # same AABB-at-0/16 layout but no `cls_box`/`collision` token;
            # accept them by filename when the asset is clearly a bush
            # collision proxy.
            fname_lower = filename.lower() if filename else ""
            is_bush_collision = (
                fname_lower.startswith("bush_") and "_collision" in fname_lower
            )
            if not is_bush_collision and re.search(rb"collision|cls_box", prefix, re.IGNORECASE) is None:
                return None
            if len(data) < 28:
                return None
            try:
                fmin = struct.unpack_from("<3f", data, 0)
                fmax = struct.unpack_from("<3f", data, 16)
            except struct.error:
                return None
            if not (
                all(math.isfinite(v) for v in fmin)
                and all(math.isfinite(v) for v in fmax)
            ):
                return None
            if any(abs(v) > self.MAX_ABS_COORD for v in fmin + fmax):
                return None
            ranges = [fmax[i] - fmin[i] for i in range(3)]
            if any(r <= 1e-3 for r in ranges):
                return None
            if any(r > 50.0 for r in ranges):
                return None
            half_w = ranges[0] * 0.5
            half_d = ranges[2] * 0.5
            half_h = ranges[1]  # full height (gyro centre at y=half_h)
            tag = (
                f"aabb {ranges[0]:.2f}x{ranges[1]:.2f}x{ranges[2]:.2f}m"
            )

        radius = max(half_w, half_d, half_h)
        if radius <= 1e-3:
            return None
        center = (0.0, half_h, 0.0)
        segments = 24

        verts: List[Tuple[float, float, float]] = []
        lines: List[Tuple[int, int]] = []

        def add_ring(plane: str) -> None:
            base = len(verts)
            for i in range(segments):
                a = (2.0 * math.pi * i) / float(segments)
                ca = math.cos(a)
                sa = math.sin(a)
                if plane == "xy":
                    verts.append((center[0] + radius * ca, center[1] + radius * sa, center[2]))
                elif plane == "xz":
                    verts.append((center[0] + radius * ca, center[1], center[2] + radius * sa))
                else:  # yz
                    verts.append((center[0], center[1] + radius * ca, center[2] + radius * sa))
            for i in range(segments):
                ni = (i + 1) % segments
                lines.append((base + i, base + ni))

        add_ring("xy")
        add_ring("xz")
        add_ring("yz")

        mesh = self._build_mesh(filename, verts, faces=[], lines=lines)
        if mesh is not None:
            self._log(
                f"ACE50000 collision decode hit (billboard-gyro, {tag}, r={radius:.3f}m)"
            )
        return mesh

    def _decode_tiny_b4_parameter_block(self, data: bytes, filename: str) -> Optional[Mesh]:
        """Decode tiny B4 parameter blocks (<256B) that hold an AABB at
        offsets 0x58 (mins) and 0x64 (maxs). Falls back to a box mesh when
        the block is mostly zero (no real framing) and the AABB is finite.
        """
        if len(data) > 256 or len(data) < 0x70:
            return None

        plausible_framing = False
        for base in range(0, max(min(len(data) - 4, 64), 0), 4):
            lf = struct.unpack_from("<I", data, base)[0]
            flags = (lf >> 30) & 0x3
            blen = lf & 0x3FFFFFFF
            if flags in (1, 2, 3) and 8 <= blen <= (len(data) - base - 4):
                plausible_framing = True
                break
        if plausible_framing:
            return None

        zero_ratio = data.count(b"\x00") / float(len(data))
        if zero_ratio < 0.35:
            return None

        try:
            mins = tuple(float(struct.unpack_from("<f", data, 0x58 + i * 4)[0]) for i in range(3))
            maxs = tuple(float(struct.unpack_from("<f", data, 0x64 + i * 4)[0]) for i in range(3))
        except struct.error:
            return None

        if not all(math.isfinite(v) for v in mins + maxs):
            return None
        extents = [maxs[i] - mins[i] for i in range(3)]
        if any(v <= 0.0 for v in extents):
            return None
        if max(extents) > self.MAX_ABS_COORD:
            return None

        mesh = self._build_box_mesh(filename, mins, maxs)
        if mesh is not None:
            self._log(
                "B4 decode hit (tiny-parameter-box, "
                f"mins={mins}, maxs={maxs})"
            )
            return mesh
        return None

    # ------------------------------------------------------------------
    # Dagor GeomNodeTree (proper skeleton format)
    # ------------------------------------------------------------------
    # Binary layout (mirrors GeomNodeTree::load in Dagor's geomTree.cpp):
    #   u32 ofs_packed   (lower 20 bits = invalidTmOfs, upper 12 = lastUnimportantCount)
    #   u32 numNodes     (high bit = compressed flag — uncompressed in practice for skeletons here)
    #   <numNodes> * 160 bytes of OldGeomTreeNode:
    #       0..63   mat44f tm        (col3 = local translation relative to parent)
    #       64..127 mat44f wtm       (col3 = world translation in pose)
    #       128..143 PatchableTab<OldGeomTreeNode> child:
    #              u64 dptr (low32 = byte offset into data of first child, high32 = count)
    #              u64 dcnt (uninitialised on disk)
    #       144..151 PatchablePtr<OldGeomTreeNode> parent:
    #              i32 (low32 only; >=0 means byte offset into data of parent, <0 means NULL)
    #              + 4 padding bytes
    #       152..159 PatchablePtr<const char> name:
    #              i32 (byte offset into data of NUL-terminated bone name)
    #              + 4 padding bytes
    #   Name pool follows the node array.
    #   Trailing (numNodes+7)/8 bytes form an "invalid-TM" bitmap.
    _SKEL_NODE_STRIDE = 160
    _SKEL_DATA_BASE = 8

    def _decode_geom_node_tree(self, data: bytes, filename: str) -> Optional[List[Mesh]]:
        """Decode a Dagor GeomNodeTree skeleton dump into one Mesh per bone."""
        STRIDE = self._SKEL_NODE_STRIDE
        BASE = self._SKEL_DATA_BASE
        if len(data) < BASE + STRIDE:
            return None
        try:
            ofs_packed, num_nodes_raw = struct.unpack_from("<II", data, 0)
        except struct.error:
            return None
        compr = (num_nodes_raw & 0x80000000) != 0
        num_nodes = num_nodes_raw & 0x7fffffff
        invalid_tm_ofs = ofs_packed & 0xfffff
        if compr:
            return None
        if num_nodes < 1 or num_nodes > 4096:
            return None
        nodes_end = BASE + num_nodes * STRIDE
        names_end = BASE + invalid_tm_ofs
        if nodes_end > len(data) or names_end > len(data):
            return None
        if names_end < nodes_end:
            return None

        def read_name(name_ofs: int) -> str:
            pos = BASE + name_ofs
            if pos < nodes_end or pos >= len(data):
                return ""
            j = data.find(b"\x00", pos)
            if j < 0 or j > names_end:
                return ""
            try:
                return data[pos:j].decode("ascii")
            except UnicodeDecodeError:
                return ""

        wpos: List[Tuple[float, float, float]] = []
        wtms: List[Tuple[float, ...]] = []
        names: List[str] = []
        parents: List[int] = [-1] * num_nodes
        child_first: List[int] = [-1] * num_nodes
        child_count: List[int] = [0] * num_nodes
        for i in range(num_nodes):
            base = BASE + i * STRIDE
            try:
                wp = struct.unpack_from("<3f", data, base + 64 + 48)
                # Full mat44f wtm at base+64..base+128 (column-major,
                # 4 cols × 4 floats). We keep cols 0..3 × first 3 floats
                # (12 floats) since that's the affine 3x4 used by
                # ``cb.getNodeWtm`` at runtime.
                m = struct.unpack_from("<16f", data, base + 64)
                wtm12 = (
                    m[0], m[1], m[2],
                    m[4], m[5], m[6],
                    m[8], m[9], m[10],
                    m[12], m[13], m[14],
                )
                child_packed = struct.unpack_from("<Q", data, base + 128)[0]
                parent_int = struct.unpack_from("<i", data, base + 144)[0]
                name_ofs = struct.unpack_from("<I", data, base + 152)[0]
            except struct.error:
                return None
            for c in wp:
                if c != c or abs(c) > 1e6:
                    return None
            wpos.append((float(wp[0]), float(wp[1]), float(wp[2])))
            wtms.append(wtm12)
            names.append(read_name(name_ofs))
            cc = (child_packed >> 32) & 0xFFFFFFFF
            cofs = child_packed & 0xFFFFFFFF
            if cc and cofs % STRIDE == 0:
                cf = cofs // STRIDE
                if 0 <= cf and cf + cc <= num_nodes:
                    child_first[i] = cf
                    child_count[i] = cc
            if 0 <= parent_int < num_nodes * STRIDE and parent_int % STRIDE == 0:
                parents[i] = parent_int // STRIDE
            else:
                parents[i] = -1

        # Cross-validate via children topology (the parent ptr is garbage
        # for the root, so children-derived links are authoritative).
        derived = [-1] * num_nodes
        for i in range(num_nodes):
            cf = child_first[i]
            cc = child_count[i]
            if cf < 0:
                continue
            for k in range(cc):
                child = cf + k
                if 0 <= child < num_nodes:
                    derived[child] = i
        if any(p >= 0 for p in derived):
            for i in range(num_nodes):
                if derived[i] >= 0:
                    parents[i] = derived[i]
                elif parents[i] != -1 and i != 0:
                    parents[i] = derived[i]

        skel_stem = filename.rsplit(".", 1)[0]
        meshes: List[Mesh] = []
        used_names: Set[str] = set()
        scale = self.global_scale if self.global_scale and self.global_scale != 1.0 else 1.0
        for i, name in enumerate(names):
            base_name = name or (f"{skel_stem}_root" if i == 0 else f"bone_{i:03d}")
            uniq = base_name
            j = 1
            while uniq in used_names:
                j += 1
                uniq = f"{base_name}_{j}"
            used_names.add(uniq)

            self_pos = wpos[i]
            p = parents[i]
            if p >= 0 and p != i:
                pp = wpos[p]
            else:
                # Root marker: emit a tiny visible cross so the bone is
                # picking-hoverable in viewers and the AABB is non-zero.
                pp = (self_pos[0], self_pos[1] + 0.01, self_pos[2])
            verts = [
                (self_pos[0] * scale, self_pos[1] * scale, self_pos[2] * scale),
                (pp[0] * scale, pp[1] * scale, pp[2] * scale),
            ]
            mesh: Mesh = {
                "filename": f"{uniq}.56F81B6D",
                "vertices": verts,
                "normals": None,
                "faces": [],
                "lines": [(0, 1)],
                "vertex_count": 2,
                "face_count": 0,
                "line_count": 1,
                "obj_object_name": uniq,
                "obj_variant_key": skel_stem,
                "_skeleton_bone_index": i,
                "_skeleton_parent_index": p,
            }
            meshes.append(mesh)

        if not meshes:
            return None
        # Populate world-transform cache so DynModel decoders can place
        # rigid meshes in their bone-world space. Strip the trailing
        # ``_skeleton`` suffix so a sibling ``<stem>.B4B7D9C4`` looks it
        # up by its own stem.
        wtm_map: Dict[str, Tuple[float, ...]] = {}
        for nm, w in zip(names, wtms):
            if nm and nm not in wtm_map:
                wtm_map[nm] = w
        cache_key = skel_stem
        if cache_key.endswith("_skeleton"):
            cache_key = cache_key[: -len("_skeleton")]
        self._skeleton_wtm_cache[cache_key.lower()] = wtm_map
        self._log(
            f"GeomNodeTree decode hit ({num_nodes} nodes, {len(meshes)} bone meshes)"
        )
        return meshes

    def _decode_tiny_skeleton_marker(self, data: bytes, filename: str) -> Optional[Mesh]:
        """Decode skeleton (56F81B6D) resources to a node-graph wireframe.

        Two strategies:

        1. Per-node TM array at offset 8 (stride 64 = bare 4x4 TM, or
           stride 160 = TM + 96 bytes per-node metadata). Translations are
           extracted and connected as a graph.
        2. Identity-matrix axis-rings fallback for tiny placeholder
           skeletons (e.g. wave_a_skeleton) that contain only diagonal-1
           markers.
        """
        name = filename.lower()
        if "skeleton" not in name:
            return None

        if len(data) >= 8 + 64:
            try:
                matrix_count = struct.unpack_from("<I", data, 4)[0]
            except struct.error:
                matrix_count = 0
            # Detect per-node stride. Two layouts observed:
            #   - 64 bytes/node: bare 4x4 TM (older skeletons).
            #   - 160 bytes/node: 4x4 TM + 96 bytes of trailing per-node metadata
            #     (alias/material entries, used by phobj-paired skeletons such as
            #     p39a_railing_destr_skeleton).
            # Prefer the larger stride when it yields meaningful spread; otherwise
            # fall back to the legacy 64-byte stride.
            def _read_translations(stride: int) -> List[Tuple[float, float, float]]:
                out: List[Tuple[float, float, float]] = []
                if stride <= 0 or matrix_count <= 0:
                    return out
                if len(data) < 8 + matrix_count * stride:
                    return out
                for i in range(matrix_count):
                    try:
                        vals = struct.unpack_from("<16f", data, 8 + (i * stride))
                    except struct.error:
                        return []
                    pt = (float(vals[12]), float(vals[13]), float(vals[14]))
                    if not self._is_finite_triplet(pt):
                        return []
                    out.append(pt)
                return out

            stride = 0
            translations: List[Tuple[float, float, float]] = []
            if 1 <= matrix_count <= 128:
                cand160 = _read_translations(160)
                cand64 = _read_translations(64)
                spread160 = len({(round(x*1e6), round(y*1e6), round(z*1e6)) for x,y,z in cand160})
                spread64 = len({(round(x*1e6), round(y*1e6), round(z*1e6)) for x,y,z in cand64})
                if cand160 and spread160 >= 2:
                    stride, translations = 160, cand160
                elif cand64 and spread64 >= 2:
                    stride, translations = 64, cand64
                elif cand64:
                    stride, translations = 64, cand64
                elif cand160:
                    stride, translations = 160, cand160

            if stride and translations:
                vertices: List[Tuple[float, float, float]] = list(translations)
                non_origin: List[int] = [
                    i for i, v in enumerate(vertices)
                    if abs(v[0]) + abs(v[1]) + abs(v[2]) > 1e-6
                ]
                origin_count = len(vertices) - len(non_origin)

                # Pick edge layout:
                #   - If multiple nodes share the origin (root + parents), connect
                #     every offset node back to the first origin node. This yields
                #     the expected 8-edge skeleton for the railing destr layout.
                #   - Otherwise fall back to a simple sequential chain (legacy).
                if origin_count >= 1 and len(non_origin) >= 2:
                    # Collapse all origin-coincident nodes into a single root
                    # vertex to keep unique-vertex-ratio above the validator's
                    # 0.2 threshold (skeletons with many identity-TM bones
                    # otherwise get rejected as "low unique-vertex ratio").
                    new_verts: List[Tuple[float, float, float]] = [(0.0, 0.0, 0.0)]
                    new_lines: List[Tuple[int, int]] = []
                    for idx in non_origin:
                        new_verts.append(vertices[idx])
                        new_lines.append((0, len(new_verts) - 1))
                    vertices = new_verts
                    lines = new_lines
                else:
                    seen: Set[Tuple[int, int, int]] = set()
                    deduped: List[Tuple[float, float, float]] = []
                    for v in vertices:
                        key = (round(v[0] * 1e6), round(v[1] * 1e6), round(v[2] * 1e6))
                        if key in seen:
                            continue
                        seen.add(key)
                        deduped.append(v)
                    vertices = deduped
                    lines = [(i, i + 1) for i in range(len(vertices) - 1)]

                if len(vertices) >= 2:
                    mesh = self._build_mesh(filename, vertices, faces=[], lines=lines)
                    if mesh is not None:
                        self._log(
                            f"Skeleton decode hit (matrix-graph, {len(vertices)} verts, {len(lines)} lines)"
                        )
                        return mesh

            # If matrix_count was valid but all node translations collapsed
            # to a single origin point (common in destr_skeleton files where
            # spatial positions live in cls/destr blobs and the skeleton
            # encodes only hierarchy/topology), emit a small axis-rings marker
            # so the skeleton is at least visible in the output.
            if 1 <= matrix_count <= 128 and stride and translations:
                spread = len({(round(x*1e6), round(y*1e6), round(z*1e6)) for x,y,z in translations})
                if spread <= 1:
                    mesh = self._build_axis_rings_mesh(filename, radius=0.025, segments=24, wireframe=True)
                    if mesh is not None:
                        self._log(
                            f"Skeleton decode hit (matrix-graph collapsed, {matrix_count} nodes -> axis-rings marker)"
                        )
                        return mesh

        if len(data) > 512:
            return None

        one_count = 0
        for off in range(0, max(len(data) - 4, 0), 4):
            try:
                v = struct.unpack_from("<I", data, off)[0]
            except struct.error:
                break
            if v == 0x3F800000:
                one_count += 1

        if one_count < 6:
            return None

        # Default to a 3-axis ring "gyro" wireframe placeholder for any small
        # skeleton resource that contains identity-matrix patterns (six 1.0f
        # values from the diagonals). Used for wave_a_skeleton and similar.
        mesh = self._build_axis_rings_mesh(filename, radius=0.025, segments=24, wireframe=True)
        if mesh is not None:
            self._log("Skeleton decode hit (axis-rings-wire fallback)")
            return mesh
        return None

    def _decode_phobj_cbox_wire(self, data: bytes, filename: str) -> Optional[Mesh]:
        """Decode physics-object (D543E771) Cbox wireframe geometry.

        Three layered strategies, tried in order:

        1. ``po1s`` body-chunk walker (preferred): reads each per-bone record's
           TMatrix and Cbox half-extents to emit oriented boxes at world-space
           bone positions.
        2. Raw ``Cbox`` scan: emits oriented or axis-aligned boxes directly
           from the Cbox payload (used by older phobj layouts without po1s
           framing).
        3. ``po1s`` anchor fallback: when no Cbox shapes are present, emits a
           small 3-axis cross marker at the body's translation so the
           destr-anchor still appears in the output.
        """
        if len(data) < 64:
            return None

        def _append_oriented_box(
            out_vertices: List[Tuple[float, float, float]],
            out_lines: List[Tuple[int, int]],
            half_sizes: Tuple[float, float, float],
            row0: Tuple[float, float, float],
            row1: Tuple[float, float, float],
            row2: Tuple[float, float, float],
            center: Tuple[float, float, float],
        ) -> None:
            hx, hy, hz = half_sizes
            corners = [
                (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
                (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
            ]
            base = len(out_vertices)
            for lx, ly, lz in corners:
                wx = center[0] + row0[0] * lx + row0[1] * ly + row0[2] * lz
                wy = center[1] + row1[0] * lx + row1[1] * ly + row1[2] * lz
                wz = center[2] + row2[0] * lx + row2[1] * ly + row2[2] * lz
                out_vertices.append((wx, wy, wz))
            out_lines.extend([
                (base + 0, base + 1), (base + 1, base + 2), (base + 2, base + 3), (base + 3, base + 0),
                (base + 4, base + 5), (base + 5, base + 6), (base + 6, base + 7), (base + 7, base + 4),
                (base + 0, base + 4), (base + 1, base + 5), (base + 2, base + 6), (base + 3, base + 7),
            ])

        # Generic po1s rigid-body decoder. The phobj file contains a sequence
        # of bone records introduced by ``<u32 chunk_size> body``. After
        # ``body`` follows ``<u32 namelen> <name padded to 4 bytes>`` and a
        # 12-float column-major TMatrix (col0=X, col1=Y, col2=Z axes;
        # col3=world translation). Later in the same record a ``Cbox`` chunk
        # carries a local TM followed by 3 floats of half-extents at
        # floats[12..14]. We use the bone TM's translation, otherwise every
        # Cbox stacks at the origin (the bug seen on abandoned_coal_cart's
        # 6 wheel/cart bones and abandoned_town fence_grape destr_phobj).
        if data[:4] == b"po1s" and len(data) >= 64:
            po_vertices: List[Tuple[float, float, float]] = []
            po_lines: List[Tuple[int, int]] = []
            po_body_names: Set[str] = set()
            # Locate every ``body`` tag whose preceding u32 looks like a
            # plausible chunk size.
            block_starts: List[int] = []
            sf = 8
            while True:
                hit = data.find(b"body", sf)
                if hit < 0:
                    break
                sf = hit + 4
                if hit < 4:
                    continue
                try:
                    chunk_size = struct.unpack_from("<I", data, hit - 4)[0]
                except struct.error:
                    continue
                if chunk_size < 32 or chunk_size > len(data):
                    continue
                if (hit - 4) + 4 + chunk_size > len(data):
                    continue
                block_starts.append(hit - 4)
            block_count = 0
            for bi, bstart in enumerate(block_starts):
                bend = block_starts[bi + 1] if bi + 1 < len(block_starts) else len(data)
                # Read namelen at bstart + 8, name from bstart + 12.
                if bstart + 12 > bend:
                    continue
                try:
                    namelen = struct.unpack_from("<I", data, bstart + 8)[0]
                except struct.error:
                    continue
                if namelen == 0 or namelen > 256:
                    continue
                pad = (-namelen) & 3  # bytes needed to align to 4
                try:
                    body_name = bytes(data[bstart + 12 : bstart + 12 + namelen]).decode("ascii", errors="ignore")
                except Exception:
                    body_name = ""
                tm_off = bstart + 12 + namelen + pad
                if tm_off + 48 > bend:
                    continue
                try:
                    tm = struct.unpack_from("<12f", data, tm_off)
                except struct.error:
                    continue
                if not all(math.isfinite(v) for v in tm):
                    continue
                col0 = (tm[0], tm[1], tm[2])
                col1 = (tm[3], tm[4], tm[5])
                col2 = (tm[6], tm[7], tm[8])
                tx, ty, tz = tm[9], tm[10], tm[11]
                # Sanity-check the rotation columns; reject non-orthonormal
                # matrices so we don't misinterpret a non-po1s payload.
                n0 = math.sqrt(sum(v * v for v in col0))
                n1 = math.sqrt(sum(v * v for v in col1))
                n2 = math.sqrt(sum(v * v for v in col2))
                if not (
                    abs(n0 - 1.0) < 0.05
                    and abs(n1 - 1.0) < 0.05
                    and abs(n2 - 1.0) < 0.05
                ):
                    continue
                if any(abs(v) > self.MAX_ABS_COORD for v in (tx, ty, tz)):
                    continue
                # Walk every Cbox within this body block. Each Cbox has its
                # own local 3x4 TM (12 floats) followed by 3 half-extents at
                # floats[12..14]. World pose = body_TM * cbox_local_TM, so
                # bones with multiple sub-shapes (e.g. lamp_post_metal_350
                # body 1 carries 2 Cboxes) all decode correctly.
                cb_search = bstart
                emitted_in_block = 0
                while True:
                    cbox_hit = data.find(b"Cbox", cb_search, bend)
                    if cbox_hit < 0:
                        break
                    cb_search = cbox_hit + 4
                    pay_off = cbox_hit + 4
                    if pay_off + 60 > bend:
                        continue
                    try:
                        cb_tm = struct.unpack_from("<12f", data, pay_off)
                        hx, hy, hz = struct.unpack_from("<3f", data, pay_off + 48)
                    except struct.error:
                        continue
                    if not all(math.isfinite(v) for v in cb_tm + (hx, hy, hz)):
                        continue
                    hx, hy, hz = abs(hx), abs(hy), abs(hz)
                    if min(hx, hy, hz) <= 1e-6 or max(hx, hy, hz) > self.MAX_ABS_COORD:
                        continue
                    # cbox local: column-major like body. Compose world TM.
                    cb_col0 = (cb_tm[0], cb_tm[1], cb_tm[2])
                    cb_col1 = (cb_tm[3], cb_tm[4], cb_tm[5])
                    cb_col2 = (cb_tm[6], cb_tm[7], cb_tm[8])
                    cb_t = (cb_tm[9], cb_tm[10], cb_tm[11])
                    # World rotation columns = body_R * cbox_R columns.
                    def _mul(c):
                        return (
                            col0[0] * c[0] + col1[0] * c[1] + col2[0] * c[2],
                            col0[1] * c[0] + col1[1] * c[1] + col2[1] * c[2],
                            col0[2] * c[0] + col1[2] * c[1] + col2[2] * c[2],
                        )
                    w_col0 = _mul(cb_col0)
                    w_col1 = _mul(cb_col1)
                    w_col2 = _mul(cb_col2)
                    w_t_off = _mul(cb_t)
                    wx_t = tx + w_t_off[0]
                    wy_t = ty + w_t_off[1]
                    wz_t = tz + w_t_off[2]
                    if any(abs(v) > self.MAX_ABS_COORD for v in (wx_t, wy_t, wz_t)):
                        continue
                    # rows of world rotation
                    row0 = (w_col0[0], w_col1[0], w_col2[0])
                    row1 = (w_col0[1], w_col1[1], w_col2[1])
                    row2 = (w_col0[2], w_col1[2], w_col2[2])
                    # po1s TMatrix is stored with Z-up storage but the rest
                    # of the OBJ (rendInst, collision, skeleton) uses Y-up,
                    # so swap world Y and Z when emitting box vertices.
                    # Verified empirically on building_4_floor_drain_snow_c
                    # (collision z[3.68,12.72] vs phobj y[3.14,13.79] before
                    # swap), abandoned_coal_cart, lamp_post_metal_350,
                    # metal_entrance_canopy_a_snow.
                    row1, row2 = row2, row1
                    wy_t, wz_t = wz_t, wy_t
                    _append_oriented_box(
                        po_vertices,
                        po_lines,
                        (hx, hy, hz),
                        row0, row1, row2,
                        (wx_t, wy_t, wz_t),
                    )
                    emitted_in_block += 1
                if emitted_in_block > 0:
                    block_count += emitted_in_block
                    if body_name:
                        # Strip Blender-style duplicate suffixes (".001",
                        # ".002", ...) so multi-instance bodies of the same
                        # base name (e.g. european_waymark_X_destr +
                        # _destr.001/_destr.002) collapse to a single
                        # distinct body. This is what tells lamp_post-style
                        # multi-piece destruction phobjs apart from
                        # waymark-style single-piece phobjs whose extra
                        # bodies are decorative duplicates.
                        base_name = re.sub(r"\.\d{1,3}$", "", body_name)
                        po_body_names.add(base_name)
            if block_count >= 1:
                mesh = self._build_mesh(filename, po_vertices, faces=[], lines=po_lines)
                if mesh is not None:
                    # Stash distinct body-name count so parse_directory can
                    # decide whether the phobj represents many independent
                    # destruction pieces (eligible for derived
                    # _destr_collision clone) versus a single body wrapped
                    # in multiple Cboxes (no clone).
                    mesh["_phobj_body_count"] = len(po_body_names)
                    self._log(
                        f"Phobj decode hit (po1s-body-chunks, {block_count} boxes, "
                        f"{len(po_body_names)} distinct bodies, "
                        f"{mesh['vertex_count']} verts, {mesh.get('line_count', 0)} lines)"
                    )
                    return mesh

        boxes: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = []
        oriented: List[Tuple[
            Tuple[float, float, float],
            Tuple[float, float, float],
            Tuple[float, float, float],
            Tuple[float, float, float],
        ]] = []
        search_from = 0
        while True:
            hit = data.find(b"Cbox", search_from)
            if hit < 0:
                break
            search_from = hit + 4

            if hit + 8 + (14 * 4) > len(data):
                continue
            try:
                vals = struct.unpack_from("<14f", data, hit + 8)
            except struct.error:
                continue

            half_x = abs(float(vals[11]))
            half_y = abs(float(vals[12]))
            half_z = abs(float(vals[13]))
            if not all(math.isfinite(v) for v in (half_x, half_y, half_z)):
                continue
            if min(half_x, half_y, half_z) <= 1e-6:
                continue
            if max(half_x, half_y, half_z) > self.MAX_ABS_COORD:
                continue

            # vals[3..8] = upper 2 rows of a 3x3 rotation about the world Y axis;
            # vals[9..10] = box center on the XZ plane; Y center is implicit at 0.
            r00, r01, r02 = vals[3], vals[4], vals[5]
            r10, r11, r12 = vals[6], vals[7], vals[8]
            cx, cz = float(vals[9]), float(vals[10])
            row_norm_ok = (
                all(math.isfinite(v) for v in (r00, r01, r02, r10, r11, r12, cx, cz))
                and abs(r00 * r00 + r01 * r01 + r02 * r02 - 1.0) < 0.05
                and abs(r10 * r10 + r11 * r11 + r12 * r12 - 1.0) < 0.05
            )
            if row_norm_ok and (abs(cx) > 1e-6 or abs(cz) > 1e-6 or abs(r01) > 1e-6 or abs(r10) > 1e-6):
                # Build 3rd row by cross product so the basis stays right-handed.
                r20 = r01 * r12 - r02 * r11
                r21 = r02 * r10 - r00 * r12
                r22 = r00 * r11 - r01 * r10
                oriented.append(
                    (
                        (half_x, half_y, half_z),
                        (r00, r01, r02),
                        (r10, r11, r12),
                        (cx, 0.0, cz),
                    )
                )
                continue

            boxes.append(((-half_x, -half_y, -half_z), (half_x, half_y, half_z)))

        if not boxes and not oriented:
            # Fallback: `po1s` rigid-body block without an explicit Cbox shape.
            # The 224-byte phobj record holds a 3x4 column-major transform at
            # offset 0x30 followed by mass/inertia. Emit a small 3-axis cross
            # marker at the body's translation so the destr-anchor still shows
            # in the output. Detected on abandoned_town town_fence_grape_*_destr_phobj.
            if data[:4] == b"po1s" and len(data) >= 0x60:
                try:
                    tm = struct.unpack_from("<12f", data, 0x30)
                except struct.error:
                    tm = None
                if tm is not None and all(math.isfinite(v) for v in tm):
                    # Columns 0..2 = rotation basis, column 3 = translation.
                    col0 = (tm[0], tm[1], tm[2])
                    col1 = (tm[3], tm[4], tm[5])
                    col2 = (tm[6], tm[7], tm[8])
                    tx, ty, tz = tm[9], tm[10], tm[11]
                    n0 = math.sqrt(sum(v * v for v in col0))
                    n1 = math.sqrt(sum(v * v for v in col1))
                    n2 = math.sqrt(sum(v * v for v in col2))
                    rot_ok = (
                        abs(n0 - 1.0) < 0.05
                        and abs(n1 - 1.0) < 0.05
                        and abs(n2 - 1.0) < 0.05
                    )
                    pos_ok = (
                        all(abs(v) < self.MAX_ABS_COORD for v in (tx, ty, tz))
                        and (abs(tx) > 1e-4 or abs(ty) > 1e-4 or abs(tz) > 1e-4)
                    )
                    if rot_ok and pos_ok:
                        size = 0.1  # 10 cm cross
                        verts = [
                            (tx - size, ty, tz), (tx + size, ty, tz),
                            (tx, ty - size, tz), (tx, ty + size, tz),
                            (tx, ty, tz - size), (tx, ty, tz + size),
                        ]
                        lines = [(0, 1), (2, 3), (4, 5)]
                        mesh = self._build_mesh(filename, verts, faces=[], lines=lines)
                        if mesh is not None:
                            self._log(
                                f"Phobj decode hit (po1s-anchor at "
                                f"{tx:.2f},{ty:.2f},{tz:.2f})"
                            )
                            return mesh
            return None

        # Deduplicate identical boxes. The "Cbox" byte sequence appears in
        # both the parameter section and embedded name/type tables of phobj
        # resources, so a raw scan picks the same box up multiple times.
        # Key on full (min, max) so distinct positions with same extents are kept.
        seen_box: Set[Tuple[float, ...]] = set()
        unique_boxes: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = []
        for mn, mx in boxes:
            key = (
                round(mn[0], 5), round(mn[1], 5), round(mn[2], 5),
                round(mx[0], 5), round(mx[1], 5), round(mx[2], 5),
            )
            if key in seen_box:
                continue
            seen_box.add(key)
            unique_boxes.append((mn, mx))
        boxes = unique_boxes

        seen_oriented: Set[Tuple[float, ...]] = set()
        unique_oriented: List[Tuple[
            Tuple[float, float, float],
            Tuple[float, float, float],
            Tuple[float, float, float],
            Tuple[float, float, float],
        ]] = []
        for half, r0, r1, ctr in oriented:
            key = tuple(round(v, 5) for v in (*half, *r0, *r1, *ctr))
            if key in seen_oriented:
                continue
            seen_oriented.add(key)
            unique_oriented.append((half, r0, r1, ctr))
        oriented = unique_oriented

        # Emit oriented and axis-aligned boxes together. They are not mutually
        # exclusive: a phobj can mix Cboxes with non-identity rotation rows
        # (oriented) and ones with identity-or-zero rotation (boxes/AABB).
        vertices: List[Tuple[float, float, float]] = []
        lines: List[Tuple[int, int]] = []
        for half, r0, r1, ctr in oriented:
            hx, hy, hz = half
            r20 = r0[1] * r1[2] - r0[2] * r1[1]
            r21 = r0[2] * r1[0] - r0[0] * r1[2]
            r22 = r0[0] * r1[1] - r0[1] * r1[0]
            base = len(vertices)
            for lx, ly, lz in (
                (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
                (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),
            ):
                wx = ctr[0] + r0[0] * lx + r0[1] * ly + r0[2] * lz
                wy = ctr[1] + r1[0] * lx + r1[1] * ly + r1[2] * lz
                wz = ctr[2] + r20 * lx + r21 * ly + r22 * lz
                vertices.append((wx, wy, wz))
            lines.extend([
                (base + 0, base + 1), (base + 1, base + 2), (base + 2, base + 3), (base + 3, base + 0),
                (base + 4, base + 5), (base + 5, base + 6), (base + 6, base + 7), (base + 7, base + 4),
                (base + 0, base + 4), (base + 1, base + 5), (base + 2, base + 6), (base + 3, base + 7),
            ])
        for mn, mx in boxes:
            base = len(vertices)
            x0, y0, z0 = mn
            x1, y1, z1 = mx
            vertices.extend([
                (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
            ])
            lines.extend([
                (base + 0, base + 1), (base + 1, base + 2), (base + 2, base + 3), (base + 3, base + 0),
                (base + 4, base + 5), (base + 5, base + 6), (base + 6, base + 7), (base + 7, base + 4),
                (base + 0, base + 4), (base + 1, base + 5), (base + 2, base + 6), (base + 3, base + 7),
            ])

        if not vertices:
            return None

        mesh = self._build_mesh(filename, vertices, faces=[], lines=lines)
        if mesh is not None:
            self._log(
                f"Phobj decode hit (cbox combined, oriented={len(oriented)}, aabb={len(boxes)}, "
                f"{mesh['vertex_count']} verts, {mesh.get('line_count', 0)} lines)"
            )
        return mesh

    def _normalize_oversized_collision_mesh(self, filename: str, mesh: Mesh) -> Mesh:
        """Localize vertex positions on collision meshes that decode in
        oversized world units (>=1000) by uniformly scaling by 0.01.

        Generic post-process: gated only on the bounding-box extent so it
        applies uniformly to any collision mesh, not specific filenames.
        """
        verts = mesh.get("vertices")
        faces = mesh.get("faces")
        lines = mesh.get("lines")
        if not isinstance(verts, list) or len(verts) < 4:
            return mesh
        if not isinstance(faces, list):
            faces = []
        if not isinstance(lines, list):
            lines = []

        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        extents = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        max_extent = max(extents)

        # Keep normal-sized meshes untouched. Oversized collisions decode in
        # local units (~thousands) and need scene-friendly scale.
        if max_extent < 1000.0:
            return mesh

        scale = 0.01
        norm_verts = [(x * scale, y * scale, z * scale) for x, y, z in verts]

        normalized: Mesh = dict(mesh)
        normalized["vertices"] = norm_verts
        normalized["vertex_count"] = len(norm_verts)
        normalized["face_count"] = len(faces)
        normalized["line_count"] = len(lines)
        if self.verbose:
            self._log(
                f"Collision normalize hit (scale={scale:.4f}, extent={max_extent:.2f})"
            )
        return normalized

    def _mesh_shape_score(self, mesh: Mesh) -> float:
        vertices = mesh.get("vertices")
        if not isinstance(vertices, list) or len(vertices) < 9:
            return -1.0

        xs = [v[0] for v in vertices]
        ys = [v[1] for v in vertices]
        zs = [v[2] for v in vertices]
        extents = sorted(
            [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)],
            reverse=True,
        )
        if extents[0] <= 0.0:
            return -1.0

        planar_ratio = extents[1] / extents[0]
        volume_ratio = extents[2] / extents[0]
        face_count = int(mesh.get("face_count", 0))
        vert_count = int(mesh.get("vertex_count", len(vertices)))
        face_density = (face_count / float(max(1, vert_count)))

        # Prefer candidates that are not axis-collapsed and have reasonable face density.
        return planar_ratio * 1.5 + volume_ratio * 0.5 + min(face_density, 2.0) * 0.2

    def _autoscale_fp16_vertices(
        self,
        vertices: Sequence[Tuple[float, float, float]],
    ) -> Tuple[List[Tuple[float, float, float]], float]:
        verts = list(vertices)
        if not verts:
            return verts, 1.0

        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
        if extent <= 0.0:
            return verts, 1.0

        # Many fp16 candidates decode to very small local units; normalize to a visible range.
        if extent >= 20.0:
            return verts, 1.0

        target_extent = 20.0
        scale = target_extent / extent
        if scale < 1.5:
            return verts, 1.0
        scale = min(scale, 2000.0)
        scaled = [(x * scale, y * scale, z * scale) for x, y, z in verts]
        return scaled, scale

    def _parse_vertex_block(self, blob: bytes, filename: str, vertex_count: Optional[int], stride: int, pos_offset: int = 0) -> Optional[Mesh]:
        if stride < 12:
            return None
        if vertex_count is None:
            max_count = min(len(blob) // stride, 600000)
        else:
            max_count = min(vertex_count, len(blob) // stride)
        if max_count < 9:
            return None

        vertices: List[Tuple[float, float, float]] = []
        in_real_region = False
        for i in range(max_count):
            off = i * stride + pos_offset
            if off + 12 > len(blob):
                break
            try:
                x, y, z = struct.unpack_from("<fff", blob, off)
            except struct.error:
                break
            # Stop when we run past the vertex region into trailing index/aux data,
            # which reinterpreted as float32 typically produces non-finite or
            # implausibly large magnitudes.
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                if in_real_region:
                    break
                continue
            if max(abs(x), abs(y), abs(z)) > self.MAX_ABS_COORD:
                if in_real_region:
                    break
                continue
            # Skip leading "preamble" vertices: header bytes reinterpreted as
            # float32 are typically all zero or subnormal. Only start accepting
            # once we see a vertex with a reasonable magnitude on at least one
            # axis (>= 1e-4). This avoids decoding garbage from B4/RendInst
            # descriptor headers as fake mesh vertices.
            mag = max(abs(x), abs(y), abs(z))
            if not in_real_region:
                if mag < 1e-4:
                    continue
                in_real_region = True
            elif mag < 1e-10:
                # We have entered the real vertex region but now hit values
                # that look like trailing index/aux data reinterpreted as
                # subnormal floats — stop.
                break
            vertices.append((x, y, z))

        if len(vertices) < 9:
            return None
        return self._build_mesh(filename, vertices)

    def _parse_fp16_stream(
        self,
        blob: bytes,
        filename: str,
        start: int,
        stride: int,
        xyz_offset: int,
        vertex_count: Optional[int],
        infer_faces: bool = True,
    ) -> Optional[Mesh]:
        if stride < 8 or xyz_offset < 0:
            return None
        if vertex_count is None:
            max_count = min((len(blob) - start) // stride, 4000)
        else:
            max_count = min(vertex_count, (len(blob) - start) // stride)
        if max_count < 9:
            return None

        vertices: List[Tuple[float, float, float]] = []
        for i in range(max_count):
            off = start + i * stride + xyz_offset
            if off + 6 > len(blob):
                break
            try:
                x = struct.unpack_from("<e", blob, off)[0]
                y = struct.unpack_from("<e", blob, off + 2)[0]
                z = struct.unpack_from("<e", blob, off + 4)[0]
            except struct.error:
                break

            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                break
            if max(abs(x), abs(y), abs(z)) > 10000.0:
                break
            vertices.append((float(x), float(y), float(z)))

        if len(vertices) < 9:
            return None

        if self.auto_scale_fp16:
            vertices, applied_scale = self._autoscale_fp16_vertices(vertices)
            if applied_scale > 1.0 and self.verbose:
                self._log(f"Applied fp16 auto-scale x{applied_scale:.2f}")

        faces: Optional[List[Tuple[int, int, int]]] = None
        if infer_faces:
            if len(vertices) <= 2000:
                faces = self._infer_faces_u16(blob, vertices, start + len(vertices) * stride)
                if faces and len(faces) < int(len(vertices) * 0.9):
                    if self.verbose:
                        self._log(
                            f"Inferred index stream too sparse ({len(faces)} faces for {len(vertices)} verts), "
                            "using spatial fallback"
                        )
                    faces = None
            elif self.verbose:
                self._log(
                    f"Skipping expensive index inference for large fp16 candidate ({len(vertices)} verts)"
                )
        return self._build_mesh(filename, vertices, faces=faces)

    def _score_faces(self, vertices: Sequence[Tuple[float, float, float]], faces: Sequence[Tuple[int, int, int]]) -> float:
        if not faces:
            return 0.0
        nondeg = 0
        sampled = 0
        max_samples = min(len(faces), 1500)
        for i in range(max_samples):
            a, b, c = faces[i]
            if a == b or b == c or a == c:
                continue
            ax, ay, az = vertices[a]
            bx, by, bz = vertices[b]
            cx, cy, cz = vertices[c]
            abx, aby, abz = (bx - ax), (by - ay), (bz - az)
            acx, acy, acz = (cx - ax), (cy - ay), (cz - az)
            cxp_x = aby * acz - abz * acy
            cxp_y = abz * acx - abx * acz
            cxp_z = abx * acy - aby * acx
            area2 = math.sqrt(cxp_x * cxp_x + cxp_y * cxp_y + cxp_z * cxp_z)
            sampled += 1
            if area2 > 1e-8 and math.isfinite(area2):
                nondeg += 1
        if sampled == 0:
            return 0.0
        return nondeg / float(sampled)

    def _face_edge_quality(
        self,
        vertices: Sequence[Tuple[float, float, float]],
        faces: Sequence[Tuple[int, int, int]],
    ) -> float:
        if not faces:
            return 0.0

        edge_lengths: List[float] = []
        max_samples = min(len(faces), 1200)
        for i in range(max_samples):
            a, b, c = faces[i]
            tri = (vertices[a], vertices[b], vertices[c])
            for u, v in ((0, 1), (1, 2), (2, 0)):
                dx = tri[u][0] - tri[v][0]
                dy = tri[u][1] - tri[v][1]
                dz = tri[u][2] - tri[v][2]
                ln = math.sqrt(dx * dx + dy * dy + dz * dz)
                if math.isfinite(ln) and ln > 1e-10:
                    edge_lengths.append(ln)

        if len(edge_lengths) < 16:
            return 0.0

        edge_lengths.sort()
        med = edge_lengths[len(edge_lengths) // 2]
        p95 = edge_lengths[min(len(edge_lengths) - 1, int(len(edge_lengths) * 0.95))]
        if med <= 1e-10:
            return 0.0

        ratio = p95 / med
        if ratio <= 4.0:
            return 1.0
        if ratio <= 8.0:
            return 0.75
        if ratio <= 16.0:
            return 0.45
        return 0.1

    def _index_quality(
        self,
        vcount: int,
        faces: Sequence[Tuple[int, int, int]],
    ) -> Tuple[float, float, float]:
        if not faces or vcount <= 0:
            return (0.0, 0.0, 1.0)

        sampled_faces = faces[: min(len(faces), 4000)]
        used = set()
        face_counts: Dict[Tuple[int, int, int], int] = {}

        for a, b, c in sampled_faces:
            used.add(a)
            used.add(b)
            used.add(c)
            key = tuple(sorted((a, b, c)))
            face_counts[key] = face_counts.get(key, 0) + 1

        coverage = len(used) / float(vcount)
        unique_face_ratio = len(face_counts) / float(len(sampled_faces))
        repeat_ratio = max(face_counts.values()) / float(len(sampled_faces))
        return (coverage, unique_face_ratio, repeat_ratio)

    def _infer_faces_u16(
        self,
        blob: bytes,
        vertices: Sequence[Tuple[float, float, float]],
        scan_start_hint: int,
    ) -> Optional[List[Tuple[int, int, int]]]:
        vcount = len(vertices)
        if vcount < 3:
            return None

        best_score = 0.0
        best_rank = 0.0
        best_faces: Optional[List[Tuple[int, int, int]]] = None
        search_end = min(len(blob), max(scan_start_hint + 2 * 1024 * 1024, 4 * 1024 * 1024))
        min_faces = max(30, min(400, vcount // 2))

        # Candidate A: u32-prefixed index count followed by u16 indices.
        for off in range(0, max(search_end - 8, 0), 4):
            try:
                nidx = struct.unpack_from("<I", blob, off)[0]
            except struct.error:
                continue
            if nidx < 9 or nidx > 2_000_000 or (nidx % 3) != 0:
                continue
            end = off + 4 + nidx * 2
            if end > len(blob):
                continue
            idx = list(struct.unpack_from(f"<{nidx}H", blob, off + 4))
            if max(idx, default=vcount) >= vcount:
                continue
            faces = [(idx[i], idx[i + 1], idx[i + 2]) for i in range(0, len(idx), 3)]
            score = self._score_faces(vertices, faces)
            coverage, unique_face_ratio, repeat_ratio = self._index_quality(vcount, faces)
            rank = (
                score
                + min(len(faces), 4000) / 4000.0 * 0.30
                + min(coverage, 1.0) * 0.20
                + min(unique_face_ratio, 1.0) * 0.10
            )
            if (
                score >= 0.45
                and len(faces) >= min_faces
                and coverage >= 0.20
                and unique_face_ratio >= 0.50
                and repeat_ratio <= 0.20
                and rank > best_rank
            ):
                best_score = score
                best_rank = rank
                best_faces = faces

        # Candidate B: raw u16 stream with no prefix; require long in-range run.
        # Focus around vertex payload tail first, then global scan at coarse step.
        starts: List[int] = []
        around = max(0, scan_start_hint - 0x20000)
        while around < min(scan_start_hint + 0x40000, len(blob) - 6):
            starts.append(around)
            around += 2
        coarse = 0
        # Keep global scan bounded to avoid pathological runtimes on large payloads.
        coarse_step = 128
        while coarse < max(search_end - 6, 0):
            starts.append(coarse)
            coarse += coarse_step

        max_starts = 12000
        if len(starts) > max_starts:
            # Prioritize the local tail window around scan_start_hint first.
            starts = starts[:max_starts]

        seen = set()
        for off in starts:
            if off in seen:
                continue
            seen.add(off)

            run = 0
            pos = off
            while pos + 2 <= len(blob):
                try:
                    v = struct.unpack_from("<H", blob, pos)[0]
                except struct.error:
                    break
                if v >= vcount:
                    break
                run += 1
                pos += 2

            if run < 24:
                continue
            run -= run % 3
            if run < 24:
                continue

            idx = list(struct.unpack_from(f"<{run}H", blob, off))
            faces = [(idx[i], idx[i + 1], idx[i + 2]) for i in range(0, len(idx), 3)]
            score = self._score_faces(vertices, faces)
            coverage, unique_face_ratio, repeat_ratio = self._index_quality(vcount, faces)
            rank = (
                score
                + min(len(faces), 4000) / 4000.0 * 0.30
                + min(coverage, 1.0) * 0.20
                + min(unique_face_ratio, 1.0) * 0.10
            )
            if (
                score >= 0.45
                and len(faces) >= min_faces
                and coverage >= 0.20
                and unique_face_ratio >= 0.50
                and repeat_ratio <= 0.20
                and rank > best_rank
            ):
                best_score = score
                best_rank = rank
                best_faces = faces

        if best_faces and self.verbose:
            self._log(
                f"Inferred u16 index stream: faces={len(best_faces)} "
                f"score={best_score:.3f} rank={best_rank:.3f}"
            )
        return best_faces

    def _find_fp16_stride16_candidate(self, blob: bytes, hinted_count: Optional[int]) -> Optional[Tuple[int, int]]:
        # We scan for dense runs of finite fp16 XYZ triplets inside 16-byte records.
        if len(blob) < 4096:
            return None

        sample = 192 if hinted_count is None else min(max(hinted_count, 48), 192)
        limit = len(blob) - (sample * 16 + 8)
        if limit <= 0:
            return None

        best: Optional[Tuple[float, int, int]] = None
        for start in range(0, min(limit, 2 * 1024 * 1024), 16):
            for xyz_offset in (0, 2):
                ok = 0
                min_x = float("inf")
                max_x = float("-inf")
                min_y = float("inf")
                max_y = float("-inf")
                min_z = float("inf")
                max_z = float("-inf")

                for i in range(sample):
                    off = start + i * 16 + xyz_offset
                    x = struct.unpack_from("<e", blob, off)[0]
                    y = struct.unpack_from("<e", blob, off + 2)[0]
                    z = struct.unpack_from("<e", blob, off + 4)[0]
                    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                        continue
                    if max(abs(x), abs(y), abs(z)) > 2000.0:
                        continue
                    ok += 1
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
                    if z < min_z:
                        min_z = z
                    if z > max_z:
                        max_z = z

                if ok < int(sample * 0.9):
                    continue

                extent = max(max_x - min_x, max_y - min_y, max_z - min_z)
                if extent < 0.02:
                    continue

                score = ok / float(sample) + min(extent, 100.0) * 1e-4
                if best is None or score > best[0]:
                    best = (score, start, xyz_offset)

        if not best:
            return None
        return (best[1], best[2])

    def _extract_b4_header_candidates(self, dec: bytes) -> List[int]:
        """Extract deterministic offset candidates from decompressed B4 header tables."""
        if len(dec) < 128:
            return []

        words: List[int] = []
        for off in range(0, min(128, len(dec)), 4):
            words.append(struct.unpack_from("<I", dec, off)[0])

        candidates: List[int] = []
        seen: Dict[int, bool] = {}
        for v in words:
            if v <= 0 or v >= len(dec) - 16:
                continue
            # Prefer aligned offsets that plausibly point to payload sections.
            if (v % 16) != 0:
                continue
            if v < 512:
                continue
            if v not in seen:
                seen[v] = True
                candidates.append(v)

        candidates.sort()
        return candidates

    def _find_fp16_candidates_from_b4_tables(
        self,
        dec: bytes,
        hinted_count: Optional[int],
    ) -> List[Tuple[int, int]]:
        """Derive FP16 stream starts from deterministic B4 table offsets first."""
        seeds = self._extract_b4_header_candidates(dec)
        if not seeds:
            return []

        out: List[Tuple[int, int]] = []
        seen: Dict[Tuple[int, int], bool] = {}
        max_seed_scans = 8
        for seed in seeds[:max_seed_scans]:
            window_start = max(0, seed - 0x2000)
            window_end = min(len(dec), window_start + 2 * 1024 * 1024)
            if window_end - window_start < 4096:
                continue

            local = dec[window_start:window_end]
            hit = self._find_fp16_stride16_candidate(local, hinted_count)
            if not hit:
                continue

            start, xyz_offset = hit
            abs_start = window_start + start
            key = (abs_start, xyz_offset)
            if key in seen:
                continue
            seen[key] = True
            out.append(key)

        # Prefer earliest deterministic hits for stable decode order.
        out.sort(key=lambda t: t[0])
        return out

    def _try_standard_block_decompress(self, payload: bytes, flags: int) -> List[bytes]:
        out: List[bytes] = []
        if flags == 2 and len(payload) >= 8:
            raw_size = struct.unpack_from("<I", payload, 0)[0]
            comp = payload[4:]
            dec = self.oodle.decompress(comp, raw_size)
            if dec:
                out.append(dec)
        elif flags == 1:
            dec = self._zstd_decompress(payload)
            if dec:
                out.append(dec)
        elif flags == 0:
            try:
                out.append(lzma.decompress(payload))
            except Exception:
                pass
        elif flags == 3:
            out.append(payload)
        return out

    def _decode_b4b7d9c4(self, data: bytes, filename: str) -> Optional[Mesh]:
        """Class-specific probing for RendInst-like resources.

        Uses conservative strategies inspired by Dagor lengthflags framing.
        """
        if len(data) < 16:
            return None

        if len(data) <= 256:
            tiny_mesh = self._decode_tiny_b4_parameter_block(data, filename)
            if tiny_mesh is not None:
                return tiny_mesh

            plausible_framing = False
            for base in range(0, max(min(len(data) - 4, 64), 0), 4):
                lf = struct.unpack_from("<I", data, base)[0]
                flags = (lf >> 30) & 0x3
                blen = lf & 0x3FFFFFFF
                if flags in (1, 2, 3) and 8 <= blen <= (len(data) - base - 4):
                    plausible_framing = True
                    break
            zero_ratio = data.count(b"\x00") / float(len(data))
            if not plausible_framing and zero_ratio >= 0.35:
                self.rejections.append(
                    f"{filename}: tiny B4 payload with no compressed framing; likely non-mesh parameter block"
                )
                return None

        head_vcount = struct.unpack_from("<I", data, 0)[0]
        self._log(f"B4 probe {filename}: vcount_hint={head_vcount}")

        # Strategy "decal": uncompressed RendInst decal record. Section descriptor
        # at +0x60 holds (stride=24, count=N, ?, ?) and a type-3 marker at +0x70
        # immediately precedes a stride-24 vertex region at +0x7c. Detected on
        # War Thunder deferred_decal_* assets (no Oodle framing; flags=0). The
        # decal is rendered as a flat quad — extract just the four real corner
        # vertices and emit a 2-triangle quad mesh.
        if (
            len(data) >= 0x7c + 24
            and struct.unpack_from("<I", data, 0x60)[0] == 24
            and struct.unpack_from("<I", data, 0x70)[0] == 3
        ):
            decal_count = struct.unpack_from("<I", data, 0x64)[0]
            if 3 <= decal_count <= 4096 and 0x7c + decal_count * 24 <= len(data):
                corners: List[Tuple[float, float, float]] = []
                for i in range(decal_count):
                    off = 0x7c + i * 24
                    x, y, z = struct.unpack_from("<fff", data, off)
                    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                        continue
                    if max(abs(x), abs(y), abs(z)) > self.MAX_ABS_COORD:
                        continue
                    if max(abs(x), abs(y), abs(z)) < 1e-4:
                        continue
                    corners.append((x, y, z))
                    if len(corners) >= 4:
                        break
                if len(corners) >= 3:
                    xs = [c[0] for c in corners]
                    ys = [c[1] for c in corners]
                    zs = [c[2] for c in corners]
                    mins = (min(xs), min(ys), min(zs))
                    maxs = (max(xs), max(ys), max(zs))
                    extents = [maxs[i] - mins[i] for i in range(3)]
                    if max(extents) > 1e-4:
                        # Build a flat quad spanning the two largest-extent axes,
                        # placed at the midpoint of the smallest-extent axis.
                        mesh = self._build_grid_plane_mesh(filename, mins, maxs)
                        if mesh:
                            self._log(
                                f"B4 decode hit (decal-quad, "
                                f"size={extents[0]:.2f}x{extents[1]:.2f}x{extents[2]:.2f})"
                            )
                            return mesh

        # Strategy 0: confirmed layout for cockpit B4B7D9C4 blobs.
        # [20..23] inner Dagor lengthflags (flags=2 Oodle, flags=3 also Oodle, bsize includes raw_size u32)
        # [24..27] raw_size, [28..] compressed payload.
        if len(data) >= 28:
            inner_lf = struct.unpack_from("<I", data, 20)[0]
            inner_flags = (inner_lf >> 30) & 0x3
            inner_bsize = inner_lf & 0x3FFFFFFF
            raw_size = struct.unpack_from("<I", data, 24)[0]
            comp_size = max(0, inner_bsize - 4)
            cstart = 28
            cend = cstart + comp_size
            if inner_flags in (2, 3) and comp_size > 0 and cend <= len(data) and 256 <= raw_size <= 256 * 1024 * 1024:
                dec = self.oodle.decompress(data[cstart:cend], raw_size)
                if dec:
                    self._log(
                        f"B4 strategy0 decompressed: comp={comp_size} raw={len(dec)} "
                        f"(inner_flags={inner_flags})"
                    )

                    # Strategy 0a: B4 visual mesh with two-section descriptor table.
                    # Header signature at +0x58/+0x5C/+0x78/+0x7C contains [stride=32, type=3]
                    # markers; vertex region begins at +0x84 with stride 24 (XYZ float + 12B
                    # packed normal/UV trailing). Detected on debris_roof_sheets variants.
                    if (
                        len(dec) >= 0x84 + 24
                        and struct.unpack_from("<I", dec, 0x58)[0] == 32
                        and struct.unpack_from("<I", dec, 0x5C)[0] == 3
                        and struct.unpack_from("<I", dec, 0x78)[0] == 32
                        and struct.unpack_from("<I", dec, 0x7C)[0] == 3
                    ):
                        mesh = self._parse_vertex_block(dec[0x84:], filename, None, 24)
                        if mesh:
                            self._log(
                                f"B4 decode hit (strategy0a, stride=24 @ 0x84, "
                                f"verts={mesh['vertex_count']})"
                            )
                            return mesh

                    for stride in (12, 16, 20, 24, 28, 32):
                        mesh = self._parse_vertex_block(
                            dec,
                            filename,
                            head_vcount if 9 <= head_vcount < 2000000 else None,
                            stride,
                        )
                        if mesh:
                            self._log(f"B4 decode hit (strategy0, stride={stride})")
                            return mesh

                    # Strategy 0b: scan with explicit starts. Some B4 visual meshes
                    # place the vertex region at a fixed header offset (e.g. 0x80)
                    # that is not a multiple of common strides — strategy 0 then
                    # never aligns onto it. Detected on War Thunder railing destr
                    # variants where stride=28 starts at 0x80.
                    best_mesh: Optional[Mesh] = None
                    best_score = -1.0
                    best_start = 0
                    best_stride = 0
                    for start in (0x40, 0x60, 0x70, 0x80, 0x84, 0xa0):
                        if start >= len(dec):
                            continue
                        for stride in (12, 16, 20, 24, 28, 32):
                            mesh = self._parse_vertex_block(
                                dec[start:], filename, None, stride
                            )
                            if not mesh:
                                continue
                            score = self._mesh_shape_score(mesh)
                            if score > best_score:
                                best_score = score
                                best_mesh = mesh
                                best_start = start
                                best_stride = stride
                    if best_mesh is not None:
                        self._log(
                            f"B4 decode hit (strategy0b, start={best_start:#x}, "
                            f"stride={best_stride}, verts={best_mesh['vertex_count']})"
                        )
                        return best_mesh

                    hint = head_vcount if 9 <= head_vcount < 2000000 else None

                    # Deterministic pass: derive stream starts from B4 header tables.
                    table_candidates = self._find_fp16_candidates_from_b4_tables(dec, hint)
                    best_table_key: Optional[Tuple[int, int, Optional[int]]] = None
                    best_table_score = -1.0
                    for start, xyz_offset in table_candidates[:8]:
                        count_hints = [hint] if hint is not None else []
                        if hint is not None:
                            count_hints.append(None)
                        if not count_hints:
                            count_hints = [None]
                        for count_hint in count_hints:
                            mesh = self._parse_fp16_stream(
                                dec,
                                filename,
                                start,
                                16,
                                xyz_offset,
                                count_hint,
                                infer_faces=False,
                            )
                            if not mesh:
                                continue
                            score = self._mesh_shape_score(mesh)
                            if self.verbose:
                                self._log(
                                    f"B4 table candidate start={start} xyz_offset={xyz_offset} "
                                    f"hint={count_hint if count_hint is not None else 'none'} "
                                    f"shape_score={score:.4f} verts={mesh['vertex_count']} faces={mesh['face_count']}"
                                )
                            if score > best_table_score:
                                best_table_score = score
                                best_table_key = (start, xyz_offset, count_hint)
                        if best_table_score >= 1.0:
                            # Good enough candidate; stop early to keep runtime bounded.
                            break

                    if best_table_key is not None:
                        bstart, bxyz, bhint = best_table_key
                        best_table_mesh = self._parse_fp16_stream(
                            dec,
                            filename,
                            bstart,
                            16,
                            bxyz,
                            bhint,
                            infer_faces=True,
                        )
                        if best_table_mesh is not None:
                            self._log(
                                f"B4 decode hit (strategy0-table-best, start={bstart}, "
                                f"xyz_offset={bxyz}, score={best_table_score:.4f})"
                            )
                            return best_table_mesh

                    # Heuristic fallback when table-derived candidates do not validate.
                    fp16_candidate = self._find_fp16_stride16_candidate(dec, hint)
                    if fp16_candidate:
                        start, xyz_offset = fp16_candidate
                        mesh = self._parse_fp16_stream(dec, filename, start, 16, xyz_offset, hint)
                        if mesh:
                            self._log(
                                f"B4 decode hit (strategy0-fp16, start={start}, "
                                f"stride=16, xyz_offset={xyz_offset})"
                            )
                            return mesh

                    # Strategy 0c: brute-force fp16 stride-16 scan. The hinted
                    # vcount can be larger than the actual count (LOD chains
                    # report combined totals), which makes the heuristic
                    # candidate finder reject the real region. Walk every
                    # 16-byte boundary, parse with hint=None, and pick the
                    # mesh with the highest shape score and a non-trivial
                    # vertex count. Detected on abandoned_town town_fence_grape_b.
                    if len(dec) >= 4096:
                        brute_best: Optional[Mesh] = None
                        brute_best_score = -1.0
                        brute_best_start = 0
                        brute_best_xoff = 0
                        scan_limit = min(len(dec) - 16 * 50, 4 * 1024 * 1024)
                        for start in range(0, scan_limit, 16):
                            for xyz_offset in (0, 2):
                                mesh = self._parse_fp16_stream(
                                    dec, filename, start, 16, xyz_offset, None,
                                    infer_faces=False,
                                )
                                if not mesh:
                                    continue
                                vc = int(mesh.get("vertex_count", 0))
                                if vc < 32:
                                    continue
                                score = self._mesh_shape_score(mesh)
                                if score > brute_best_score:
                                    brute_best_score = score
                                    brute_best = mesh
                                    brute_best_start = start
                                    brute_best_xoff = xyz_offset
                        if brute_best is not None:
                            final = self._parse_fp16_stream(
                                dec, filename, brute_best_start, 16,
                                brute_best_xoff, None, infer_faces=True,
                            )
                            if final is not None:
                                self._log(
                                    f"B4 decode hit (strategy0c-brute, start={brute_best_start}, "
                                    f"xyz_offset={brute_best_xoff}, score={brute_best_score:.4f})"
                                )
                                return final

        # Strategy 1: treat data[4] as lengthflags and data[8:] as block payload.
        if len(data) >= 12:
            lf = struct.unpack_from("<I", data, 4)[0]
            flags = (lf >> 30) & 0x3
            blen = lf & 0x3FFFFFFF
            if 0 < blen <= len(data) - 8:
                payload = data[8 : 8 + blen]
                for blob in self._try_standard_block_decompress(payload, flags):
                    for stride in (12, 16, 20, 24, 28, 32):
                        mesh = self._parse_vertex_block(blob, filename, head_vcount if head_vcount < 2000000 else None, stride)
                        if mesh:
                            self._log(f"B4 decode hit (strategy1, stride={stride}, flags={flags})")
                            return mesh

        # Strategy 2: if data[4] looks like raw_size, try oodle on data[8:].
        raw_size_hint = struct.unpack_from("<I", data, 4)[0]
        if 256 <= raw_size_hint <= 128 * 1024 * 1024:
            dec = self.oodle.decompress(data[8:], raw_size_hint)
            if dec:
                for stride in (12, 16, 20, 24, 28, 32):
                    mesh = self._parse_vertex_block(dec, filename, head_vcount if head_vcount < 2000000 else None, stride)
                    if mesh:
                        self._log(f"B4 decode hit (strategy2, stride={stride})")
                        return mesh

        # Strategy 3: scan first 512 bytes for lengthflags-aligned compressed subblocks.
        scan_end = min(len(data) - 8, 512)
        for base in range(0, max(scan_end, 0), 4):
            lf = struct.unpack_from("<I", data, base)[0]
            flags = (lf >> 30) & 0x3
            blen = lf & 0x3FFFFFFF
            pstart = base + 4
            if blen < 8 or pstart + blen > len(data):
                continue
            payload = data[pstart : pstart + blen]
            for blob in self._try_standard_block_decompress(payload, flags):
                for stride in (12, 16, 20, 24, 28, 32):
                    mesh = self._parse_vertex_block(blob, filename, head_vcount if head_vcount < 2000000 else None, stride)
                    if mesh:
                        self._log(f"B4 decode hit (strategy3 @ {base}, stride={stride}, flags={flags})")
                        return mesh

        # Strategy 4: uncompressed B4 with vertex region at a fixed header
        # offset. Some RendInst variants (e.g. War Thunder railing posts and
        # post destr) store float-XYZ vertices directly in the file with no
        # Oodle/lengthflags framing, and the start offset is not a multiple of
        # the stride so strategy 0/2 cannot align onto it. Probe combinations.
        best_mesh: Optional[Mesh] = None
        best_score = -1.0
        best_start = 0
        best_stride = 0
        for start in (0x40, 0x60, 0x70, 0x80, 0x84, 0x90, 0xa0, 0xb0,
                      0x180, 0x190, 0x1a0, 0x1ac, 0x1b0):
            if start >= len(data):
                continue
            for stride in (12, 16, 20, 24, 28, 32):
                mesh = self._parse_vertex_block(data[start:], filename, None, stride)
                if not mesh:
                    continue
                score = self._mesh_shape_score(mesh)
                if score > best_score:
                    best_score = score
                    best_mesh = mesh
                    best_start = start
                    best_stride = stride
        if best_mesh is not None:
            self._log(
                f"B4 decode hit (strategy4 uncompressed-scan, start={best_start:#x}, "
                f"stride={best_stride}, verts={best_mesh['vertex_count']})"
            )
            return best_mesh

        return None

    def _decode_generic(self, data: bytes, filename: str) -> Optional[Mesh]:
        if len(data) < 16:
            return None
        vcount = struct.unpack_from("<I", data, 0)[0]
        if 9 <= vcount <= 300000:
            for stride in (12, 16, 20, 24):
                mesh = self._parse_vertex_block(data[4:], filename, vcount, stride)
                if mesh:
                    self._log(f"Generic decode hit (stride={stride})")
                    return mesh
        return None

    def _decode_ace50000_collision_mesh(self, data: bytes, filename: str) -> Optional[Mesh]:
        """Decode ACE50000 collision mesh.

        Observed layouts:
        - raw float XYZ stream (optionally with a small preamble)
        - float vertex stream followed by u16 index list (common for bunker collision)
        """
        mesh = self._file_specific.try_special_ace50000_override(filename)
        if mesh is not None:
            self._log("ACE50000 collision decode hit (file-specific-special-override)")
            return mesh

        # ACE resources may be wrapped in an ACE50001 container that carries
        # Dagor lengthflags framing (including zstd-compressed payloads).
        if len(data) >= 12:
            sig = struct.unpack_from("<I", data, 0)[0]
            if sig == 0xACE50001:
                lf = struct.unpack_from("<I", data, 4)[0]
                flags = (lf >> 30) & 0x3
                blen = lf & 0x3FFFFFFF
                if 0 < blen <= len(data) - 8:
                    payload = data[8 : 8 + blen]
                    tiny_marine = self._file_specific.decode_bush_marine_collision_wire(payload, filename)
                    if tiny_marine is not None:
                        return tiny_marine

                    blobs = self._try_standard_block_decompress(payload, flags)
                    if not blobs and flags == 0:
                        # Some tiny ACE50001 payloads are raw parameter blocks (not compressed).
                        blobs = [payload]

                    for blob in blobs:
                        # Tiny single-cls collision blobs (a single AABB +
                        # one cls_* token, e.g. solar_farm_panel_a_glass_collision)
                        # have no real vcount/icount framing. The structured
                        # fan-fallback misfires on them; prefer the AABB box
                        # decode in that case.
                        if (
                            len(blob) <= 320
                            and self._extract_collision_cls_token(blob) is not None
                            and not self._try_decode_ace_multi_cls_boxes(blob, filename)
                        ):
                            box = self._try_embedded_cls_aabb_wire(blob, filename)
                            if box is not None:
                                return box

                        # Prefer a structured mesh decode first. Only fall back
                        # to the cls_*/<material> AABB wireframe when no
                        # structured layout is detected. This avoids collapsing
                        # detailed collision meshes (e.g. bunker_concrete_pipe
                        # 16v/28-tri octagonal prism) to a single AABB.
                        mesh = self._decode_ace50000_collision_mesh(blob, filename)
                        if mesh:
                            if self.verbose:
                                self._log(
                                    f"ACE50000 container decode hit (ace50001 flags={flags}, payload={len(payload)} -> {len(blob)})"
                                )
                            return mesh

                        box = self._try_embedded_cls_aabb_wire(blob, filename)
                        if box is not None:
                            return box

        if len(data) < 36:
            return None

        def _parse_vertices(buf: bytes) -> Optional[List[Tuple[float, float, float]]]:
            if len(buf) < 36 or (len(buf) % 12) != 0:
                return None
            verts: List[Tuple[float, float, float]] = []
            try:
                for i in range(0, len(buf), 12):
                    x = struct.unpack_from("<f", buf, i)[0]
                    y = struct.unpack_from("<f", buf, i + 4)[0]
                    z = struct.unpack_from("<f", buf, i + 8)[0]
                    if not all(math.isfinite(v) for v in (x, y, z)):
                        return None
                    verts.append((x, y, z))
            except struct.error:
                return None
            return verts if len(verts) >= 3 else None

        is_lake = self._file_specific.is_lake_collision(filename)

        # 1a) Structured layout: [..header..][u32 vcount][stride*vcount verts][u32 icount][u16*icount indices]
        # Tries vertex strides 12 (XYZ) and 16 (XYZ + pad/W). For stride 16 the padding
        # words are typically (Z=0, W=1) which encodes a planar mesh in the XY plane.
        def _try_structured(stride: int) -> Optional[Tuple[List[Tuple[float, float, float]], List[Tuple[int, int, int]]]]:
            n = len(data)
            for vcount_off in range(0, min(512, n - 12), 4):
                try:
                    vcount = struct.unpack_from("<I", data, vcount_off)[0]
                except struct.error:
                    continue
                if vcount < 4 or vcount > 200000:
                    continue
                vstart = vcount_off + 4
                vend = vstart + vcount * stride
                if vend + 4 > n:
                    continue
                try:
                    icount = struct.unpack_from("<I", data, vend)[0]
                except struct.error:
                    continue
                if icount < 12 or (icount % 3) != 0 or icount > 1_000_000:
                    continue
                istart = vend + 4
                iend = istart + icount * 2
                if iend != n:
                    continue
                # Parse vertices
                verts: List[Tuple[float, float, float]] = []
                ok = True
                for i in range(vcount):
                    o = vstart + i * stride
                    try:
                        x = struct.unpack_from("<f", data, o)[0]
                        y = struct.unpack_from("<f", data, o + 4)[0]
                        z = struct.unpack_from("<f", data, o + 8)[0]
                    except struct.error:
                        ok = False
                        break
                    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                        ok = False
                        break
                    if any(abs(c) > self.MAX_ABS_COORD for c in (x, y, z)):
                        ok = False
                        break
                    verts.append((x, y, z))
                if not ok or len(verts) != vcount:
                    continue
                # Parse indices
                try:
                    idx = list(struct.unpack_from(f"<{icount}H", data, istart))
                except struct.error:
                    continue
                if max(idx) >= vcount:
                    continue
                faces: List[Tuple[int, int, int]] = []
                for i in range(0, icount, 3):
                    faces.append((idx[i], idx[i + 1], idx[i + 2]))
                return verts, faces
            return None

        # 1aa) Multi-section collision blob. Some ACE50000 payloads carry
        # several named sub-meshes (e.g. building + cls_* parts), each
        # introduced by a `1B 00 04 00` marker followed by a 48-byte transform
        # matrix, an 8-byte separator, then u32 vcount, vertex stream
        # (stride 16, XYZW), u32 icount, u16 indices. The single-section
        # `_try_structured` path requires `iend == n` and so misses these.
        # Concatenate all parsed sections into one mesh.
        if not is_lake:
            multi = self._try_decode_ace_multi_section(data)
            if multi is not None:
                verts_m, faces_m = multi
                verts_m, faces_m = self._file_specific.postprocess_oreshek_indexed(filename, verts_m, faces_m)
                mesh = self._build_mesh(filename, verts_m, faces=faces_m)
                if mesh:
                    self._log(
                        f"ACE50000 collision decode hit (multi-section, {len(verts_m)} vertices, {len(faces_m)} faces)"
                    )
                    return mesh

        if not is_lake:
            for stride in (16, 12):
                structured = _try_structured(stride)
                if structured is None:
                    continue
                verts_s, faces_s = structured
                verts_s, faces_s = self._file_specific.postprocess_oreshek_indexed(filename, verts_s, faces_s)
                if self._file_specific.prefer_wireframe_collision(filename):
                    lines = self._faces_to_unique_lines(faces_s)
                    lines = self._file_specific.filter_wireframe_lines_for_readability(filename, verts_s, lines)
                    mesh = self._build_mesh(filename, verts_s, faces=[], lines=lines)
                    if mesh:
                        self._log(
                            f"ACE50000 collision decode hit (structured stride={stride}, {len(verts_s)} vertices, {len(lines)} lines)"
                        )
                        return mesh
                mesh = self._build_mesh(filename, verts_s, faces=faces_s)
                if mesh:
                    self._log(
                        f"ACE50000 collision decode hit (structured stride={stride}, {len(verts_s)} vertices, {len(faces_s)} faces)"
                    )
                    return mesh

        # 1z) Multi-part Cbox composite: ACE50001 collision blobs that contain
        # `count` named sub-parts, each carrying its own AABB but no explicit
        # vertex/index list (flag word `1B 00 04 02`). Emit one wireframe box
        # per sub-part so composite shapes (T-shaped poles, multi-segment
        # towers, etc.) appear as the union of their boxes. Detected on
        # solar_farm_panel_a_pole_collision (cls_01 vertical pole +
        # cls_02 horizontal cross-bar).
        if not is_lake and len(data) >= 0x80:
            multi_mesh = self._try_decode_ace_multi_cls_boxes(data, filename)
            if multi_mesh is not None:
                return multi_mesh

        # 1z2) Bush collision proxy short-circuit. bush_*_collision blobs
        # carry an AABB at offsets 0/16 but no `cls_box`/`collision`
        # ASCII token, so the indexed-fan fallback below misreads them as
        # 16v/14f junk. Try the billboard gyro path early; the
        # filename-based bush trigger inside accepts these blobs.
        fname_lower = (filename or "").lower()
        if (
            not is_lake
            and fname_lower.startswith("bush_")
            and "_collision" in fname_lower
        ):
            bush_proxy = self._try_decode_billboard_collision_proxy(data, filename)
            if bush_proxy is not None:
                return bush_proxy

        # 1) Try vertex + index split first (matches bunker collision resources).
        # NOTE: this is a brute-force O(N^2) scan over all (vstart, split) pairs.
        # Cap it by blob size so large aircraft-style ACE50000 collisions
        # (e.g. fn_aircraft_logic ~30KB per blob) don't hang. Real meshes that
        # size are handled by the structured / multi-section paths above; if
        # those miss, brute-forcing a 30KB blob would not produce a reliable
        # match anyway.
        best: Optional[Tuple[float, List[Tuple[float, float, float]], List[Tuple[int, int, int]]]] = None
        max_start = min(32, max(len(data) - 36, 0))
        BRUTE_FORCE_MAX_BLOB = 4096
        if not is_lake and len(data) <= BRUTE_FORCE_MAX_BLOB:
            for vstart in range(0, max_start + 1, 4):
                for split in range(vstart + 36, len(data) - 6, 2):
                    vbytes = data[vstart:split]
                    ibytes = data[split:]
                    if (len(vbytes) % 12) != 0:
                        continue
                    if (len(ibytes) % 2) != 0:
                        continue
                    idx_count = len(ibytes) // 2
                    if idx_count < 12 or (idx_count % 3) != 0:
                        continue

                    verts = _parse_vertices(vbytes)
                    if not verts:
                        continue

                    idx: List[int] = []
                    try:
                        for i in range(0, len(ibytes), 2):
                            idx.append(struct.unpack_from("<H", ibytes, i)[0])
                    except struct.error:
                        continue

                    if not idx:
                        continue
                    max_idx = max(idx)
                    if max_idx >= len(verts):
                        continue

                    # Filter/remap indexed vertices to skip embedded garbage triplets.
                    used = set(idx)
                    remap: Dict[int, int] = {}
                    filtered: List[Tuple[float, float, float]] = []
                    for old_i, v in enumerate(verts):
                        if old_i not in used:
                            continue
                        if any(abs(c) > self.MAX_ABS_COORD for c in v):
                            # Keep topology intact for indexed collision meshes by
                            # replacing outlier payload triplets with origin.
                            v = (0.0, 0.0, 0.0)
                        remap[old_i] = len(filtered)
                        filtered.append(v)
                    if len(filtered) < 4:
                        continue

                    faces: List[Tuple[int, int, int]] = []
                    for i in range(0, len(idx), 3):
                        a0, b0, c0 = idx[i], idx[i + 1], idx[i + 2]
                        if a0 not in remap or b0 not in remap or c0 not in remap:
                            continue
                        a, b, c = remap[a0], remap[b0], remap[c0]
                        faces.append((a, b, c))
                    if len(faces) < 4:
                        continue

                    uniq_idx = len(set(idx))
                    nonzero = sum(1 for v in idx if v != 0)
                    geom_score = self._score_faces(filtered, faces)
                    edge_quality = self._face_edge_quality(filtered, faces)
                    # Favor dense index usage and expected preamble positions.
                    score = (
                        (len(faces) / 32.0)
                        +
                        (uniq_idx / float(max(1, len(filtered))))
                        + (nonzero / float(max(1, len(idx)))) * 0.2
                        + geom_score * 0.5
                        + edge_quality * 0.8
                        - (vstart * 0.001)
                    )
                    if best is None or score > best[0]:
                        best = (score, filtered, faces)

        if best is not None:
            _, vertices, faces = best
            vertices, faces = self._file_specific.postprocess_oreshek_indexed(filename, vertices, faces)
            if self._file_specific.is_vietnam_rocks_collision(filename):
                wv, wf = self._weld_vertices_and_remap_faces(vertices, faces, eps_decimals=6)
                if len(wv) >= 8 and len(wf) >= 16:
                    if self.verbose and (len(wv) != len(vertices) or len(wf) != len(faces)):
                        self._log(
                            f"Vietnam collision weld: verts {len(vertices)}->{len(wv)}, faces {len(faces)}->{len(wf)}"
                        )
                    vertices, faces = wv, wf

            if self._file_specific.prefer_wireframe_collision(filename):
                lines = self._faces_to_unique_lines(faces)
                lines = self._file_specific.filter_wireframe_lines_for_readability(filename, vertices, lines)
                mesh = self._build_mesh(filename, vertices, faces=[], lines=lines)
                if mesh:
                    self._log(
                        f"ACE50000 collision decode hit (indexed-wire, {len(vertices)} vertices, {len(lines)} lines)"
                    )
                    return mesh

            mesh = self._build_mesh(filename, vertices, faces=faces)
            if mesh:
                self._log(
                    f"ACE50000 collision decode hit (indexed, {len(vertices)} vertices, {len(faces)} faces)"
                )
                return mesh

        # 2) Lake-style planar fallback (file-specific; comparison-only).
        if is_lake:
            mesh = self._file_specific.decode_lake_collision_planar(data, filename)
            if mesh:
                return mesh

        # 2a) Billboard "COLLISION_<W>-<D>-<H>" proxy.
        # Many billboard assets (bushes, small trees, etc.) ship with a
        # canonical collision proxy whose geometry is described purely by an
        # ASCII tag of the form `COLLISION_60-60-120` (cm dimensions: width,
        # depth, height). Render it as a 3-axis "gyro" wireframe sphere
        # placed inside the AABB so the OBJ visualisation matches the
        # reference 2Gyro proxy used by Dagor.
        billboard = self._try_decode_billboard_collision_proxy(data, filename)
        if billboard is not None:
            return billboard

        # 2b) Planar-AABB quad fallback for tiny decal-style collisions.
        # Many decal_factory_*_collision payloads encode the geometry as
        #   [min XYZ, w][max XYZ, w][...]
        # where one axis has zero range (the decal thickness). The fan
        # fallback below misreads these as 3D fans, producing nonsense
        # meshes. When the first 24 bytes look like a planar AABB and the
        # structured/indexed decoders did not match, emit a flat 4-corner
        # quad in the planar plane instead.
        if len(data) >= 24:
            try:
                fmin = struct.unpack_from("<3f", data, 0)
                fmax = struct.unpack_from("<3f", data, 16)
            except struct.error:
                fmin = fmax = None  # type: ignore
            if fmin is not None and fmax is not None and \
               all(math.isfinite(v) for v in fmin) and \
               all(math.isfinite(v) for v in fmax) and \
               all(abs(v) <= self.MAX_ABS_COORD for v in fmin + fmax):
                ranges = tuple(fmax[i] - fmin[i] for i in range(3))
                # Find the planar axis (range < 1e-3) and require the other
                # two axes to span a non-trivial extent.
                planar_axis = None
                for ax in range(3):
                    other = [r for i, r in enumerate(ranges) if i != ax]
                    if abs(ranges[ax]) < 1e-3 and all(r > 1e-3 for r in other):
                        planar_axis = ax
                        break
                if planar_axis is not None:
                    plane_val = (fmin[planar_axis] + fmax[planar_axis]) * 0.5
                    axes = [i for i in range(3) if i != planar_axis]
                    a0, a1 = axes
                    corners_2d = [
                        (fmin[a0], fmin[a1]),
                        (fmax[a0], fmin[a1]),
                        (fmax[a0], fmax[a1]),
                        (fmin[a0], fmax[a1]),
                    ]
                    quad: List[Tuple[float, float, float]] = []
                    for u, v in corners_2d:
                        pt = [0.0, 0.0, 0.0]
                        pt[a0] = u
                        pt[a1] = v
                        pt[planar_axis] = plane_val
                        quad.append((pt[0], pt[1], pt[2]))
                    # Emit as 4-corner wireframe quad (matches user spec:
                    # "4 vertices at end and edges connecting them").
                    quad_lines = [(0, 1), (1, 2), (2, 3), (3, 0)]
                    mesh = self._build_mesh(filename, quad, faces=[], lines=quad_lines)
                    if mesh:
                        self._log(
                            f"ACE50000 collision decode hit (planar-aabb-quad-wire, "
                            f"axis={planar_axis}, "
                            f"size={ranges[a0]:.3f}x{ranges[a1]:.3f})"
                        )
                        return mesh

        # 3) Generic fallback: pure vertex stream + fan triangulation.
        for vstart in range(0, max_start + 1, 4):
            raw = _parse_vertices(data[vstart:])
            if not raw:
                continue

            verts = [
                v
                for v in raw
                if all(math.isfinite(c) and abs(c) <= self.MAX_ABS_COORD for c in v)
            ]
            if len(verts) < 4:
                continue

            faces: List[Tuple[int, int, int]] = []
            for i in range(1, len(verts) - 1):
                faces.append((0, i, i + 1))

            if self._file_specific.prefer_wireframe_collision(filename):
                lines = self._faces_to_unique_lines(faces)
                lines = self._file_specific.filter_wireframe_lines_for_readability(filename, verts, lines)
                mesh = self._build_mesh(filename, verts, faces=[], lines=lines)
                if mesh:
                    self._log(
                        f"ACE50000 collision decode hit (fan-wire, start={vstart}, {len(verts)} vertices, {len(lines)} lines)"
                    )
                    return mesh

            mesh = self._build_mesh(filename, verts, faces=faces)
            if mesh:
                self._log(
                    f"ACE50000 collision decode hit (fan, start={vstart}, {len(verts)} vertices, {len(faces)} faces)"
                )
                return mesh

        return None

    def _try_split_ace50000_named_cls(self, raw: bytes, filename: str) -> Optional[List[Mesh]]:
        """Decompose a multi-cls ACE50000 collision blob into named sub-meshes.

        Some collision resources (e.g. ``bridge_modern_concrete_a_collision``)
        carry several length-prefixed ``..._cls`` sections back-to-back inside
        a zstd-wrapped payload. The default decode concatenates them into one
        mesh; this splitter emits one mesh per cls section so the OBJ
        contains the actual sub-part names exposed by AssetViewer.

        Returns ``None`` unless at least two named cls sections were
        successfully parsed.
        """
        if len(raw) < 80:
            return None
        # ACE50001-style wrapper: 4-byte magic + 4-byte size + zstd payload.
        payload: Optional[bytes] = None
        if raw[:4] == b"\x01\x00\xE5\xAC":
            zstd_off = raw.find(b"\x28\xB5\x2F\xFD", 4, 32)
            if zstd_off > 0:
                payload = self._zstd_decompress(raw[zstd_off:])
        if payload is None:
            payload = raw
        n = len(payload)
        if n < 80:
            return None

        # Scan for length-prefixed strings ending in "_cls".
        sections: List[Tuple[int, str]] = []
        i = 0
        while i + 4 <= n:
            try:
                ln = struct.unpack_from("<I", payload, i)[0]
            except struct.error:
                break
            if 4 <= ln <= 128 and i + 4 + ln <= n:
                blob_name = bytes(payload[i + 4 : i + 4 + ln])
                if blob_name.endswith(b"_cls") and all(
                    32 <= b < 127 for b in blob_name
                ):
                    try:
                        name = blob_name.decode("ascii")
                    except UnicodeDecodeError:
                        i += 1
                        continue
                    sections.append((i + 4, name))
                    i += 4 + ln
                    continue
            i += 1

        if len(sections) < 2:
            return None

        marker = b"\x1B\x00\x04\x00"
        results: List[Mesh] = []
        for k, (tok_off, name) in enumerate(sections):
            end_search = sections[k + 1][0] if k + 1 < len(sections) else n
            m = payload.find(marker, tok_off, end_search)
            if m < 0:
                return None
            vcount_off = m + 60
            if vcount_off + 4 > n:
                return None
            try:
                vcount = struct.unpack_from("<I", payload, vcount_off)[0]
            except struct.error:
                return None
            if not (3 <= vcount <= 200_000):
                return None
            stride = 16
            vstart = vcount_off + 4
            vend = vstart + vcount * stride
            if vend + 4 > n:
                return None
            try:
                icount = struct.unpack_from("<I", payload, vend)[0]
            except struct.error:
                return None
            if not (3 <= icount <= 5_000_000) or (icount % 3) != 0:
                return None
            istart = vend + 4
            iend = istart + icount * 2
            if iend > n:
                return None
            verts: List[Tuple[float, float, float]] = []
            ok = True
            for v in range(vcount):
                o = vstart + v * stride
                try:
                    x, y, z = struct.unpack_from("<fff", payload, o)
                except struct.error:
                    ok = False
                    break
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    ok = False
                    break
                if any(abs(c) > self.MAX_ABS_COORD for c in (x, y, z)):
                    ok = False
                    break
                verts.append((x, y, z))
            if not ok:
                return None
            try:
                idx = struct.unpack_from(f"<{icount}H", payload, istart)
            except struct.error:
                return None
            if max(idx) >= vcount:
                return None
            faces: List[Tuple[int, int, int]] = []
            for q in range(0, icount, 3):
                faces.append((idx[q], idx[q + 1], idx[q + 2]))
            sub_filename = f"{name}.ACE50000"
            sub_mesh = self._build_mesh(sub_filename, verts, faces=faces)
            if not sub_mesh:
                return None
            results.append(sub_mesh)

        return results if len(results) >= 2 else None

    # ---- 77F8232F (RendInst) typed decoder ------------------------------
    #
    # Layout (verified against avg_volokolamsk_lake and avg_poland_iced_lake):
    #
    #   [0x00] u32 hdr_size      = 0x60
    #   [0x04] u64 reserved      = 0xFFFFFFFFFFFFFFFF
    #   [0x0C] u32 lod_count
    #   [0x10] u32 unknown       = 0xC0000090 (constant)
    #   [0x14] u32 comp_size     (high bit set as flag, mask 0x7FFFFFFF)
    #   [0x18] u32 raw_size      (uncompressed payload size)
    #   [0x1C ...]               Oodle Kraken-compressed payload of `comp_size`
    #                            bytes that decompresses to `raw_size` bytes
    #   [trailer]                RoSceneRendInst metadata (bbox, bsphere,
    #                            per-LOD distances, fixup table)
    #
    # Decoded payload layout:
    #
    #   [0x10] u32 rec_off    \u2014 offset of the per-LOD record table
    #   [0x14] u32 lod_count
    #   [rec_off + i*32] 32-byte LOD record whose first 16 bytes are
    #       Dagor's `MatVdataSrcHdr` (see `_decode_77f8232f_rendinst`).
    #   Vertex+index streams follow contiguously, one LOD after another,
    #   starting right after the record table.
    #
    # The per-LOD index buffer is encoded with meshoptimizer's
    # `encodeIndexSequence` codec (header byte 0xD0); we decode it via
    # `_decode_meshopt_index_sequence` and feed the authored triangles
    # straight into `_build_mesh`.  The 2D XZ Delaunay path is retained
    # only as a structural fallback for streams whose IB cannot be
    # decoded (unknown codec / corrupted data).

    # Per-LOD record (32 bytes) starts with Dagor's `MatVdataSrcHdr`
    # (16 bytes) and is followed by 16 bytes of per-LOD aux data (bbox /
    # range / material refs — not consumed here).  The first 16 bytes are
    # the same struct used by the engine's `matVdataLoad.cpp`:
    #   u32 vertNum
    #   u32 vertStride : 8 | packedIdxSizeLo : 24
    #   u32 idxSizeBytes : 28 | packedIdxSizeHi : 4
    #   u32 flags    (VDATA_PACKED_IB=0x200, VDATA_I16=0x20, ...)
    # `idxSizeBytes` is the size of the *unpacked* IB in bytes; the
    # logical index count is therefore `idxSizeBytes / sizeof(uint16)`.
    _RI_LOD_RECORD_SIZE = 32
    _VDATA_I16 = 0x20
    _VDATA_I32 = 0x40
    _VDATA_PACKED_IB = 0x200

    # Per-channel sizes for the VSDT_* enum (high 16 bits of
    # ``CompiledShaderChannelId::t``). Used when walking a vDecl to
    # determine the byte offset of each channel within a vertex.
    # See ``D:/DagorEngine/prog/dagorInclude/drv/3d/dag_consts_base.h``.
    _VSDT_SIZES = {
        0x00: 4,    # FLOAT1
        0x01: 8,    # FLOAT2
        0x02: 12,   # FLOAT3
        0x03: 16,   # FLOAT4
        0x04: 4,    # E3DCOLOR
        0x05: 4,    # UBYTE4
        0x06: 4,    # SHORT2
        0x07: 8,    # SHORT4
        0x09: 4,    # SHORT2N
        0x0A: 8,    # SHORT4N
        0x0B: 4,    # USHORT2N
        0x0C: 8,    # USHORT4N
        0x0D: 4,    # UDEC3
        0x0E: 4,    # DEC3N
        0x0F: 4,    # HALF2
        0x10: 8,    # HALF4
    }

    @staticmethod
    def _decode_meshopt_index_sequence(
        buf: bytes, index_count: int
    ) -> Optional[List[int]]:
        """Port of `meshopt_decodeIndexSequence` (header byte 0xD0).

        Layout: 1 header byte (0xD0 | version), then ``index_count``
        zigzag-varint encoded values of `(delta << 1) | baseline_bit`,
        then a 4-byte zero tail.  Returns ``None`` on any structural
        violation so callers can fall back to the heuristic path.
        """
        if index_count <= 0 or len(buf) < 1 + index_count + 4:
            return None
        if (buf[0] & 0xF0) != 0xD0:
            return None
        pos = 1
        safe_end = len(buf) - 4
        last = [0, 0]
        out: List[int] = []
        for _ in range(index_count):
            if pos >= safe_end:
                return None
            lead = buf[pos]
            pos += 1
            if lead < 128:
                v = lead
            else:
                v = lead & 0x7F
                shift = 7
                ok = False
                for _i in range(4):
                    if pos >= len(buf):
                        return None
                    g = buf[pos]
                    pos += 1
                    v |= (g & 0x7F) << shift
                    shift += 7
                    if g < 128:
                        ok = True
                        break
                if not ok:
                    return None
            cur = v & 1
            v >>= 1
            d = (v >> 1) ^ -(v & 1)
            idx = (last[cur] + d) & 0xFFFFFFFF
            last[cur] = idx
            out.append(idx)
        # The decoder must consume exactly the data region (everything
        # before the 4-byte zero tail).
        if pos != safe_end:
            return None
        return out

    def _decode_77f8232f_rendinst(self, data: bytes, filename: str) -> Optional[List[Mesh]]:
        """Decode a 77F8232F (RendInst) resource into one Mesh per LOD.

        Returns ``None`` when the file is too small, the Oodle DLL is not
        available, or any structural sanity check fails. Callers should fall
        back to the heuristic decoder in that case.
        """
        if len(data) < 0x20:
            return None

        try:
            sentinel0, sentinel1 = struct.unpack_from("<II", data, 0x04)
            csize_raw = struct.unpack_from("<I", data, 0x14)[0]
            usize = struct.unpack_from("<I", data, 0x18)[0]
        except struct.error:
            return None

        if sentinel0 != 0xFFFFFFFF or sentinel1 != 0xFFFFFFFF:
            return None
        csize = csize_raw & 0x7FFFFFFF
        if csize <= 0 or usize <= 0 or usize > 32 * 1024 * 1024:
            return None
        comp_start = 0x1C
        comp_end = comp_start + csize
        if comp_end > len(data):
            return None

        decoded = self.oodle.decompress(data[comp_start:comp_end], usize)
        if decoded is None or len(decoded) < 0x20:
            return None

        # Read records offset and lod count from the decoded header, NOT
        # the file header. The decoded layout is:
        #   decoded[0x10] = u32 records_offset (varies: 0x48 / 0x50 / 0x60)
        #   decoded[0x14] = u32 lod_count
        try:
            rec_off = struct.unpack_from("<I", decoded, 0x10)[0]
            lod_count = struct.unpack_from("<I", decoded, 0x14)[0]
        except struct.error:
            return None
        if not (1 <= lod_count <= 8):
            return None
        if rec_off < 0x18 or rec_off + lod_count * self._RI_LOD_RECORD_SIZE > len(decoded):
            return None

        # Per-LOD records: read the full 16-byte MatVdataSrcHdr.
        # Layout (little-endian, bit-packed):
        #   u32 vertNum
        #   u32 (vertStride : 8) | (packedIdxSizeLo : 24)
        #   u32 (idxSizeBytes : 28) | (packedIdxSizeHi : 4)
        #   u32 flags
        # We capture (vertNum, vertStride, packedIdxSize, idxSizeBytes, flags).
        lod_records: List[Tuple[int, int, int, int, int]] = []
        for i in range(lod_count):
            off = rec_off + i * self._RI_LOD_RECORD_SIZE
            vertNum = struct.unpack_from("<I", decoded, off + 0)[0]
            w1 = struct.unpack_from("<I", decoded, off + 4)[0]
            w2 = struct.unpack_from("<I", decoded, off + 8)[0]
            flags = struct.unpack_from("<I", decoded, off + 12)[0]
            vertStride = w1 & 0xFF
            packedIdxLo = (w1 >> 8) & 0xFFFFFF
            idxSizeBytes = w2 & 0x0FFFFFFF
            packedIdxHi = (w2 >> 28) & 0xF
            packedIdxSize = packedIdxLo | (packedIdxHi << 24)
            if vertNum <= 0 or vertNum > 65535:
                return None
            if vertStride < 12 or vertStride > 64:
                return None
            if idxSizeBytes < 0 or packedIdxSize < 0:
                return None
            lod_records.append(
                (vertNum, vertStride, packedIdxSize, idxSizeBytes, flags)
            )

        # Walk vertex+index streams sequentially.  After each LOD's
        # vertex buffer we know the exact byte size of the index buffer
        # (packedIdxSize when VDATA_PACKED_IB is set, otherwise idxSizeBytes
        # for raw u16/u32 indices), so no marker-scanning is needed.
        meshes: List[Mesh] = []
        v_off = rec_off + lod_count * self._RI_LOD_RECORD_SIZE
        stem = filename.rsplit(".", 1)[0]

        for lod_idx, (vertNum, vertStride, packedIdxSize, idxSizeBytes, flags) \
                in enumerate(lod_records):
            v_need = vertNum * vertStride
            if v_off + v_need > len(decoded):
                # Higher LODs may use a not-yet-supported encoding.  Stop
                # and keep whatever we have already decoded.
                break

            verts: List[Tuple[float, float, float]] = []
            ok = True
            for vi in range(vertNum):
                base = v_off + vi * vertStride
                try:
                    x, y, z = struct.unpack_from("<fff", decoded, base)
                except struct.error:
                    ok = False
                    break
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    ok = False
                    break
                if max(abs(x), abs(y), abs(z)) > self.MAX_ABS_COORD:
                    ok = False
                    break
                verts.append((x, y, z))
            if not ok or not verts:
                break
            v_off += v_need

            # Decode the index buffer.
            ib_bytes_in_stream = packedIdxSize if (flags & self._VDATA_PACKED_IB) \
                else idxSizeBytes
            ib_end = v_off + ib_bytes_in_stream
            faces: List[Tuple[int, int, int]] = []
            indices: Optional[List[int]] = None
            if ib_bytes_in_stream > 0 and ib_end <= len(decoded):
                ib_buf = decoded[v_off:ib_end]
                elem_size = 4 if (flags & self._VDATA_I32) else 2
                idx_count = idxSizeBytes // elem_size
                if flags & self._VDATA_PACKED_IB:
                    indices = self._decode_meshopt_index_sequence(ib_buf, idx_count)
                else:
                    if elem_size == 2:
                        indices = list(
                            struct.unpack_from(f"<{idx_count}H", ib_buf, 0)
                        )
                    else:
                        indices = list(
                            struct.unpack_from(f"<{idx_count}I", ib_buf, 0)
                        )
                if indices is not None and indices and max(indices) < vertNum \
                        and len(indices) % 3 == 0:
                    faces = [
                        (indices[k], indices[k + 1], indices[k + 2])
                        for k in range(0, len(indices), 3)
                    ]
                else:
                    indices = None
            v_off = ib_end

            # Fallback only when the IB couldn't be decoded structurally
            # (e.g. unknown codec / corrupted stream).  The XZ-Delaunay
            # path is exact for flat meshes and produces a usable surface
            # for non-flat ones until proper decoding is added.
            if not faces:
                faces = self._delaunay_triangulate_xz(verts)

            sub_filename = f"{stem}_lod{lod_count - 1 - lod_idx}.77F8232F"
            sub_mesh = self._build_mesh(sub_filename, verts, faces=faces)
            if not sub_mesh:
                break
            meshes.append(sub_mesh)

        return meshes if meshes else None

    @staticmethod
    def _delaunay_triangulate_xz(
        verts: Sequence[Tuple[float, float, float]],
    ) -> List[Tuple[int, int, int]]:
        """XZ-Delaunay triangulation \u2014 fallback for clouds whose authored
        index buffer cannot be decoded structurally.

        Exact for flat ground meshes (lakes, water decals, ground cards);
        produces a plausible-but-not-authoritative surface for volumetric
        clouds.  Falls back to a trivial fan when SciPy is unavailable or
        the projection is degenerate.
        """
        if len(verts) < 3:
            return []
        try:
            from scipy.spatial import Delaunay  # type: ignore
        except Exception:
            return [(i, i + 1, i + 2) for i in range(len(verts) - 2)]
        pts = [(v[0], v[2]) for v in verts]
        try:
            tri = Delaunay(pts)
        except Exception:
            return [(0, i + 1, i + 2) for i in range(len(verts) - 2)]
        return [
            (int(simplex[0]), int(simplex[1]), int(simplex[2]))
            for simplex in tri.simplices
        ]

    def _decode_b4b7d9c4_dynmodel(
        self, data: bytes, filename: str
    ) -> Optional[List[Mesh]]:
        """Decode a Dagor DynamicRenderableSceneLodsResource (class
        id ``B4B7D9C4``) into one Mesh per (LOD, rigid-node) pair.

        Wire format (matches ``DynamicRenderableSceneLodsResource::loadResource``
        in ``D:\\DagorEngine\\prog\\engine\\shaders\\dynSceneRes.cpp``):

          [00] u32 res_sz                    — LodsResource dump size
          [04] 4×u32 matVdata header
                 (tex_count|0xFFFFFFFF, mat_count|0xFFFFFFFF,
                  vdata_count, hdrSz_with_compr_flags)
          if non-sentinel: u32 tex_pool_size + tex_pool_size bytes
          loadMatVdata:
            u32 block_hdr (tag=high2, length=low30)
            block_body (zstd/oodle compressed):
              hdrSz bytes  PatchableTab<mat>(16)+PatchableTab<vdata>(16)
                           + N×VdataHdr(32) + per-vdata vDecl payload
              per-vdata VB (vertNum*vertStride raw bytes)
              per-vdata IB (raw or meshopt-encoded sequence)
          res_sz bytes LodsResource dump (lods[]+bbox+bpC arrays)
          per-LOD: u32 scene_sz + scene_sz bytes scene dump (rigids[]+skins[])
                   per-rigid: mesh_sz bytes ShaderMesh dump (RElem[])

        Returns None if the binary doesn't match the expected layout
        (caller should fall back to the legacy ``_decode_b4b7d9c4``).
        """
        if len(data) < 24:
            return None
        try:
            res_sz, t0, t1, vdata_count, hdr_sz_raw = struct.unpack_from(
                "<IIIII", data, 0
            )
        except struct.error:
            return None
        # Sanity: LodsResource dump must be reasonable.
        if res_sz < 16 or res_sz > 4096:
            return None
        if vdata_count == 0 or vdata_count > 64:
            return None

        pos = 20

        # Decode compression flags from hdrSz (matches matVdataLoad.cpp).
        compr_type = 0
        matVdataHdrSz = hdr_sz_raw
        if (matVdataHdrSz & 0xE0000000) == 0xC0000000:
            compr_type = 3  # zstd / oodle
            matVdataHdrSz &= ~0xC0000000
        elif matVdataHdrSz & 0x80000000:
            compr_type = 1  # zlib
            matVdataHdrSz = ((~matVdataHdrSz) + 1) & 0xFFFFFFFF
        elif matVdataHdrSz & 0x40000000:
            compr_type = 2  # lzma
            matVdataHdrSz &= ~0x40000000
        if matVdataHdrSz < 32 or matVdataHdrSz > 64 * 1024:
            return None

        # Skip texname pool when materials/textures are inline.
        if not (t0 == 0xFFFFFFFF and t1 == 0xFFFFFFFF):
            if pos + 4 > len(data):
                return None
            tex_sz = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if tex_sz > 0:
                if pos + tex_sz > len(data):
                    return None
                pos += tex_sz

        # Read matVdata block. Only zstd/oodle compressed blocks are
        # supported (which is what every modern .grp uses).
        if compr_type != 3:
            self._log(
                f"DynModel {filename}: unsupported matVdata compr_type={compr_type}"
            )
            return None
        if pos + 4 > len(data):
            return None
        block_hdr = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        block_tag = (block_hdr >> 30) & 0x3  # 1=ZSTD, 2=OODLE
        block_len = block_hdr & 0x3FFFFFFF
        block_start = pos
        block_end = pos + block_len
        if block_end > len(data):
            return None

        if block_tag == 2:
            if block_len < 4:
                return None
            raw_size = struct.unpack_from("<I", data, block_start)[0]
            if raw_size <= 0 or raw_size > 256 * 1024 * 1024:
                return None
            comp = bytes(data[block_start + 4:block_end])
            full = self.oodle.decompress(comp, raw_size)
        elif block_tag == 1:
            full = self._zstd_decompress(bytes(data[block_start:block_end]))
        else:
            self._log(
                f"DynModel {filename}: unsupported beginBlock tag={block_tag}"
            )
            return None
        if full is None or len(full) < matVdataHdrSz:
            return None
        pos = block_end  # advance past compressed block

        # Parse MatVdataHdr. Layout (verified against Dagor source):
        #   PatchableTab<ShaderMaterialProperties> mat (16 B)
        #   PatchableTab<VdataHdr> vdata (16 B)
        # PatchableTab on disk = u64 dptr (low32=byte offset, high32=count)
        # plus 8 bytes of unused dcnt/reserved.
        try:
            vdata_dptr = struct.unpack_from("<Q", full, 16)[0]
        except struct.error:
            return None
        vdata_ofs = vdata_dptr & 0xFFFFFFFF
        vd_count = (vdata_dptr >> 32) & 0xFFFFFFFF
        if vd_count != vdata_count:
            self._log(
                f"DynModel {filename}: vdata count mismatch "
                f"hdr={vdata_count} tab={vd_count}"
            )
            return None
        if vdata_ofs + vd_count * 32 > len(full):
            return None

        vdatas: List[Tuple[int, int, int, int, int]] = []
        # Per-vdata position channel descriptor: (byte_offset, vsdt_type)
        # parsed from the per-vdata vDecl PatchableTab. Without this we
        # mis-decode 20-byte-stride vdatas where the layout is
        # ``SHORT2 TC1 (4) + SHORT4N POS (8) + E3DCOLOR NORM (4) + SHORT2
        # TC0 (4)`` — i.e. position lives at offset 4, not 0.
        vdata_pos_ch: List[Tuple[int, int]] = []
        for i in range(vd_count):
            off = vdata_ofs + i * 32
            try:
                vertNum = struct.unpack_from("<I", full, off)[0]
                w1 = struct.unpack_from("<I", full, off + 4)[0]
                w2 = struct.unpack_from("<I", full, off + 8)[0]
                flags = struct.unpack_from("<I", full, off + 12)[0]
                decl_ofs = struct.unpack_from("<I", full, off + 16)[0]
                decl_cnt = struct.unpack_from("<I", full, off + 20)[0]
            except struct.error:
                return None
            vertStride = w1 & 0xFF
            packedIdxLo = (w1 >> 8) & 0xFFFFFF
            idxSize = w2 & 0x0FFFFFFF
            packedIdxHi = (w2 >> 28) & 0xF
            packedIdxSize = packedIdxLo | (packedIdxHi << 24)
            if vertNum > (1 << 24) or vertStride < 4 or vertStride > 128:
                return None
            vdatas.append((vertNum, vertStride, packedIdxSize, idxSize, flags))

            # Walk the vDecl and find the POS channel (SCUSAGE_POS=0).
            # Each ``CompiledShaderChannelId`` is 12 bytes:
            #   int32 t (VSDT|CMOD), int32 vbu (usage), int16 vbui (idx),
            #   uint16 streamId.
            pos_off = 0
            pos_vsdt = 0x02  # default VSDT_FLOAT3
            cur_off = 0
            found_pos = False
            if 0 < decl_ofs < len(full) and 0 < decl_cnt < 16:
                for k in range(decl_cnt):
                    o = decl_ofs + k * 12
                    if o + 12 > len(full):
                        break
                    try:
                        t_val = struct.unpack_from("<i", full, o)[0]
                        usage = struct.unpack_from("<i", full, o + 4)[0]
                    except struct.error:
                        break
                    vsdt = (t_val >> 16) & 0xFF
                    ch_size = self._VSDT_SIZES.get(vsdt, 0)
                    if usage == 0 and not found_pos:  # SCUSAGE_POS
                        pos_off = cur_off
                        pos_vsdt = vsdt
                        found_pos = True
                    cur_off += ch_size
            vdata_pos_ch.append((pos_off, pos_vsdt))

        # Read per-vdata VB+IB from the rest of the decompressed block.
        # Each vdata: VB (vertNum*vertStride bytes) then IB (packedIdxSize
        # bytes when VDATA_PACKED_IB else idxSize bytes).
        fpos = matVdataHdrSz
        # Each entry: (vertNum, vertStride, vb_bytes, indices_or_None)
        vb_buffers: List[Tuple[int, int, bytes, Optional[List[int]]]] = []
        for vertNum, vertStride, packedIdxSize, idxSize, flags in vdatas:
            vb_size = vertNum * vertStride
            if fpos + vb_size > len(full):
                return None
            vb = bytes(full[fpos:fpos + vb_size])
            fpos += vb_size

            ib_bytes = packedIdxSize if (flags & self._VDATA_PACKED_IB) else idxSize
            elem_size = 4 if (flags & self._VDATA_I32) else 2
            idx_count = idxSize // elem_size if elem_size else 0
            ib_buf = full[fpos:fpos + ib_bytes]
            fpos += ib_bytes

            indices: Optional[List[int]] = None
            if idx_count > 0 and ib_bytes > 0:
                if flags & self._VDATA_PACKED_IB:
                    indices = self._decode_meshopt_index_sequence(
                        bytes(ib_buf), idx_count
                    )
                else:
                    try:
                        if elem_size == 2:
                            indices = list(
                                struct.unpack_from(
                                    f"<{idx_count}H", ib_buf, 0
                                )
                            )
                        else:
                            indices = list(
                                struct.unpack_from(
                                    f"<{idx_count}I", ib_buf, 0
                                )
                            )
                    except struct.error:
                        indices = None
            vb_buffers.append((vertNum, vertStride, vb, indices))

        # Read LodsResource dump (lods[] PatchableTab + bbox + bpC arrays).
        if pos + res_sz > len(data):
            return None
        lods_dump = data[pos:pos + res_sz]
        pos += res_sz
        try:
            lods_dptr = struct.unpack_from("<Q", lods_dump, 0)[0]
        except struct.error:
            return None
        lods_ofs = lods_dptr & 0xFFFFFFFF
        lod_count = (lods_dptr >> 32) & 0xFFFFFFFF
        if not (1 <= lod_count <= 8):
            return None
        if lods_ofs + lod_count * 16 > len(lods_dump):
            return None
        lod_descs: List[Tuple[int, float, float]] = []
        for i in range(lod_count):
            off = lods_ofs + i * 16
            scene_sz = struct.unpack_from("<I", lods_dump, off)[0]
            rng = struct.unpack_from("<f", lods_dump, off + 8)[0]
            tex_scale = struct.unpack_from("<f", lods_dump, off + 12)[0]
            lod_descs.append((scene_sz, rng, tex_scale))

        # Bound-pack constants (used when ``isBoundPackUsed()`` returns
        # true). Layout in LodsResource dump (after the lods PatchableTab):
        #   [16..39]  BBox3 bbox (Point3 lim_min + Point3 lim_max)
        #   [40..55]  float bpC254[4]  — pos offset (xyz) for unpacking
        #   [56..71]  float bpC255[4]  — pos scale  (xyz) for unpacking
        # Compressed-position vertices store i16x4 with the position as
        # ``i16x3 * bpC255.xyz / 32767 + bpC254.xyz``. Reference:
        # `D:\\DagorEngine\\prog\\engine\\shaders\\dynSceneRes.cpp:1391`.
        bp_ofs = (0.0, 0.0, 0.0)
        bp_mul = (0.0, 0.0, 0.0)
        if len(lods_dump) >= 72:
            try:
                bp_ofs = struct.unpack_from("<fff", lods_dump, 40)
                bp_mul = struct.unpack_from("<fff", lods_dump, 56)
            except struct.error:
                pass

        bound_pack_used = any(
            v != 0.0 and math.isfinite(v) for v in bp_mul
        )

        # Read DynSceneResNameMapResource (RoNameMapEx) dump.
        if pos + 4 > len(data):
            return None
        nm_sz = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if nm_sz <= 0 or nm_sz > 1024 * 1024 or pos + nm_sz > len(data):
            return None
        nm_dump = data[pos:pos + nm_sz]
        pos += nm_sz
        nodeid_to_name: Dict[int, str] = {}
        try:
            map_dptr = struct.unpack_from("<Q", nm_dump, 0)[0]
            id_dptr = struct.unpack_from("<Q", nm_dump, 16)[0]
        except struct.error:
            return None
        map_ofs = map_dptr & 0xFFFFFFFF
        map_count = (map_dptr >> 32) & 0xFFFFFFFF
        id_ofs = id_dptr & 0xFFFFFFFF
        id_count = (id_dptr >> 32) & 0xFFFFFFFF
        names_by_ord: List[str] = []
        if 0 <= map_count <= 4096 and map_ofs + map_count * 8 <= len(nm_dump):
            for i in range(map_count):
                ptr_off = map_ofs + i * 8
                str_ofs = (
                    struct.unpack_from("<Q", nm_dump, ptr_off)[0] & 0xFFFFFFFF
                )
                if str_ofs >= len(nm_dump):
                    names_by_ord.append(f"node_{i}")
                    continue
                end = nm_dump.find(b"\x00", str_ofs)
                if end < 0:
                    end = len(nm_dump)
                try:
                    name = bytes(nm_dump[str_ofs:end]).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    name = f"node_{i}"
                names_by_ord.append(name or f"node_{i}")
        if 0 <= id_count <= 4096 and id_ofs + id_count * 2 <= len(nm_dump):
            for i in range(min(id_count, len(names_by_ord))):
                nid = struct.unpack_from("<H", nm_dump, id_ofs + i * 2)[0]
                if names_by_ord[i] not in (None, ""):
                    nodeid_to_name[nid] = names_by_ord[i]

        # Per LOD: read scene dump + per-rigid mesh dumps.
        meshes_out: List[Mesh] = []
        stem = filename.rsplit(".", 1)[0]
        # Look up the sibling skeleton's world transforms so each rigid
        # can be placed in node-world space (each rigid stores
        # node-local geometry; runtime renders with ``cb.getNodeWtm``).
        wtm_lookup = self._skeleton_wtm_cache.get(stem.lower())
        for lod_idx, (scene_sz, lod_range, _ts) in enumerate(lod_descs):
            if scene_sz <= 0 or scene_sz > 16 * 1024 * 1024:
                continue
            if pos + scene_sz > len(data):
                break
            scene_dump = data[pos:pos + scene_sz]
            pos += scene_sz
            try:
                rigids_dptr = struct.unpack_from("<Q", scene_dump, 0)[0]
            except struct.error:
                break
            rigids_ofs = rigids_dptr & 0xFFFFFFFF
            rigids_count = (rigids_dptr >> 32) & 0xFFFFFFFF
            if rigids_count > 4096:
                break
            if rigids_ofs + rigids_count * 32 > len(scene_dump):
                break
            rigid_descs: List[Tuple[int, int]] = []
            for r in range(rigids_count):
                roff = rigids_ofs + r * 32
                # mesh field is u64 PatchablePtr; res_sz is the low 32 bits.
                mesh_sz = struct.unpack_from("<I", scene_dump, roff)[0]
                node_id = struct.unpack_from("<i", scene_dump, roff + 24)[0]
                rigid_descs.append((mesh_sz, node_id))

            name_use_count: Dict[str, int] = {}
            for r_idx, (mesh_sz, node_id) in enumerate(rigid_descs):
                if mesh_sz <= 0 or mesh_sz > 16 * 1024 * 1024:
                    continue
                if pos + mesh_sz > len(data):
                    break
                mesh_dump = data[pos:pos + mesh_sz]
                pos += mesh_sz
                # ShaderMesh dump:
                #   PatchableTab<RElem> elems (16)         — opaque-stage elems
                #   PatchableTab<RElem> telem (16)         — older format only;
                #       in modern format these 16 bytes are stageEndElemIdx[8]
                #   int _deprecatedMaxMatPass (4)          — high bit set ⇒
                #       modern format; otherwise older format with elem+telem
                #   int _resv (4)
                # RElem on disk = 48 bytes:
                #   u64 e + u64 mat + u64 vertexData
                #   i32 vdOrderIndex,sv,numv,si,numf,baseVertex
                if len(mesh_dump) < 40:
                    continue
                try:
                    elems_dptr = struct.unpack_from("<Q", mesh_dump, 0)[0]
                    telem_dptr = struct.unpack_from("<Q", mesh_dump, 16)[0]
                    deprecated_max_mat_pass = struct.unpack_from(
                        "<I", mesh_dump, 32
                    )[0]
                except struct.error:
                    continue
                elems_ofs = elems_dptr & 0xFFFFFFFF
                elems_count = (elems_dptr >> 32) & 0xFFFFFFFF
                # Older format: concatenate elems + telem (matches
                # `ShaderMesh::patchData` in shaderMesh.cpp).
                if not (deprecated_max_mat_pass & 0x80000000):
                    telem_ofs = telem_dptr & 0xFFFFFFFF
                    telem_count = (telem_dptr >> 32) & 0xFFFFFFFF
                    if elems_count == 0 and telem_count > 0:
                        elems_ofs = telem_ofs
                        elems_count = telem_count
                    elif (
                        telem_count > 0
                        and elems_ofs + elems_count * 48 == telem_ofs
                    ):
                        # elems immediately followed by telem in memory ⇒
                        # combined count.
                        elems_count += telem_count
                if elems_count > 1024:
                    continue
                if elems_ofs + elems_count * 48 > len(mesh_dump):
                    continue

                verts_combined: List[Tuple[float, float, float]] = []
                faces_combined: List[Tuple[int, int, int]] = []
                base_v = 0
                for e in range(elems_count):
                    eoff = elems_ofs + e * 48
                    try:
                        vd_idx = (
                            struct.unpack_from("<Q", mesh_dump, eoff + 16)[0]
                            & 0xFFFFFFFF
                        )
                        sv = struct.unpack_from("<i", mesh_dump, eoff + 28)[0]
                        numv = struct.unpack_from("<i", mesh_dump, eoff + 32)[0]
                        si = struct.unpack_from("<i", mesh_dump, eoff + 36)[0]
                        numf = struct.unpack_from("<i", mesh_dump, eoff + 40)[0]
                        baseVertex = struct.unpack_from(
                            "<i", mesh_dump, eoff + 44
                        )[0]
                    except struct.error:
                        continue
                    if vd_idx >= len(vb_buffers):
                        continue
                    vert_num, v_stride, vb, indices = vb_buffers[vd_idx]
                    pos_off, pos_vsdt = vdata_pos_ch[vd_idx]
                    # Per Dagor's d3d::drawind(si, numf, baseVertex)
                    # semantics: locked CPU verts span
                    # [baseVertex+sv, baseVertex+sv+numv); IB stores
                    # values in [sv, sv+numv) (pre-baseVertex).
                    vstart = baseVertex + sv
                    if numv <= 0 or vstart < 0 or vstart + numv > vert_num:
                        continue
                    local_verts: List[Tuple[float, float, float]] = []
                    ok = True
                    # Decode position channel using its VSDT type from the
                    # vDecl. For SHORT4N positions we apply the bound-pack
                    # constants (i16x3 * bpC255 / 32767 + bpC254). For
                    # FLOAT3 we read raw float3.
                    if pos_vsdt == 0x02:  # VSDT_FLOAT3
                        pos_size = 12
                    elif pos_vsdt == 0x0A:  # VSDT_SHORT4N (compressed)
                        pos_size = 8
                    else:
                        # Fall back to legacy heuristic: float3 if stride>=12
                        # else short4n.
                        pos_vsdt = 0x02 if v_stride >= 12 else 0x0A
                        pos_size = 12 if pos_vsdt == 0x02 else 8
                    for vi in range(numv):
                        po = (vstart + vi) * v_stride + pos_off
                        if po + pos_size > len(vb):
                            ok = False
                            break
                        try:
                            if pos_vsdt == 0x0A:
                                xi, yi, zi, _wi = struct.unpack_from(
                                    "<hhhh", vb, po
                                )
                                x = xi * bp_mul[0] / 32767.0 + bp_ofs[0]
                                y = yi * bp_mul[1] / 32767.0 + bp_ofs[1]
                                z = zi * bp_mul[2] / 32767.0 + bp_ofs[2]
                            else:
                                x, y, z = struct.unpack_from("<fff", vb, po)
                        except struct.error:
                            ok = False
                            break
                        if not (
                            math.isfinite(x)
                            and math.isfinite(y)
                            and math.isfinite(z)
                        ):
                            ok = False
                            break
                        local_verts.append((x, y, z))
                    if not ok or not local_verts:
                        continue
                    if (
                        indices is not None
                        and numf > 0
                        and si >= 0
                        and si + numf * 3 <= len(indices)
                    ):
                        for f in range(numf):
                            i0 = indices[si + f * 3 + 0] - sv
                            i1 = indices[si + f * 3 + 1] - sv
                            i2 = indices[si + f * 3 + 2] - sv
                            if (
                                0 <= i0 < len(local_verts)
                                and 0 <= i1 < len(local_verts)
                                and 0 <= i2 < len(local_verts)
                            ):
                                faces_combined.append(
                                    (
                                        base_v + i0,
                                        base_v + i1,
                                        base_v + i2,
                                    )
                                )
                    verts_combined.extend(local_verts)
                    base_v += len(local_verts)

                if not verts_combined:
                    continue
                node_name = nodeid_to_name.get(
                    node_id, f"node{node_id}"
                )
                use_n = name_use_count.get(node_name, 0)
                name_use_count[node_name] = use_n + 1
                disambig = f"_{use_n}" if use_n > 0 else ""
                obj_name = f"{stem}_lod{lod_idx}_{node_name}{disambig}"
                # Apply node world transform if a sibling skeleton was
                # decoded. Without it every rigid sits at the local
                # origin (e.g. all wheels overlap on top of each other).
                if wtm_lookup is not None:
                    # Skeleton bone names occasionally drop the leading
                    # '@' that DynModel rigid names use (e.g. mesh
                    # "@root" looks up bone "root").
                    w = wtm_lookup.get(node_name)
                    if w is None and node_name.startswith("@"):
                        w = wtm_lookup.get(node_name[1:])
                    if w is not None and len(w) == 12:
                        c0x, c0y, c0z = w[0], w[1], w[2]
                        c1x, c1y, c1z = w[3], w[4], w[5]
                        c2x, c2y, c2z = w[6], w[7], w[8]
                        c3x, c3y, c3z = w[9], w[10], w[11]
                        verts_combined = [
                            (
                                c0x * vx + c1x * vy + c2x * vz + c3x,
                                c0y * vx + c1y * vy + c2y * vz + c3y,
                                c0z * vx + c1z * vy + c2z * vz + c3z,
                            )
                            for (vx, vy, vz) in verts_combined
                        ]
                m: Mesh = {
                    "filename": f"{obj_name}.B4B7D9C4",
                    "vertices": verts_combined,
                    "normals": None,
                    "faces": faces_combined,
                    "lines": [],
                    "vertex_count": len(verts_combined),
                    "face_count": len(faces_combined),
                    "line_count": 0,
                    "obj_object_name": obj_name,
                    "obj_variant_key": stem,
                    "_dynmodel_lod_index": lod_idx,
                    "_dynmodel_lod_range": lod_range,
                    "_dynmodel_node_id": node_id,
                    "_dynmodel_node_name": node_name,
                }
                meshes_out.append(m)

        if not meshes_out:
            self._log(
                f"DynModel {filename}: parsed but produced no meshes"
            )
            return None
        self._log(
            f"DynModel decode hit ({lod_count} LODs, {len(meshes_out)} rigid meshes)"
        )
        return meshes_out

    def parse_file(self, filepath: Path) -> Optional[Mesh]:
        try:
            data = filepath.read_bytes()
        except Exception as exc:
            self.rejections.append(f"{filepath.name}: read failed ({exc})")
            return None

        self._current_file = filepath
        class_id = self._class_id_from_name(filepath.name)
        mesh: Optional[Mesh] = None

        stem = filepath.name.rsplit(".", 1)[0].lower()
        skip_reason = self._file_specific.pre_parse_filter_reason(class_id, stem)
        if skip_reason is not None:
            self.rejections.append(f"{filepath.name}: {skip_reason}")
            return None

        if class_id == "77f8232f":
            ri_meshes = self._decode_77f8232f_rendinst(data, filepath.name)
            if ri_meshes:
                # Stash the full list so parse_directory can emit one Mesh
                # per LOD instead of just LOD0.
                self._rendinst_lod_cache[filepath.name.lower()] = ri_meshes
                mesh = ri_meshes[0]
            else:
                mesh = self._decode_b4b7d9c4(data, filepath.name)
        elif class_id == "b4b7d9c4":
            dyn_meshes = self._decode_b4b7d9c4_dynmodel(data, filepath.name)
            if dyn_meshes:
                self._dynmodel_lod_cache[filepath.name.lower()] = dyn_meshes
                mesh = dyn_meshes[0]
            else:
                mesh = self._decode_b4b7d9c4(data, filepath.name)
        elif class_id == "56f81b6d":
            # Try the proper Dagor GeomNodeTree decoder first; if that
            # detects the format, stash the per-bone meshes and return
            # the first one as a placeholder so parse_directory expands
            # the rest. Fall back to the legacy heuristic decoder for
            # placeholder/tiny skeletons that don't match the
            # GeomNodeTree binary layout.
            bone_meshes = self._decode_geom_node_tree(data, filepath.name)
            if bone_meshes:
                self._skeleton_bone_cache[filepath.name.lower()] = bone_meshes
                mesh = bone_meshes[0]
            else:
                mesh = self._decode_tiny_skeleton_marker(data, filepath.name)
        elif class_id == "ace50000":
            mesh = self._decode_ace50000_collision_mesh(data, filepath.name)
        elif class_id == "d543e771":
            mesh = self._decode_phobj_cbox_wire(data, filepath.name)
        elif class_id in ("4e1d5f5e", "40c586f9"):
            mesh = self._decode_generic(data, filepath.name)

        if mesh:
            mesh = self._normalize_oversized_collision_mesh(filepath.name, mesh)
            self._log(
                f"Parsed {filepath.name}: {mesh['vertex_count']} verts, {mesh['face_count']} faces"
            )
            return mesh

        if self.rejections and self.rejections[-1].startswith(f"{filepath.name}:"):
            return None

        self.rejections.append(f"{filepath.name}: no valid mesh decode")
        return None

    @staticmethod
    def _mesh_extents(mesh: Mesh) -> Tuple[float, float, float]:
        verts = mesh.get("vertices") or []
        if not verts:
            return (0.0, 0.0, 0.0)
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))

    @staticmethod
    def _mesh_base_name(filename: str) -> str:
        """Strip class-id extension and any ``_lodN`` / ``_destr`` suffix."""
        stem = filename.rsplit(".", 1)[0].lower()
        # Strip _lodN suffix added by multi-LOD splitter.
        m = re.match(r"^(.*)_lod\d+$", stem)
        if m:
            stem = m.group(1)
        if stem.endswith("_destr"):
            stem = stem[: -len("_destr")]
        return stem

    def _substitute_collision_for_nonflat(self, meshes: List[Mesh]) -> None:
        """Replace geometry of non-flat rendInst/destr meshes with the
        sibling ``*_collision.ACE50000`` mesh's geometry, in-place.

        Heuristic: we currently approximate rendInst index buffers with a
        2D Delaunay in the XZ plane. That works for flat ground assets
        (lakes, water decals) but is meaningless for vertical structures
        like ``p39a_post`` or ``p39a_railing``. When such a mesh has
        ``y_extent / max(x_extent, z_extent) > 0.2`` AND a collision
        sibling exists in this directory, we copy the collision mesh's
        vertices/faces/lines into the rendInst/destr mesh while keeping
        its filename.
        """
        if not meshes:
            return

        # Build an index of available collision meshes by base stem.
        collision_by_base: Dict[str, Mesh] = {}
        for m in meshes:
            fname = str(m.get("filename", ""))
            low = fname.lower()
            if not low.endswith(".ace50000"):
                continue
            stem = low.rsplit(".", 1)[0]
            if stem.endswith("_collision"):
                base = stem[: -len("_collision")]
                if base.endswith("_destr"):
                    base = base[: -len("_destr")]
                # Prefer the base (non-_destr) collision when both exist.
                collision_by_base.setdefault(base, m)

        if not collision_by_base:
            return

        for m in meshes:
            fname = str(m.get("filename", ""))
            low = fname.lower()
            if not (low.endswith(".77f8232f") or low.endswith(".b4b7d9c4")):
                continue
            verts = m.get("vertices") or []
            if not verts:
                continue
            faces = m.get("faces") or []
            # Heuristic: our XZ-Delaunay triangulation is meaningful only
            # when the projection captures the shape well. When the
            # resulting triangle count is much smaller than the vertex
            # count, the projection collapsed (vertical structure such as
            # railings/posts) — fall back to the collision sibling.
            sparse_faces = len(faces) < max(4, len(verts) * 0.7)
            if not sparse_faces:
                continue

            base = self._mesh_base_name(fname)
            coll = collision_by_base.get(base)
            if coll is None:
                continue

            new_verts = list(coll.get("vertices") or [])
            new_faces = list(coll.get("faces") or [])
            new_lines = list(coll.get("lines") or [])
            # Skip placeholder gyro collisions (lines-only, no faces) —
            # they are visualisation aids, not real geometry, e.g. for
            # bushes whose collision is just an icon billboard.
            if not new_faces:
                continue
            if not new_verts:
                continue

            self._log(
                f"Substituting collision geometry into {fname} "
                f"(from {coll.get('filename')})"
            )
            m["vertices"] = new_verts
            m["faces"] = new_faces
            m["lines"] = new_lines
            m["vertex_count"] = len(new_verts)
            m["face_count"] = len(new_faces)
            m["line_count"] = len(new_lines)
            m["normals"] = None
            m["_collision_substituted_from"] = coll.get("filename")

    def parse_directory(self, dirpath: Path) -> List[Mesh]:
        meshes: List[Mesh] = []
        if not dirpath.exists():
            raise FileNotFoundError(f"Directory not found: {dirpath}")

        files: List[Path] = []
        for p in sorted(dirpath.glob("*")):
            if not p.is_file():
                continue
            class_id = self._class_id_from_name(p.name)
            if len(class_id) == 8 and all(ch in "0123456789abcdef" for ch in class_id):
                files.append(p)
        self._log(f"Found {len(files)} resources")
        # Pre-pass: decode skeletons (56F81B6D) first so the world-tm
        # cache is populated before sibling DynModel resources read it.
        # The full bone Mesh list is stashed so the regular pass below
        # picks it up via ``_skeleton_bone_cache``.
        for fp in files:
            if not fp.name.lower().endswith(".56f81b6d"):
                continue
            try:
                sk_data = fp.read_bytes()
            except Exception:
                continue
            bone_meshes = self._decode_geom_node_tree(sk_data, fp.name)
            if bone_meshes:
                self._skeleton_bone_cache[fp.name.lower()] = bone_meshes
        # Index by lowercase name so the destr-phobj duplication step below
        # can detect missing `_destr_collision` resources cheaply.
        existing_names = {fp.name.lower() for fp in files}
        for fp in files:
            mesh = self.parse_file(fp)
            if mesh:
                # If this is an ACE50000 collision blob carrying multiple
                # named ``*_cls`` sections, replace the concatenated mesh
                # with one mesh per named sub-part. Sub-meshes inherit
                # the collision filename suffix so the exporter splits
                # them cleanly.
                if fp.name.lower().endswith(".ace50000"):
                    try:
                        raw_bytes = fp.read_bytes()
                    except Exception:
                        raw_bytes = b""
                    if raw_bytes:
                        sub = self._try_split_ace50000_named_cls(raw_bytes, fp.name)
                        if sub:
                            self._log(
                                f"Splitting {fp.name} into {len(sub)} named cls sub-meshes"
                            )
                            meshes.extend(sub)
                            continue
                # Multi-LOD rendInst: emit one Mesh per LOD instead of just LOD0.
                if fp.name.lower().endswith(".77f8232f"):
                    ri_subs = self._rendinst_lod_cache.pop(fp.name.lower(), None)
                    if ri_subs and len(ri_subs) >= 2:
                        self._log(
                            f"Splitting {fp.name} into {len(ri_subs)} LOD meshes"
                        )
                        meshes.extend(ri_subs)
                        continue
                # Skeleton (GeomNodeTree): emit one named ``o BONE_NAME``
                # mesh per bone instead of a single combined wireframe.
                if fp.name.lower().endswith(".56f81b6d"):
                    bone_subs = self._skeleton_bone_cache.pop(fp.name.lower(), None)
                    if bone_subs and len(bone_subs) >= 2:
                        self._log(
                            f"Splitting {fp.name} into {len(bone_subs)} named bone meshes"
                        )
                        meshes.extend(bone_subs)
                        continue
                # DynModel (B4B7D9C4): emit one named mesh per
                # (LOD × rigid node) instead of just the first rigid.
                if fp.name.lower().endswith(".b4b7d9c4"):
                    dyn_subs = self._dynmodel_lod_cache.pop(fp.name.lower(), None)
                    if dyn_subs and len(dyn_subs) >= 2:
                        self._log(
                            f"Splitting {fp.name} into {len(dyn_subs)} DynModel rigid meshes"
                        )
                        meshes.extend(dyn_subs)
                        continue
                meshes.append(mesh)
                # Some assets (e.g. lamp_post_metal_350) ship a multi-box
                # `_destr_phobj` but no separate `_destr_collision.ACE50000`.
                # When the phobj decoded into a multi-box wireframe (>=3
                # boxes / >=24 verts), emit a renamed clone as
                # `<stem>_destr_collision` so downstream consumers see the
                # collision shape under its expected name.
                if (
                    fp.name.lower().endswith("_destr_phobj.d543e771")
                    and mesh.get("vertex_count", 0) >= 24
                    and mesh.get("line_count", 0) >= 36
                    and mesh.get("_phobj_body_count", 0) >= 2
                ):
                    stem = fp.stem
                    if stem.lower().endswith("_destr_phobj"):
                        base = stem[: -len("_destr_phobj")]
                        coll_stem = base + "_destr_collision"
                        ace_name = f"{coll_stem}.ACE50000".lower()
                        # Only clone when there is no real collision sibling
                        # at all. Some assets (e.g. p39a_railing) ship with
                        # a base ``<base>_collision.ACE50000`` instead of a
                        # ``<base>_destr_collision.ACE50000``; in that case
                        # the AssetViewer reports `_destr_collision` as
                        # missing, so we must NOT fabricate one.
                        base_ace = f"{base}_collision.ACE50000".lower()
                        if (
                            ace_name not in existing_names
                            and base_ace not in existing_names
                        ):
                            clone = dict(mesh)
                            clone["filename"] = f"{coll_stem}.ACE50000"
                            meshes.append(clone)
                            self._log(
                                f"Emitting derived collision mesh {coll_stem} from {fp.name}"
                            )
        # Post-pass: for non-flat rendInst (77F8232F) and destr (B4B7D9C4)
        # meshes whose triangulation is approximate (we have not yet
        # decoded the index codec), substitute the geometry from a sibling
        # ``*_collision.ACE50000`` mesh when one is available. This makes
        # vertical structures like ``p39a_post`` / ``p39a_railing`` render
        # as their collision shape rather than a Delaunay-XZ blob.
        self._substitute_collision_for_nonflat(meshes)
        return meshes
