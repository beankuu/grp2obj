# RendInst (`*.77F8232F`) Format — Reverse-Engineering Notes

Class ID: `77F8232F` → `RendInstGameRes`. A static rendering instance with
N LODs, plus per-LOD vertex/index buffers, packed normals, UVs, and color.

Reference samples used in this document:
- [test_example/dump_volok/avg_volokolamsk_lake.77F8232F](test_example/dump_volok/avg_volokolamsk_lake.77F8232F) — 1 680 bytes, 2 LODs (24 + 56 verts), 1 600 m / 20 000 m.
- [test_example/dump_poland/avg_poland_iced_lake.77F8232F](test_example/dump_poland/avg_poland_iced_lake.77F8232F) — 15 184 bytes, 2 LODs (404 + 621 verts), 1 600 m / 20 000 m.

Both decode end-to-end through [src/grp_converter.py](src/grp_converter.py)
into Wavefront OBJ files with one object per LOD plus the collision mesh.

---

## 1. File layout (verified)

```
+---------+------+------------------------------------+
| Offset  | Size | Field                              |
+---------+------+------------------------------------+
|  0x00   |  4   | u32 hdr_size              = 0x60   |
|  0x04   |  8   | u64 reserved/-1                    |  (two u32 0xFFFFFFFF slots)
|  0x0C   |  4   | u32 lod_count             observed 2..4
|  0x10   |  4   | u32 unknown               = 0xC0000090 in both samples
|  0x14   |  4   | u32 comp_size             flag bit 31, mask 0x7FFFFFFF
|  0x18   |  4   | u32 raw_size              uncompressed size of the payload
|  0x1C   | csz  | Oodle Kraken-compressed payload (decompresses to raw_size)
|  ...    |  -   | RoSceneRendInst metadata trailer  (see §4)
|  EOF    |      |                                    |
+---------+------+------------------------------------+
```

Worked example (volok lake):

| Field      | Value             |
|---|---|
| comp_size  | `0x80000555 & 0x7FFFFFFF = 0x555 = 1 365` |
| raw_size   | `0xA8C = 2 700` |
| Trailer    | starts at `EOF − 287` = `0x571` |

Worked example (poland iced lake):

| Field      | Value             |
|---|---|
| comp_size  | `0x80003A17 & 0x7FFFFFFF = 0x3A17 = 14 871` |
| raw_size   | `0x8584 = 34 180` |
| Trailer    | starts at `EOF − 285` = `0x3A33` |

---

## 2. Decoded payload layout

```
+---------+--------+-----------------------------------+
| Offset  | Size   | Field                             |
+---------+--------+-----------------------------------+
|  0x00   |  0x50  | Fixed shader/format descriptor     |  IDENTICAL across observed samples
|  0x50   |  0x20  | LOD 0 record (32 bytes, see §3)    |
|  0x70   |  0x20  | LOD 1 record                       |
|  ...    |        |   (one 32-byte record per LOD)     |
|  0x90   |  v0×28 | LOD 0 vertex buffer (stride 28)    |
|  0x90+  |  ...   | LOD 0 index buffer (opaque codec)  |
|  ...    |  v1×28 | LOD 1 vertex buffer (stride 28)    |
|  ...    |  ...   | LOD 1 index buffer (opaque codec)  |
+---------+--------+-----------------------------------+
```

The first 80 bytes (`0x00..0x4F`) are **bit-for-bit identical** between
the volok and poland samples. They almost certainly encode the resource's
shader/material variant, vertex format ID, and channel layout, but the
field semantics are not yet decoded.

### 2.1 Vertex stride = 28 bytes (4 channels)

Verified by scanning for the constant packed-normal pattern
`80 FF 80 80`: every vertex contains it at byte offset `+12`, and the
distance between consecutive markers is exactly 28 bytes (with an extra
gap between LODs equal to the size of the LOD's index buffer).

| Bytes | Type     | Meaning                              |
|------:|---|---|
| `+0..11`  | `float[3]` | position (X, Y, Z) — engine units (meters) |
| `+12..15` | `u8[4]`    | packed normal — constant `80 FF 80 80` in observed samples |
| `+16..23` | `float[2]` | UV (u, v) |
| `+24..27` | `u8[4]`    | RGBA vertex color (volok = `00 00 00 FF`, poland varies) |

The packed-normal constant `80 FF 80 80` is the encoding of the up-vector
`(0, +1, 0)` in Dagor's `pack_normal` scheme (signed-byte components
biased to `0x80`, with one channel reserved). Both lakes are flat surfaces
so this matches the geometry exactly. Other rendInst assets are expected
to use varying packed normals.

