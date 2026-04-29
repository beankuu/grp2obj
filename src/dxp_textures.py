"""DxP2/DDSx texture extraction.

Unpacks WarThunder *.dxp.bin texture archives and decodes the contained
*.ddsx textures into standard *.dds files.

Container layout (DxP2) per kotiq/wt-tools `dxp_unpack.py`:
    +0x00  '4s'  magic                     'DxP2'
    +0x04  '4B'  version                   (unused)
    +0x08  'H'   total file count
    +0x0C  ...
    +0x10  'I'   block_4 offset (rel +0x10)
    +0x20  'I'   dds_block offset (rel +0x10)  -- 0x20 bytes/file (DDSx hdr)
    +0x30  'I'   block_3 offset (rel +0x10)    -- 0x18 bytes/file
    +0x48        file-name table (NUL-terminated, '*' suffix on each name)

DDSx header layout per Dagor source (also in wt-tools `formats/ddsx_parser.py`):
    +0x00 4s  label 'DDSx'
    +0x04 4s  d3dFormat fourCC ('DXT1', 'DXT5', ...)
    +0x08 I   flags (top byte encodes compression: 0x60=oodle, 0x20=zstd,
                    0x40=lzma, 0x80=zlib, 0x00=raw)
    +0x0C HH  width, height
    +0x10 BB  levels, hqPartLevels
    +0x12 H   depth
    +0x14 H   bitsPerPixel
    +0x16 BB  qmip-bits / dxtShift packing
    +0x18 I   memSz   (uncompressed body size)
    +0x1C I   packedSz (compressed body size; 0 means raw)
    +0x20 ... body
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence


DXP2_MAGIC = b"DxP2"
DDSX_MAGIC = b"DDSx"

# Compression code lives in the high byte of the flags word (offset 0x0B).
COMPRESSION_NAMES = {
    0x00: "raw",
    0x20: "zstd",
    0x40: "lzma",
    0x60: "oodle",
    0x80: "zlib",
}

# Standard 128-byte DDS header template (DDS_HEADER_FLAGS_TEXTURE | MIPMAP).
# Layout fields filled per-texture:
#   0x0C u32 height, 0x10 u32 width, 0x14 u32 pitchOrLinearSize,
#   0x1C u8  mipMapCount, 0x54 4s pixelformat.fourCC.
_DDS_HEADER_TEMPLATE = bytes([
    0x44, 0x44, 0x53, 0x20, 0x7C, 0x00, 0x00, 0x00,
    0x07, 0x10, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x20, 0x00, 0x00, 0x00,
    0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])
assert len(_DDS_HEADER_TEMPLATE) == 128


@dataclass
class DDSxEntry:
    name: str            # base name (no suffix)
    header: bytes        # 0x20 byte DDSx header
    body: bytes          # raw payload (still compressed if compression != raw)

    @property
    def fourcc(self) -> bytes:
        return self.header[4:8]

    @property
    def width(self) -> int:
        return struct.unpack_from("<H", self.header, 0x0C)[0]

    @property
    def height(self) -> int:
        return struct.unpack_from("<H", self.header, 0x0E)[0]

    @property
    def levels(self) -> int:
        return self.header[0x10]

    @property
    def mem_size(self) -> int:
        return struct.unpack_from("<I", self.header, 0x18)[0]

    @property
    def packed_size(self) -> int:
        return struct.unpack_from("<I", self.header, 0x1C)[0]

    @property
    def compression_code(self) -> int:
        return self.header[0x0B]

    @property
    def compression_name(self) -> str:
        return COMPRESSION_NAMES.get(self.compression_code, f"unknown_{self.compression_code:#x}")


def parse_dxp2(data: bytes) -> List[DDSxEntry]:
    """Parse a DxP2 container blob into a list of DDSxEntry objects."""
    if len(data) < 0x48 or data[:4] != DXP2_MAGIC:
        raise ValueError("not a DxP2 file (magic mismatch)")

    total = struct.unpack_from("<H", data, 0x08)[0]

    # File-name table starts at 0x48; entries are NUL-terminated. Each name
    # contains a '*' separator followed by variant/suffix metadata that is
    # not part of a usable filename (e.g. 'foo*?u01'). Match wt-tools and
    # keep only the part before the first '*'.
    names: List[str] = []
    cur = 0x48
    for _ in range(total):
        end = data.index(b"\x00", cur)
        name = data[cur:end].decode("utf-8", errors="replace")
        name = name.split("*", 1)[0]
        names.append(name)
        cur = end + 1

    # DDSx header table (0x20 bytes per entry).
    dds_block_off = struct.unpack_from("<I", data, 0x20)[0] + 0x10
    headers: List[bytes] = []
    cur = dds_block_off
    for _ in range(total):
        headers.append(data[cur:cur + 0x20])
        cur += 0x20

    # Body table (0x18 bytes per entry, body offset @+0xC, body size @+0x10).
    body_block_off = struct.unpack_from("<I", data, 0x30)[0] + 0x10
    entries: List[DDSxEntry] = []
    cur = body_block_off
    for i in range(total):
        body_off = struct.unpack_from("<I", data, cur + 0x0C)[0]
        body_sz = struct.unpack_from("<I", data, cur + 0x10)[0]
        body = data[body_off:body_off + body_sz]
        entries.append(DDSxEntry(name=names[i], header=headers[i], body=body))
        cur += 0x18

    return entries


def _decompress_body(entry: DDSxEntry, oodle_decompressor=None) -> bytes:
    """Return the uncompressed pixel-data block for one DDSx entry."""
    code = entry.compression_code
    body = entry.body
    mem_sz = entry.mem_size

    if code == 0x00 or entry.packed_size == 0:
        return body[:mem_sz] if mem_sz else body

    if code == 0x20:
        import zstandard

        return zstandard.ZstdDecompressor().decompress(body, max_output_size=mem_sz)

    if code == 0x80:
        return zlib.decompress(body)

    if code == 0x40:
        try:
            import pylzma  # type: ignore
        except ImportError as exc:
            raise RuntimeError("LZMA-compressed DDSx requires the 'pylzma' package") from exc
        return pylzma.decompress(body, maxlength=mem_sz)

    if code == 0x60:
        if oodle_decompressor is None:
            raise RuntimeError("Oodle-compressed DDSx requires an OodleDecompressor")
        out = oodle_decompressor.decompress(body, mem_sz)
        if not out:
            raise RuntimeError(f"Oodle decompression failed for {entry.name}")
        return out

    raise RuntimeError(f"Unsupported DDSx compression {code:#x} ({entry.compression_name})")


# DXGI formats used when writing the DX10 extended header for BCn textures
# that have no legacy FOURCC representation (BC4/BC5/BC6/BC7).
# Reference: dxgiformat.h
DXGI_FORMAT_BC4_UNORM = 80
DXGI_FORMAT_BC5_UNORM = 83
DXGI_FORMAT_BC6H_UF16 = 95
DXGI_FORMAT_BC7_UNORM = 98

_DXGI_BY_DDSX_FOURCC = {
    b"ATI1": DXGI_FORMAT_BC4_UNORM,
    b"BC4 ": DXGI_FORMAT_BC4_UNORM,
    b"ATI2": DXGI_FORMAT_BC5_UNORM,
    b"BC5 ": DXGI_FORMAT_BC5_UNORM,
    b"BC6H": DXGI_FORMAT_BC6H_UF16,
    b"BC7 ": DXGI_FORMAT_BC7_UNORM,
}

# Some DDSx headers (e.g. foliage pivot-point textures) store a Direct3D 9
# D3DFORMAT enum in the low byte instead of a printable FOURCC. The upper
# three bytes are zero in that case. Map known D3DFMT_* codes to DXGI.
DXGI_FORMAT_R8G8B8A8_UNORM = 28
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_R16G16B16A16_FLOAT = 10
DXGI_FORMAT_R32G32B32A32_FLOAT = 2
DXGI_FORMAT_R16G16_FLOAT = 34
DXGI_FORMAT_R32_FLOAT = 41
DXGI_FORMAT_R16_FLOAT = 54

_DXGI_BY_D3DFMT = {
    21: DXGI_FORMAT_B8G8R8A8_UNORM,   # D3DFMT_A8R8G8B8
    22: DXGI_FORMAT_B8G8R8A8_UNORM,   # D3DFMT_X8R8G8B8
    32: DXGI_FORMAT_R8G8B8A8_UNORM,   # D3DFMT_A8B8G8R8
    113: DXGI_FORMAT_R16G16B16A16_FLOAT,  # D3DFMT_A16B16G16R16F
    114: DXGI_FORMAT_R32G32B32A32_FLOAT,  # D3DFMT_A32B32G32R32F (per Dagor)
    115: DXGI_FORMAT_R16G16_FLOAT,    # D3DFMT_G16R16F
    111: DXGI_FORMAT_R16_FLOAT,       # D3DFMT_R16F
    112: DXGI_FORMAT_R32_FLOAT,       # D3DFMT_R32F
}


def _build_dds_legacy(fourcc: bytes, width: int, height: int, levels: int, mem_size: int) -> bytes:
    dds = bytearray(_DDS_HEADER_TEMPLATE)
    struct.pack_into("<I", dds, 0x0C, height)
    struct.pack_into("<I", dds, 0x10, width)
    struct.pack_into("<I", dds, 0x14, mem_size)
    dds[0x1C] = levels
    dds[0x54:0x58] = fourcc
    return bytes(dds)


def _build_dds_dx10(dxgi_format: int, width: int, height: int, levels: int, mem_size: int) -> bytes:
    dds = bytearray(_DDS_HEADER_TEMPLATE)
    struct.pack_into("<I", dds, 0x0C, height)
    struct.pack_into("<I", dds, 0x10, width)
    struct.pack_into("<I", dds, 0x14, mem_size)
    dds[0x1C] = levels
    dds[0x54:0x58] = b"DX10"
    # 20-byte DDS_HEADER_DXT10:
    #   u32 dxgiFormat, u32 resourceDimension, u32 miscFlag, u32 arraySize, u32 miscFlags2
    # resourceDimension = 3 (DDS_DIMENSION_TEXTURE2D)
    dx10 = struct.pack("<5I", dxgi_format, 3, 0, 1, 0)
    return bytes(dds) + dx10


def ddsx_to_dds(entry: DDSxEntry, oodle_decompressor=None) -> bytes:
    """Convert a single DDSxEntry into a complete .dds byte string."""
    fourcc = entry.fourcc
    pixels = _decompress_body(entry, oodle_decompressor=oodle_decompressor)

    if fourcc in (b"DXT1", b"DXT3", b"DXT5"):
        header = _build_dds_legacy(fourcc, entry.width, entry.height, entry.levels, entry.mem_size)
        return header + pixels

    if fourcc in _DXGI_BY_DDSX_FOURCC:
        header = _build_dds_dx10(
            _DXGI_BY_DDSX_FOURCC[fourcc], entry.width, entry.height, entry.levels, entry.mem_size
        )
        return header + pixels

    # D3DFORMAT enum stored in low byte (upper 3 bytes are zero).
    if fourcc[1:] == b"\x00\x00\x00":
        d3dfmt = fourcc[0]
        if d3dfmt in _DXGI_BY_D3DFMT:
            header = _build_dds_dx10(
                _DXGI_BY_D3DFMT[d3dfmt], entry.width, entry.height, entry.levels, entry.mem_size
            )
            return header + pixels

    raise RuntimeError(
        f"Unsupported DDSx pixel format {fourcc!r} for {entry.name}"
    )


def extract_dxp(
    dxp_path: Path,
    output_dir: Optional[Path] = None,
    oodle_decompressor=None,
    verbose: bool = False,
) -> List[Path]:
    """Extract every texture in `dxp_path` to .dds files in `output_dir`.

    Returns the list of written file paths. `output_dir` defaults to
    `<dxp_path>_u/` (matches the wt-tools convention).
    """
    dxp_path = Path(dxp_path)
    data = dxp_path.read_bytes()
    entries = parse_dxp2(data)

    if output_dir is None:
        output_dir = dxp_path.with_name(dxp_path.name + "_u")
    output_dir.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for entry in entries:
        try:
            dds_blob = ddsx_to_dds(entry, oodle_decompressor=oodle_decompressor)
        except Exception as exc:
            if verbose:
                print(f"[DDSx] skip {entry.name}: {exc}")
            continue

        out_path = output_dir / f"{entry.name}.dds"
        out_path.write_bytes(dds_blob)
        written.append(out_path)
        if verbose:
            print(
                f"[DDSx] {entry.name}: {entry.fourcc.decode()} "
                f"{entry.width}x{entry.height} levels={entry.levels} "
                f"comp={entry.compression_name} -> {out_path.name}"
            )

    return written


def find_sibling_dxp_files(grp_path: Path) -> List[Path]:
    """Locate companion *.dxp.bin texture archives for a .grp file.

    Search order:
      1. Same directory as the GRP.
      2. Sibling `dxp/` directory (when the GRP itself lives under `grp/`),
         e.g. `<base>/grp/foo.grp` -> `<base>/dxp/foo.dxp.bin`.

    A candidate is accepted when its filename equals `<stem>.dxp.bin` or
    starts with the GRP stem.
    """
    grp_path = Path(grp_path)
    stem = grp_path.stem.lower()

    search_dirs: List[Path] = [grp_path.parent]
    if grp_path.parent.name.lower() == "grp":
        sibling = grp_path.parent.parent / "dxp"
        if sibling.is_dir():
            search_dirs.append(sibling)

    out: List[Path] = []
    seen: set = set()
    for d in search_dirs:
        for candidate in d.glob("*.dxp.bin"):
            cname = candidate.name.lower()
            if cname == f"{stem}.dxp.bin" or cname.startswith(stem):
                key = candidate.resolve()
                if key not in seen:
                    seen.add(key)
                    out.append(candidate)
    return sorted(out)
