"""CollisionDecoderMixin: ACE50000 and related collision mesh decoders."""

import math
import re
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

Mesh = Dict[str, object]


class CollisionDecoderMixin:

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