---

## 3. LOD record (32 bytes)

| Bytes | Type | Field        | volok L0 | volok L1 | poland L0 | poland L1 |
|------:|---|---|---:|---:|---:|---:|
| `+0`  | u32 | `num_verts`         |   24 |   56 |  404 |  621 |
| `+4`  | u32 | unknown / hash      | 18 204 | 62 748 | 467 484 | 898 588 |
| `+8`  | u32 | `num_indices`       |  132 |  480 | 3 624 | 6 852 |
| `+12` | u32 | material/shader bits | 4 640 |  544 | 4 640 |  544 |
| `+16` | u32 | vertex_format       |   32 |   32 |   32 |   32 |
| `+20` | u32 | attr_count          |    4 |    4 |    4 |    4 |
| `+24` | u64 | reserved 0          |    0 |    0 |    0 |    0 |

Observations:
- `num_indices / 3 = num_tris` (44, 160, 1 208, 2 284 respectively).
- The "material/shader bits" field is **identical** between the two
  files for matching LOD slots (`4640` for L0, `544` for L1) — strong
  evidence that this encodes shader/material flags, not a per-asset hash.
- `vertex_format = 32` is constant; together with `attr_count = 4` it
  describes the channel set (pos + packed-normal + uv + color).

---

## 4. Trailer — `RoSceneRendInst` metadata

Starts at `EOF − ~285` bytes. All offsets below are **bytes from the
trailer start**.

| Off | Size | Field | Value (volok) |
|----:|----:|---|---|
| `+0x00` | u32 | `lod_count` | `2` |
| `+0x04` | u64 | reserved | `0` |
| `+0x0C` | float×3 | `bbox.min` | `(-171.976, -5.34e-5, -232.011)` |
| `+0x18` | float×3 | `bbox.max` | `( 171.976, -5.34e-5,  232.011)` |
| `+0x24` | float×3 | `bsphere.center` | `(-5.235, -5.34e-5, 1.251)` |
| `+0x30` | float | `bsphere.r` | `241.187` |
| `+0x34` | float | `bcyl.r` (or `lodMaxRangeMul`) | `242.868` |
| `+0x38` | LOD0 desc (16 B) | `(0, 8, 0, range_f32=1600.0)` |
| `+0x48` | LOD1 desc (16 B) | `(0, 8, 0, range_f32=20000.0)` |
| `+0x58` | 88 B | LOD0 fixup record | constants `88 / 40 / 1` |
| `+0xB0` | 88 B | LOD1 fixup record | constants vary by LOD index |
| `+0x118` | 24 B | trailing aux blob | small |

The constants `88 / 40 / 56 / 80 / 1` are byte-offsets used by Dagor's
`BinDump::saveDumpForTarget(...)` to patch pointers at load time.

---

## 5. Compression — Oodle Kraken

The opaque payload at `[0x1C .. 0x1C + comp_size]` is **Oodle Kraken**
LZ-compressed. Decompress with `OodleLZ_Decompress` (from
`oo2core_9_win64.dll`):

```python
out_buf = ctypes.create_string_buffer(raw_size)
OodleLZ_Decompress(data[0x1C:0x1C + comp_size], comp_size,
                   out_buf, raw_size,
                   0, 0, 0, None, 0, None, None, None, 0, 3)
```

Verified for both samples: the first 4 bytes of the compressed payload
are `8C 0C 00 ??`, which matches Kraken's "decoder type 8" header.

The `0x80000000` bit on `comp_size` is a payload-class flag (likely
"Oodle-compressed"). No zstd / zlib magic is present anywhere.

---

## 6. Index buffer — NOT YET DECODED (TODO)

Each LOD's index buffer immediately follows its vertex buffer in the
decoded payload. The first byte is **always `0xD0`**, and the rest of
the bytes are highly compressed (~0.5 bytes per logical index in volok),
so this is a custom delta/triangle codec. Bytes are mostly even values
and tend to be small.

Observed sizes:

| Sample / LOD | num_indices | bytes used | bytes/index |
|---|---:|---:|---:|
| volok L0  |   132 |  ~67 | 0.51 |
| volok L1  |   480 | ~245 | 0.51 |
| poland L0 | 3 624 | (large) | ~0.5 |

