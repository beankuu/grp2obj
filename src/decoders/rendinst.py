"""RendInstDecoderMixin: 77F8232F RendInst and Delaunay triangulation."""

import math
import struct
from typing import Dict, List, Optional, Sequence, Tuple

Mesh = Dict[str, object]


class RendInstDecoderMixin:

    _RI_LOD_RECORD_SIZE = 32
    _VDATA_I16 = 0x20
    _VDATA_I32 = 0x40
    _VDATA_PACKED_IB = 0x200
    _VSDT_SIZES = {
        0x00: 4, 0x01: 8, 0x02: 12, 0x03: 16, 0x04: 4, 0x05: 4,
        0x06: 4, 0x07: 8, 0x09: 4, 0x0A: 8, 0x0B: 4, 0x0C: 8,
        0x0D: 4, 0x0E: 4, 0x0F: 4, 0x10: 8,
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

        try:
            rec_off = struct.unpack_from("<I", decoded, 0x10)[0]
            lod_count = struct.unpack_from("<I", decoded, 0x14)[0]
        except struct.error:
            return None
        if not (1 <= lod_count <= 8):
            return None
        if rec_off < 0x18 or rec_off + lod_count * self._RI_LOD_RECORD_SIZE > len(decoded):
            return None

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

        meshes: List[Mesh] = []
        v_off = rec_off + lod_count * self._RI_LOD_RECORD_SIZE
        stem = filename.rsplit(".", 1)[0]

        for lod_idx, (vertNum, vertStride, packedIdxSize, idxSizeBytes, flags) \
                in enumerate(lod_records):
            v_need = vertNum * vertStride
            if v_off + v_need > len(decoded):
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
