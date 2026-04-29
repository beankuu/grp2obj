"""VertexStreamMixin: vertex/index stream decoders and B4B7D9C4 probing."""

import lzma
import math
import struct
from typing import Dict, List, Optional, Sequence, Tuple

Mesh = Dict[str, object]


class VertexStreamMixin:

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
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                if in_real_region:
                    break
                continue
            if max(abs(x), abs(y), abs(z)) > self.MAX_ABS_COORD:
                if in_real_region:
                    break
                continue
            mag = max(abs(x), abs(y), abs(z))
            if not in_real_region:
                if mag < 1e-4:
                    continue
                in_real_region = True
            elif mag < 1e-10:
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
        starts: List[int] = []
        around = max(0, scan_start_hint - 0x20000)
        while around < min(scan_start_hint + 0x40000, len(blob) - 6):
            starts.append(around)
            around += 2
        coarse = 0
        coarse_step = 128
        while coarse < max(search_end - 6, 0):
            starts.append(coarse)
            coarse += coarse_step

        max_starts = 12000
        if len(starts) > max_starts:
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

        # Strategy "decal": uncompressed RendInst decal record.
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
                        mesh = self._build_grid_plane_mesh(filename, mins, maxs)
                        if mesh:
                            self._log(
                                f"B4 decode hit (decal-quad, "
                                f"size={extents[0]:.2f}x{extents[1]:.2f}x{extents[2]:.2f})"
                            )
                            return mesh

        # Strategy 0: confirmed layout for cockpit B4B7D9C4 blobs.
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

                    # Strategy 0a
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

                    # Strategy 0b
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

                    # Heuristic fallback
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

                    # Strategy 0c: brute-force fp16 stride-16 scan.
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

        # Strategy 1
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

        # Strategy 2
        raw_size_hint = struct.unpack_from("<I", data, 4)[0]
        if 256 <= raw_size_hint <= 128 * 1024 * 1024:
            dec = self.oodle.decompress(data[8:], raw_size_hint)
            if dec:
                for stride in (12, 16, 20, 24, 28, 32):
                    mesh = self._parse_vertex_block(dec, filename, head_vcount if head_vcount < 2000000 else None, stride)
                    if mesh:
                        self._log(f"B4 decode hit (strategy2, stride={stride})")
                        return mesh

        # Strategy 3
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

        # Strategy 4: uncompressed B4 with vertex region at a fixed header offset.
        best_mesh2: Optional[Mesh] = None
        best_score2 = -1.0
        best_start2 = 0
        best_stride2 = 0
        for start in (0x40, 0x60, 0x70, 0x80, 0x84, 0x90, 0xa0, 0xb0,
                      0x180, 0x190, 0x1a0, 0x1ac, 0x1b0):
            if start >= len(data):
                continue
            for stride in (12, 16, 20, 24, 28, 32):
                mesh = self._parse_vertex_block(data[start:], filename, None, stride)
                if not mesh:
                    continue
                score = self._mesh_shape_score(mesh)
                if score > best_score2:
                    best_score2 = score
                    best_mesh2 = mesh
                    best_start2 = start
                    best_stride2 = stride
        if best_mesh2 is not None:
            self._log(
                f"B4 decode hit (strategy4 uncompressed-scan, start={best_start2:#x}, "
                f"stride={best_stride2}, verts={best_mesh2['vertex_count']})"
            )
            return best_mesh2

        return None
