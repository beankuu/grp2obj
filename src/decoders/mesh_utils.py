"""MeshBuilderMixin: mesh construction and validation helpers."""

import math
import struct
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

Mesh = Dict[str, object]


class MeshBuilderMixin:

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
