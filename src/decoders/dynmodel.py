"""DynModelDecoderMixin: Dagor DynamicRenderableSceneLodsResource decoder."""

import math
import struct
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Tuple

Mesh = Dict[str, object]


class DynModelDecoderMixin:
    if TYPE_CHECKING:
        oodle: Any
        _skeleton_wtm_cache: Dict[str, Dict[str, Tuple[float, ...]]]
        _VDATA_PACKED_IB: int
        _VDATA_I32: int
        _VSDT_SIZES: Dict[int, int]

        def _log(self, msg: str) -> None: ...

        def _zstd_decompress(self, payload: bytes) -> Optional[bytes]: ...

        @staticmethod
        def _decode_meshopt_index_sequence(
            buf: bytes, index_count: int
        ) -> Optional[List[int]]: ...

    def _decode_b4b7d9c4_dynmodel(
        self, data: bytes, filename: str
    ) -> Optional[List[Mesh]]:
        """Decode a Dagor DynamicRenderableSceneLodsResource (class
        id ``B4B7D9C4``) into one Mesh per (LOD, rigid-node) pair.

        Wire format (matches ``DynamicRenderableSceneLodsResource::loadResource``
        in ``D:\\DagorEngine\\prog\\engine\\shaders\\dynSceneRes.cpp``):

          [00] u32 res_sz                    -- LodsResource dump size
          [04] 4xu32 matVdata header
                 (tex_count|0xFFFFFFFF, mat_count|0xFFFFFFFF,
                  vdata_count, hdrSz_with_compr_flags)
          if non-sentinel: u32 tex_pool_size + tex_pool_size bytes
          loadMatVdata:
            u32 block_hdr (tag=high2, length=low30)
            block_body (zstd/oodle compressed):
              hdrSz bytes  PatchableTab<mat>(16)+PatchableTab<vdata>(16)
                           + N*VdataHdr(32) + per-vdata vDecl payload
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
        if res_sz < 16 or res_sz > 4096:
            return None
        if vdata_count == 0 or vdata_count > 64:
            return None

        pos = 20

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

        if not (t0 == 0xFFFFFFFF and t1 == 0xFFFFFFFF):
            if pos + 4 > len(data):
                return None
            tex_sz = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if tex_sz > 0:
                if pos + tex_sz > len(data):
                    return None
                pos += tex_sz

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
        pos = block_end

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

        fpos = matVdataHdrSz
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

        meshes_out: List[Mesh] = []
        stem = filename.rsplit(".", 1)[0]
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
                    vstart = baseVertex + sv
                    if numv <= 0 or vstart < 0 or vstart + numv > vert_num:
                        continue
                    local_verts: List[Tuple[float, float, float]] = []
                    ok = True
                    if pos_vsdt == 0x02:  # VSDT_FLOAT3
                        pos_size = 12
                    elif pos_vsdt == 0x0A:  # VSDT_SHORT4N (compressed)
                        pos_size = 8
                    else:
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
                if wtm_lookup is not None and node_name not in ("root", "@root"):
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
