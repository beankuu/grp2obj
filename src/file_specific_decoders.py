#!/usr/bin/env python3
"""File-specific and game-asset decoders for GRP converter.

DEPRECATED MODULE: These decoders are kept only for validation/result-checking.
Do NOT expand with new file-specific heuristics. All new decoding should use
generic strategies in grp_converter.py.

This module should eventually be removed as generic decoders mature.
"""

import math
import struct
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

Mesh = Dict[str, object]


class NullFileSpecificDecoder:
    """No-op stand-in for FileSpecificDecoderHelper.

    Used when GRPResourceParser is in default (non-comparison) mode. Every
    method returns the structurally-neutral value (False / None / passthrough),
    so the main converter path is purely structural with no filename
    heuristics or hand-tuned per-asset overrides.
    """

    def __init__(self, parent_parser: "GRPResourceParser") -> None:
        self.parser = parent_parser

    def pre_parse_filter_reason(self, class_id: str, stem: str) -> Optional[str]:
        return None

    def prefer_wireframe_collision(self, filename: str) -> bool:
        return False

    def is_vietnam_rocks_collision(self, filename: str) -> bool:
        return False

    def filter_wireframe_lines_for_readability(
        self,
        filename: str,
        verts: Sequence[Tuple[float, float, float]],
        lines: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        return list(lines)

    def is_bush_marine_collision(self, filename: str) -> bool:
        return False

    def decode_bush_marine_collision_wire(self, payload: bytes, filename: str) -> Optional[Mesh]:
        return None

    def should_skip_bush_marine_visual(self, filename: str) -> bool:
        return False

    def build_axis_rings_mesh(self, *args, **kwargs) -> Optional[Mesh]:
        return None

    def is_tropical_arctium_collision(self, filename: str) -> bool:
        return False

    def decode_tropical_arctium_collision(self, filename: str) -> Optional[Mesh]:
        return None

    def try_special_ace50000_override(self, filename: str) -> Optional[Mesh]:
        return None

    def is_lake_collision(self, filename: str) -> bool:
        return False

    def decode_lake_collision_planar(self, data: bytes, filename: str) -> Optional[Mesh]:
        return None

    def is_oreshek_collision(self, filename: str) -> bool:
        return False

    def postprocess_oreshek_indexed(
        self,
        filename: str,
        vertices: List[Tuple[float, float, float]],
        faces: List[Tuple[int, int, int]],
    ) -> Tuple[List[Tuple[float, float, float]], List[Tuple[int, int, int]]]:
        return vertices, faces


class FileSpecificDecoderHelper:
    """Helper methods extracted from GRPResourceParser for organizational clarity."""

    def __init__(self, parent_parser: "GRPResourceParser") -> None:
        self.parser = parent_parser

    def _log(self, msg: str) -> None:
        if self.parser.verbose:
            print(f"[GRP-FileSpecific] {msg}")

    # ========== PRE-PARSE FILTERS ==========

    def pre_parse_filter_reason(self, class_id: str, stem: str) -> Optional[str]:
        """Return file-specific skip reason for resources not meant for export."""
        if class_id == "77f8232f" and "lake" in stem:
            return "water surface marker (no geometry)"
        if class_id == "77f8232f" and self.should_skip_bush_marine_visual(stem):
            return "visual mesh (B4 visual-only, no OBJ export)"
        if class_id == "03fb59c4":
            return "vegetation/composition asset (not exportable)"
        return None

    # ========== VIETNAM ROCKS COLLISION FUNCTIONS ==========

    def prefer_wireframe_collision(self, filename: str) -> bool:
        if "_collision" not in filename.lower():
            return False
        if self.parser._current_file is None:
            return False
        p = str(self.parser._current_file.parent).lower()
        if "gm_logic__grp_extract" in p:
            return True
        if "vietnam_rocks__grp_extract" in p and "vietnam_rock" in filename.lower():
            return True
        return False

    def is_vietnam_rocks_collision(self, filename: str) -> bool:
        if "_collision" not in filename.lower():
            return False
        if "vietnam_rock" not in filename.lower():
            return False
        if self.parser._current_file is None:
            return False
        p = str(self.parser._current_file.parent).lower()
        return "vietnam_rocks__grp_extract" in p

    def filter_wireframe_lines_for_readability(
        self,
        filename: str,
        vertices: Sequence[Tuple[float, float, float]],
        lines: Sequence[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        """Vietnam rock collisions decode into valid topology but include a tail of
        very long cross-surface edges that render as a "hairball" in viewers."""
        if not self.is_vietnam_rocks_collision(filename):
            return list(lines)
        if len(lines) < 24 or len(vertices) < 8:
            return list(lines)

        lengths: List[float] = []
        for a, b in lines:
            if not (0 <= a < len(vertices) and 0 <= b < len(vertices)):
                lengths.append(float("inf"))
                continue
            ax, ay, az = vertices[a]
            bx, by, bz = vertices[b]
            dx, dy, dz = (ax - bx), (ay - by), (az - bz)
            ln = math.sqrt(dx * dx + dy * dy + dz * dz)
            lengths.append(ln)

        finite = sorted(ln for ln in lengths if math.isfinite(ln) and ln > 0.0)
        if not finite:
            return list(lines)
        median = finite[len(finite) // 2]
        cap = max(median * 2.6, median + 1.0)

        filtered: List[Tuple[int, int]] = []
        for edge, ln in zip(lines, lengths):
            if math.isfinite(ln) and ln <= cap:
                filtered.append(edge)

        min_keep = max(24, len(lines) // 5)
        if len(filtered) < min_keep:
            return list(lines)

        if self.parser.verbose and len(filtered) != len(lines):
            self._log(f"Wireframe edge filter (vietnam): kept {len(filtered)}/{len(lines)} lines (median={median:.3f}, cap={cap:.3f})")
        return filtered

    # ========== BUSH_MARINE COLLISION FUNCTIONS ==========

    def is_bush_marine_collision(self, filename: str) -> bool:
        """Check if this is a bush_marine collision resource."""
        name = filename.lower()
        return "marine_bush" in name and "_collision" in name

    def decode_bush_marine_collision_wire(self, payload: bytes, filename: str) -> Optional[Mesh]:
        """DEPRECATED: File-specific bush_marine collision decode. Use for validation only.
        
        Bush marine collisions encode AABB (axis-aligned bounding box) as dual-ring
        gimbal wireframe (2 concentric rings + connecting segments for 3D visualization).
        """
        if not self.is_bush_marine_collision(filename):
            return None
        if len(payload) < 32 or len(payload) > 512:
            return None

        try:
            # Parse AABB: mins at offset 0-11 (3x float32), maxs at offset 16-27 (3x float32)
            min_x = struct.unpack("<f", payload[0:4])[0]
            min_y = struct.unpack("<f", payload[4:8])[0]
            min_z = struct.unpack("<f", payload[8:12])[0]
            max_x = struct.unpack("<f", payload[16:20])[0]
            max_y = struct.unpack("<f", payload[20:24])[0]
            max_z = struct.unpack("<f", payload[24:28])[0]
        except struct.error:
            return None

        if not all(math.isfinite(c) for c in (min_x, min_y, min_z, max_x, max_y, max_z)):
            return None
        if max_x <= min_x or max_y <= min_y or max_z <= min_z:
            return None

        # Build dual-ring gimbal wireframe:
        # Ring 1: at min_y (8 vertices around XZ box)
        # Ring 2: at max_y (8 vertices around XZ box)
        # Connect corresponding vertices between rings
        cx = (min_x + max_x) / 2
        cz = (min_z + max_z) / 2
        rx = (max_x - min_x) / 2
        rz = (max_z - min_z) / 2

        vertices: List[Tuple[float, float, float]] = []
        
        # Inner ring at min_y
        for i in range(8):
            angle = (i / 8.0) * 2 * math.pi
            x = cx + rx * math.cos(angle)
            z = cz + rz * math.sin(angle)
            vertices.append((x, min_y, z))

        # Outer ring at max_y
        for i in range(8):
            angle = (i / 8.0) * 2 * math.pi
            x = cx + rx * math.cos(angle)
            z = cz + rz * math.sin(angle)
            vertices.append((x, max_y, z))

        # Build edges: ring 1, ring 2, and connecting segments
        lines: List[Tuple[int, int]] = []
        
        # Ring 1 edges
        for i in range(8):
            lines.append((i, (i + 1) % 8))
        
        # Ring 2 edges
        for i in range(8):
            lines.append((8 + i, 8 + (i + 1) % 8))
        
        # Connecting segments
        for i in range(8):
            lines.append((i, 8 + i))

        mesh = self.parser._build_mesh(filename, vertices, faces=[], lines=lines)
        if mesh:
            self._log(
                f"Bush marine collision wireframe decoded ({len(vertices)} vertices, {len(lines)} lines)"
            )
            return mesh
        return None

    def should_skip_bush_marine_visual(self, filename: str) -> bool:
        """Check if this is a bush_marine visual mesh that should be skipped.
        
        Bush marine visual meshes (.77F8232F with "marine_bush" in name) use complex
        B4 encoding and are not exportable to OBJ. Only collision meshes (.ACE50000) should be exported.
        """
        name = filename.lower()
        return "marine_bush" in name

    def build_axis_rings_mesh(
        self,
        filename: str,
        radius: float = 0.025,
        segments: int = 24,
        wireframe: bool = False,
    ) -> Optional[Mesh]:
        """DEPRECATED: File-specific axis-rings mesh builder for skeleton markers and similar assets.
        
        Used for water_decals, tropical_arctium, and similar procedural wireframe geometries.
        """
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
                    base_a = base + i * 2
                    base_b = base + ni * 2
                    faces.append((base_a, base_a + 1, base_b + 1))
                    faces.append((base_a, base_b + 1, base_b))

        add_ring("xy")
        add_ring("xz")
        add_ring("yz")

        return self.parser._build_mesh(filename, vertices, faces=faces, lines=lines)

    def is_tropical_arctium_collision(self, filename: str) -> bool:
        """Check if this is a tropical_arctium collision resource."""
        name = filename.lower()
        return "tropical_arctium" in name and "_collision" in name

    def decode_tropical_arctium_collision(self, filename: str) -> Optional[Mesh]:
        """DEPRECATED: File-specific tropical_arctium collision. Use for validation only.
        
        Tropical arctium collision proxies are expected to be the same visual topology
        as water_decals wave_a_skeleton (axis-rings wireframe).
        """
        if not self.is_tropical_arctium_collision(filename):
            return None
        
        mesh = self.build_axis_rings_mesh(filename, radius=0.025, segments=24, wireframe=True)
        if mesh:
            self._log("Tropical arctium axis-rings wireframe decoded")
        return mesh

    def try_special_ace50000_override(self, filename: str) -> Optional[Mesh]:
        """File-specific ACE50000 early override hook."""
        return self.decode_tropical_arctium_collision(filename)

    # ========== LAKE PLANAR DECODER (file-specific, comparison-only) ==========

    def is_lake_collision(self, filename: str) -> bool:
        return "lake" in filename.lower()

    def is_oreshek_collision(self, filename: str) -> bool:
        name = filename.lower()
        return "fortress_oreshek" in name and "_collision" in name

    def postprocess_oreshek_indexed(
        self,
        filename: str,
        vertices: List[Tuple[float, float, float]],
        faces: List[Tuple[int, int, int]],
    ) -> Tuple[List[Tuple[float, float, float]], List[Tuple[int, int, int]]]:
        if not self.is_oreshek_collision(filename):
            return vertices, faces
        vertices, faces = self.parser._keep_largest_face_component(vertices, faces)
        faces = [
            f
            for f in faces
            if self.parser._triangle_area(vertices[f[0]], vertices[f[1]], vertices[f[2]]) > 1e-8
        ]
        pruned = self.parser._filter_spike_faces(vertices, faces, edge_mult=7.0)
        if len(pruned) != len(faces):
            self._log(f"Ladoga face prune: {len(faces)} -> {len(pruned)}")
            faces = pruned
        return vertices, faces

    def decode_lake_collision_planar(self, data: bytes, filename: str) -> Optional[Mesh]:
        """File-specific planar/ring fallback for lake collision ACE50000 payloads."""
        if not self.is_lake_collision(filename):
            return None
        if len(data) < 36:
            return None

        MAX_ABS_COORD = self.parser.MAX_ABS_COORD
        max_start = min(32, max(len(data) - 36, 0))

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

        best_lake: Optional[Tuple[float, int, str, List[Tuple[float, float, float]]]] = None

        def _ring_candidate(verts_in: Sequence[Tuple[float, float, float]]) -> Optional[List[Tuple[float, float, float]]]:
            if len(verts_in) < 8:
                return None
            verts_ok = [
                v
                for v in verts_in
                if all(math.isfinite(c) and abs(c) <= MAX_ABS_COORD for c in v)
            ]
            if len(verts_ok) < 8:
                return None
            ys = sorted(v[1] for v in verts_ok)
            y_mid = ys[len(ys) // 2]
            ring = [v for v in verts_ok if abs(v[1] - y_mid) <= 0.5]
            if len(ring) < 8:
                return None
            seen = set()
            dedup: List[Tuple[float, float, float]] = []
            for v in ring:
                key = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(v)
            if len(dedup) < 6:
                return None
            cx0 = sum(v[0] for v in dedup) / len(dedup)
            cz0 = sum(v[2] for v in dedup) / len(dedup)
            d = [math.hypot(v[0] - cx0, v[2] - cz0) for v in dedup]
            d_sorted = sorted(d)
            d_med = d_sorted[len(d_sorted) // 2]
            d_cap = max(1.0, d_med * 3.5)
            pruned = [v for v, dv in zip(dedup, d) if dv <= d_cap]
            if len(pruned) >= 6:
                dedup = pruned
            return dedup if len(dedup) >= 6 else None

        def _score_ring(ring: Sequence[Tuple[float, float, float]]) -> float:
            xs = [v[0] for v in ring]
            ys = [v[1] for v in ring]
            zs = [v[2] for v in ring]
            x_sp = max(xs) - min(xs)
            y_sp = max(ys) - min(ys)
            z_sp = max(zs) - min(zs)
            abs_max = max(max(abs(x) for x in xs), max(abs(z) for z in zs))
            min_sp = max(1e-6, min(x_sp, z_sp))
            ratio = max(x_sp, z_sp) / min_sp
            return (
                (x_sp + z_sp) * 0.01
                - y_sp * 1000.0
                - max(0.0, abs_max - 1000.0) * 0.002
                - max(0.0, ratio - 4.0) * 10.0
                + len(ring) * 0.4
            )

        def _resample_closed_loop(
            verts_in: Sequence[Tuple[float, float, float]], target_n: int
        ) -> List[Tuple[float, float, float]]:
            if target_n <= 0 or len(verts_in) < 3:
                return list(verts_in)
            loop = list(verts_in) + [verts_in[0]]
            seg_len: List[float] = []
            for i in range(len(verts_in)):
                ax, ay, az = loop[i]
                bx, by, bz = loop[i + 1]
                seg_len.append(math.sqrt((bx - ax) ** 2 + (by - ay) ** 2 + (bz - az) ** 2))
            total_len = sum(seg_len)
            if total_len <= 1e-9:
                return list(verts_in)
            cum: List[float] = [0.0]
            for ln in seg_len:
                cum.append(cum[-1] + ln)
            out: List[Tuple[float, float, float]] = []
            for k in range(target_n):
                t = (total_len * k) / float(target_n)
                si = 0
                while si + 1 < len(cum) and cum[si + 1] < t:
                    si += 1
                if si >= len(verts_in):
                    si = len(verts_in) - 1
                a = loop[si]
                b = loop[si + 1]
                span = max(1e-9, cum[si + 1] - cum[si])
                u = (t - cum[si]) / span
                out.append(
                    (
                        a[0] + (b[0] - a[0]) * u,
                        a[1] + (b[1] - a[1]) * u,
                        a[2] + (b[2] - a[2]) * u,
                    )
                )
            return out

        for vstart in range(0, max_start + 1, 4):
            raw = _parse_vertices(data[vstart:])
            if not raw:
                continue
            ring = _ring_candidate(raw)
            if not ring:
                continue
            s = _score_ring(ring)
            if best_lake is None or s > best_lake[0]:
                best_lake = (s, vstart, "stride12", ring)

        stride16_maps: List[Tuple[int, int, int]] = [(0, 1, 3), (2, 1, 3), (3, 1, 2)]
        for vstart in range(0, max_start + 1, 4):
            if len(data) - vstart < 16 * 8:
                continue
            rows: List[Tuple[float, float, float, float]] = []
            ok = True
            for i in range(vstart, len(data) - 15, 16):
                try:
                    a, b, c, d = struct.unpack_from("<ffff", data, i)
                except struct.error:
                    ok = False
                    break
                if not all(math.isfinite(v) for v in (a, b, c, d)):
                    ok = False
                    break
                rows.append((a, b, c, d))
            if not ok or len(rows) < 8:
                continue
            for m in stride16_maps:
                mapped = [(r[m[0]], r[m[1]], r[m[2]]) for r in rows]
                ring = _ring_candidate(mapped)
                if not ring:
                    continue
                s = _score_ring(ring)
                if best_lake is None or s > best_lake[0]:
                    best_lake = (s, vstart, f"stride16-map{m}", ring)

        if best_lake is None:
            return None

        _, best_start, best_mode, ring_best = best_lake

        pts2 = [(v[0], v[2], i) for i, v in enumerate(ring_best)]
        pts2_sorted = sorted(pts2, key=lambda p: (p[0], p[1]))

        def _cross(o: Tuple[float, float, int], a: Tuple[float, float, int], b: Tuple[float, float, int]) -> float:
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        lower: List[Tuple[float, float, int]] = []
        for p in pts2_sorted:
            while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0.0:
                lower.pop()
            lower.append(p)
        upper: List[Tuple[float, float, int]] = []
        for p in reversed(pts2_sorted):
            while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0.0:
                upper.pop()
            upper.append(p)

        hull = lower[:-1] + upper[:-1]
        hull_idx = [p[2] for p in hull]
        ring_hull = [ring_best[i] for i in hull_idx]

        if len(ring_hull) < 16 and len(ring_best) >= 16:
            cx = sum(v[0] for v in ring_best) / len(ring_best)
            cz = sum(v[2] for v in ring_best) / len(ring_best)
            bin_count = min(24, max(16, len(ring_best)))
            bins: List[Optional[Tuple[float, float, float, float]]] = [None] * bin_count
            for vx, vy, vz in ring_best:
                ang = math.atan2(vz - cz, vx - cx)
                if ang < 0.0:
                    ang += 2.0 * math.pi
                bi = min(bin_count - 1, int((ang / (2.0 * math.pi)) * bin_count))
                r = math.hypot(vx - cx, vz - cz)
                prev = bins[bi]
                if prev is None or r > prev[0]:
                    bins[bi] = (r, vx, vy, vz)
            sampled: List[Tuple[float, float, float]] = []
            for rec in bins:
                if rec is None:
                    continue
                _, vx, vy, vz = rec
                sampled.append((vx, vy, vz))
            if len(sampled) >= 12:
                seen_s = set()
                sampled_dedup: List[Tuple[float, float, float]] = []
                for v in sampled:
                    k = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
                    if k in seen_s:
                        continue
                    seen_s.add(k)
                    sampled_dedup.append(v)
                if len(sampled_dedup) >= 12:
                    ring_hull = sampled_dedup

        if len(ring_hull) >= 6:
            ring_sorted = ring_hull
        else:
            cx = sum(v[0] for v in ring_best) / len(ring_best)
            cz = sum(v[2] for v in ring_best) / len(ring_best)
            ring_sorted = sorted(
                ring_best,
                key=lambda v: math.atan2(v[2] - cz, v[0] - cx),
            )

        out_verts: List[Tuple[float, float, float]] = ring_sorted

        if len(out_verts) >= 6 and len(out_verts) < 20:
            target_n = min(24, max(20, len(out_verts)))
            resampled = _resample_closed_loop(out_verts, target_n)
            if len(resampled) >= 12:
                out_verts = resampled

        def _poly_area_xz(poly: Sequence[Tuple[float, float, float]]) -> float:
            s = 0.0
            for i in range(len(poly)):
                x1, _, z1 = poly[i]
                x2, _, z2 = poly[(i + 1) % len(poly)]
                s += x1 * z2 - x2 * z1
            return 0.5 * s

        def _cross_xz(a: int, b: int, c: int, poly: Sequence[Tuple[float, float, float]]) -> float:
            ax, _, az = poly[a]
            bx, _, bz = poly[b]
            cx, _, cz = poly[c]
            return (bx - ax) * (cz - az) - (bz - az) * (cx - ax)

        def _point_in_tri_xz(
            p: Tuple[float, float],
            a: Tuple[float, float],
            b: Tuple[float, float],
            c: Tuple[float, float],
        ) -> bool:
            def sgn(p1: Tuple[float, float], p2: Tuple[float, float], p3: Tuple[float, float]) -> float:
                return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
            d1 = sgn(p, a, b)
            d2 = sgn(p, b, c)
            d3 = sgn(p, c, a)
            has_neg = (d1 < 0.0) or (d2 < 0.0) or (d3 < 0.0)
            has_pos = (d1 > 0.0) or (d2 > 0.0) or (d3 > 0.0)
            return not (has_neg and has_pos)

        tri_verts: List[Tuple[float, float, float]] = list(out_verts)
        if _poly_area_xz(tri_verts) < 0.0:
            tri_verts.reverse()

        idx = list(range(len(tri_verts)))
        lake_faces: List[Tuple[int, int, int]] = []
        guard = 0
        max_guard = len(idx) * len(idx)
        eps = 1e-9
        while len(idx) > 3 and guard < max_guard:
            guard += 1
            ear_found = False
            m = len(idx)
            for i in range(m):
                i_prev = idx[(i - 1) % m]
                i_curr = idx[i]
                i_next = idx[(i + 1) % m]
                if _cross_xz(i_prev, i_curr, i_next, tri_verts) <= eps:
                    continue
                ax, _, az = tri_verts[i_prev]
                bx, _, bz = tri_verts[i_curr]
                cx, _, cz = tri_verts[i_next]
                tri2 = ((ax, az), (bx, bz), (cx, cz))
                contains_other = False
                for j in idx:
                    if j in (i_prev, i_curr, i_next):
                        continue
                    px, _, pz = tri_verts[j]
                    if _point_in_tri_xz((px, pz), tri2[0], tri2[1], tri2[2]):
                        contains_other = True
                        break
                if contains_other:
                    continue
                lake_faces.append((i_prev, i_curr, i_next))
                del idx[i]
                ear_found = True
                break
            if not ear_found:
                break
        if len(idx) == 3:
            lake_faces.append((idx[0], idx[1], idx[2]))

        if len(lake_faces) == 0 and len(tri_verts) >= 3:
            for i in range(1, len(tri_verts) - 1):
                lake_faces.append((0, i, i + 1))

        if len(lake_faces) >= 3:
            deg = [0] * len(tri_verts)
            for a, b, c in lake_faces:
                deg[a] += 1
                deg[b] += 1
                deg[c] += 1
            max_deg = max(deg)
            if max_deg > max(8, int(len(tri_verts) * 0.45)):
                balanced: List[Tuple[int, int, int]] = []

                def _triangulate_span(lo: int, hi: int) -> None:
                    if hi - lo < 2:
                        return
                    mid = (lo + hi) // 2
                    balanced.append((lo, mid, hi))
                    _triangulate_span(lo, mid)
                    _triangulate_span(mid, hi)

                _triangulate_span(0, len(tri_verts) - 1)
                if len(balanced) == len(tri_verts) - 2:
                    lake_faces = balanced

        mesh = self.parser._build_mesh(filename, tri_verts, faces=lake_faces, lines=[])
        if mesh:
            self._log(
                f"ACE50000 collision decode hit (planar-lake, mode={best_mode}, start={best_start}, {len(tri_verts)} vertices, {len(lake_faces)} faces)"
            )
        return mesh

