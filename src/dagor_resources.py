"""Structural decoders for Dagor compiled game-resource binaries.

The GRP files used by War Thunder Dagor contain `dabuild`-compiled game
resources, NOT editor `.dag` files. Each resource is identified by a class id
in the filename suffix (e.g. `*.ACE50000`, `*.77F8232F`).

This module reverse-engineers the *outer* container headers structurally so
the rest of the converter can stop relying on filename heuristics. Inner
mesh decoding still happens elsewhere — this only normalizes the payload
boundary (and transparently zstd-decompresses when needed).

Supported headers in this file:

ACE50000 (CollisionRes), 8-byte header:
    u32 magic              = 0xACE50001  (LE bytes: 01 00 E5 AC)
    u32 size_with_flags    bits 0..29 = payload size in bytes
                           bit  30    = Zstd-compressed payload (magic 28 B5 2F FD)
                           bit  31    = (unused in observed samples)

77F8232F (RendInst), 16-byte preamble:
    u32 hdr_size_or_flags  low 24 bits = header table size, high byte = flags
    u32 0xFFFFFFFF
    u32 0xFFFFFFFF
    u32 lod_count          (observed 2..4)

The (decompressed) inner payload layouts for CollisionRes / RendInst are not
publicly documented; the bulk of the converter's existing logic continues to
do the inner mesh decoding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

try:
    import zstandard as _zstd
except Exception:  # pragma: no cover - optional dependency
    _zstd = None


# --- magics ---------------------------------------------------------------

ACE50000_MAGIC = 0xACE50001  # CollisionRes container magic+version, LE 01 00 E5 AC
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DAG_EDITOR_MAGIC = 0x1A474144  # "DAG\x1A" — *editor* format, not used by GRPs

# Payload-size flag bits inside the ACE50000 size_with_flags u32.
ACE50000_FLAG_COMPRESSED = 0x40000000
ACE50000_SIZE_MASK = 0x3FFFFFFF


# --- ACE50000 (CollisionRes) ---------------------------------------------

@dataclass
class CollisionResHeader:
    magic: int
    size_with_flags: int
    payload_size: int
    compressed: bool

    @property
    def is_valid(self) -> bool:
        return self.magic == ACE50000_MAGIC and self.payload_size > 0


def parse_ace50000_header(data: bytes) -> Optional[CollisionResHeader]:
    """Parse the 8-byte CollisionRes (ACE50000) header.

    Returns None if the buffer is too small or magic does not match.
    """
    if len(data) < 8:
        return None
    magic, sf = struct.unpack_from("<II", data, 0)
    if magic != ACE50000_MAGIC:
        return None
    return CollisionResHeader(
        magic=magic,
        size_with_flags=sf,
        payload_size=sf & ACE50000_SIZE_MASK,
        compressed=bool(sf & ACE50000_FLAG_COMPRESSED),
    )


def decode_ace50000_payload(data: bytes) -> Optional[bytes]:
    """Strip the CollisionRes header and zstd-decompress when needed.

    Returns the raw inner payload bytes (the data the rest of the converter
    needs to interpret as collision mesh content), or ``None`` if the header
    is missing/invalid or decompression fails.
    """
    hdr = parse_ace50000_header(data)
    if hdr is None or not hdr.is_valid:
        return None

    body = data[8 : 8 + hdr.payload_size]
    if len(body) < hdr.payload_size:
        # Some files have a short tail pad; tolerate truncation.
        body = data[8:]

    if not hdr.compressed:
        return body

    if not body.startswith(ZSTD_MAGIC):
        return None

    if _zstd is None:
        return None

    try:
        dctx = _zstd.ZstdDecompressor()
        return dctx.decompress(body)
    except Exception:
        # Some Dagor zstd streams are written without a content-size frame
        # field; fall back to streaming decode with a generous output cap.
        try:
            dctx = _zstd.ZstdDecompressor()
            return dctx.decompress(body, max_output_size=64 * 1024 * 1024)
        except Exception:
            return None


# --- 77F8232F (RendInst) --------------------------------------------------

@dataclass
class RendInstHeader:
    hdr_size: int
    flags: int
    lod_count: int

    @property
    def is_plausible(self) -> bool:
        # Sentinels at offsets 4..12 must be 0xFFFFFFFF in observed samples.
        return 1 <= self.lod_count <= 16 and self.hdr_size >= 0x10


def parse_rendinst_preamble(data: bytes) -> Optional[RendInstHeader]:
    """Parse the 16-byte RendInst (77F8232F) preamble."""
    if len(data) < 16:
        return None
    hdr_word, s0, s1, lod_count = struct.unpack_from("<IIII", data, 0)
    if s0 != 0xFFFFFFFF or s1 != 0xFFFFFFFF:
        return None
    flags = (hdr_word >> 24) & 0xFF
    hdr_size = hdr_word & 0x00FFFFFF
    out = RendInstHeader(hdr_size=hdr_size, flags=flags, lod_count=lod_count)
    return out if out.is_plausible else None


# --- generic dispatcher ---------------------------------------------------

def looks_like_dag_editor_file(data: bytes) -> bool:
    """Return True iff ``data`` starts with the DAG editor magic.

    GRP payloads have never been observed to start with this magic; this
    helper exists so callers can confirm that fact before falling back to
    structural decoders.
    """
    if len(data) < 4:
        return False
    return struct.unpack_from("<I", data, 0)[0] == DAG_EDITOR_MAGIC
