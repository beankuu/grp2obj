"""SkeletonDecoderMixin: Dagor GeomNodeTree, tiny skeleton, and phobj decoders."""

import math
import re
import struct
from typing import Dict, List, Optional, Set, Tuple

Mesh = Dict[str, object]


class SkeletonDecoderMixin:

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