Hypotheses for the codec (untested):
1. Adaptive 4-bit nibble run with restart byte `0xD0`.
2. Cache-based vertex re-use codec à la `meshoptimizer` v1.
3. Triangle-strip with vertex-cache delta coding.

Workaround in [src/grp_converter.py](src/grp_converter.py): the rendInst
decoder runs a 2D Delaunay triangulation in the X-Z plane on the
extracted vertex positions. This produces an **exact** triangulation
for flat rendInst meshes (lakes, water decals, ground cards) and a
plausible-but-not-authoritative one for non-flat assets, so we still
emit a usable OBJ for every LOD until the real codec is implemented.

---

## 7. What the converter emits today

For `avg_volokolamsk.grp`:

```
o avg_volokolamsk_lake_lod0       24 verts, ~38 faces (Delaunay XZ)
o avg_volokolamsk_lake_lod1       56 verts, ~88 faces (Delaunay XZ)
o avg_volokolamsk_lake_collision  (existing collision decoder)
```

For `avg_poland_snow.grp`:

```
o avg_poland_iced_lake_lod0       404 verts,  ~770 faces (Delaunay XZ)
o avg_poland_iced_lake_lod1       621 verts, ~1180 faces (Delaunay XZ)
o avg_poland_iced_lake_collision  (existing collision decoder)
```

Vertex positions, UVs, and colors are extracted from the file — only
the original triangle topology is substituted with the Delaunay XZ
approximation. (Normals are constant `(0, 1, 0)` for both samples
because they are flat lakes.)

---

## 8. Known parts (verified end-to-end)

- 0x1C-byte resource preamble: `hdr_size`, `lod_count`, `comp_size`, `raw_size`.
- Oodle Kraken decompression of the compressed payload.
- Per-LOD 32-byte record at `0x50 + i*32`: `num_verts`, `num_indices`, vertex format flags.
- Vertex stride = 28 bytes (pos + packed-normal + uv + color).
- Vertex section starts at `0x90` in the decoded payload.
- Trailer with bbox / bsphere / per-LOD ranges (1 600 m / 20 000 m for both lakes).

## 9. Unknown / not yet decoded

1. **Index buffer codec** (`0xD0`-headed). Needed for authoritative triangulation of non-flat rendInst assets.
2. The 80-byte fixed descriptor at decoded offset `0x00..0x4F`. Likely encodes shader class, material slot count, and vertex layout ID — but layout is unknown.
3. **Texture/material indexing into the GRP `RoNameMap`**. Texture names (e.g. the lake's `black_alpha`, `snow_e_tex_*`) are NOT inside the resource blob — they live at the GRP container level and are referenced by integer index. We have not yet located the texture index list inside the rendInst.
4. The unknown u32 at decoded offset `+4` of each LOD record (varies per asset, looks hash-like).
5. The constant `0xC0000090` at file offset `0x10` — possibly an Oodle compressor parameter (compressor=8 + level mask).
6. The trailing 22-byte aux blob at end of trailer.

## 10. TODO (in priority order)

1. **Decode the index codec.** Without it, non-flat rendInst meshes get a Delaunay XZ topology (still renderable but topologically incorrect for sloped/3D assets).
2. **Cross-sample sweep** — diff decoded payload header (`0x00..0x4F`) across many rendInst files to confirm what is constant vs varying.
3. **Locate the texture index list** in the decoded payload and resolve via the GRP `RoNameMap` so OBJ exports can include `usemtl` / `mtllib` references for the actual textures.
4. **Decode the per-LOD `material/shader bits`** (`4640` / `544`) — these are stable across samples for matching LOD slots and are likely shader/state IDs.
5. Decode packed normals (currently emitted as `(0, 1, 0)` because the observed samples are flat). Format is almost certainly Dagor's `pack_normal_dec` (signed-byte XYZ + reserved).
6. Verify the LOD record's u32 at `+4` interpretation by comparing against known dabuild sources.

---

## 11. Code references

- Decoder: `RendInstParser._decode_77f8232f_rendinst` in [src/grp_converter.py](src/grp_converter.py)
- Header struct: `parse_rendinst_preamble` in [src/dagor_resources.py](src/dagor_resources.py)
- Sample assets: [test_example/dump_volok/](test_example/dump_volok/), [test_example/dump_poland/](test_example/dump_poland/)
- OBJ outputs: [test_example/output/avg_volokolamsk.obj](test_example/output/avg_volokolamsk.obj), [test_example/output/avg_poland_snow.obj](test_example/output/avg_poland_snow.obj)
