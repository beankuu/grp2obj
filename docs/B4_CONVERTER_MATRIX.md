# small_grps B4 Converter Matrix

Toolchain used:

- `dumpGrp-dev.exe -exp`
- `cargo run -- --verbose`
- Source subset: B4-bearing samples from `example/model/small_grps` plus targeted ship regression cases
- Oodle backend: Rust `oozextract`

## Summary

- B4-bearing samples tested: `5`
- Native geometry successes: `4`
- Synthetic fallback successes: `1`
- Failed converter runs: `0`
- Best broad geometry sample: `fr_pships_weaponry.grp`
- Best compact vehicle sample: `jp_a6m2_n.grp`
- Best tiny special-case sample: `water_decals.grp`

## Matrix

| GRP | Converter | B4 blobs | OBJ files | Vertices | Faces | Notes |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `fr_pships_weaponry.grp` | PASS | 9 | 9 | 1567 | 1259 | Strongest breadth sample; repeated torpedo/depth-charge patterns |
| `jp_a6m2_n.grp` | PASS | 2 | 2 | 959 | 779 | Good compact aircraft sample with full resource mix |
| `usa_towed_at_m40.grp` | PASS | 3 | 3 | 1164 | 794 | Good ground-vehicle sample with base/dmg/xray variants |
| `water_decals.grp` | PASS (synthetic) | 1 | 1 | 9 | 8 | Tiny special-case sample; exported as a planar fallback inferred from parameter bounds |
| `jap_battleship_fuso.grp` | PASS | 3 | 3 | varies | varies | Base/dmg/xray regression case |

## Details

### `fr_pships_weaponry.grp`

- Extracted B4 resources: `9`
- Exported OBJ files: `9`
- Total vertices: `1567`
- Total faces: `1259`
- Observations:
  - Repeated `vcount_hint=120` pattern across many resources
  - Multiple successful table-seeded decodes with starts clustered in plausible late-buffer regions
  - Good candidate for studying repeated B4 layouts and decoder consistency

### `jp_a6m2_n.grp`

- Extracted B4 resources: `2`
- Exported OBJ files: `2`
- Total vertices: `959`
- Total faces: `779`
- Observations:
  - Main aircraft body decoded as `869v / 713f`
  - Undercarriage decoded as `90v / 66f`
  - Good compact multi-part aircraft repro

### `usa_towed_at_m40.grp`

- Extracted B4 resources: `3`
- Exported OBJ files: `3`
- Total vertices: `1164`
- Total faces: `794`
- Observations:
  - Three useful variants: base, damage, xray
  - Main body decode is large enough to be informative without being huge
  - Good cross-check against aircraft-specific assumptions

### `water_decals.grp`

- Extracted B4 resources: `1`
- Exported OBJ files: `1`
- Total vertices: `9`
- Total faces: `8`
- Result: converter exported a synthetic fallback plane
- Output:
  - `example/model/water_decals__grp_extract/output/wave_a.obj`
- Value:
  - Very small, fast negative test
  - Good for isolating unsupported B4 layouts or non-mesh payload assumptions
  - Confirms that not every `.B4B7D9C4` resource is a compressed mesh container
- Isolated structure notes:
  - File size is only `220` bytes
  - `u32 @ 0x14` is `0x00000010`, so it does not match the Oodle strategy-0 framing used by the current analyzer/converter
  - No plausible compressed lengthflags were found in the first `128` bytes
  - Payload contains sparse parameters and an `@root` string instead of a decompression/table header followed by a vertex stream
  - Current converter behavior treats this as a tiny parameter block and synthesizes a 3x3 planar mesh from the inferred bounds

### `jap_battleship_fuso.grp`

- Extracted B4 resources: `3`
- Exported OBJ files with default filters: `3`
- Output variants:
  - `battleship_fuso.obj`
  - `battleship_fuso_dmg.obj`
  - `battleship_fuso_xray.obj`
- Observations:
  - `battleship_fuso_dmg.B4B7D9C4` and `battleship_fuso_xray.B4B7D9C4` decode as native DynModel rigid-node meshes
  - `battleship_fuso.B4B7D9C4` main block remains a useful large DynModel regression case
  - When neither backend can decode the main block, the variant is now skipped instead of being filled with collision-island placeholders
  - With a working backend, base LOD0 exports real DynModel rigid-node objects such as `body`, `rudder_01`, and `propeller_03`

## Recommended Focus Order

1. `fr_pships_weaponry.grp`
2. `jp_a6m2_n.grp`
3. `usa_towed_at_m40.grp`
4. `water_decals.grp`
5. `jap_battleship_fuso.grp`

## Takeaway

For current B4 geometry work, the compact regression pack now contains:

- `4` native geometry converter samples
- `1` synthetic fallback sample

That is a good balance for iterative decoder work: broad success coverage plus a tiny special-case fallback case.
