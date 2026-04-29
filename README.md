# GRP Converter

Converts Dagor GRP resources to Wavefront OBJ format.

**Supported decoders:**
- **77F8232F** (RendInst): renderable instances with multi-LOD support
- **B4B7D9C4** (DynModel): dynamic renderable scenes with rigid body placement
- **ACE50000** (Collision): collision mesh geometry (cls boxes, fences, etc.)
- **56F81B6D** (Skeleton): GeomNodeTree bone hierarchies
- **D543E771** (PhysObj): physics object wireframes (multi-box collision debug)
- **4E1D5F5E, 40C586F9**: generic encoded meshes

## Architecture

**Modular mixin-based design** (refactored Apr 2026):
- `src/grp_converter.py` — Thin orchestrator (`GRPResourceParser` class)
- `src/decoders/` — Functional mixins:
  - `mesh_utils.py` — `MeshBuilderMixin` (geometry construction)
  - `collision.py` — `CollisionDecoderMixin` (ACE50000, cls)
  - `skeleton.py` — `SkeletonDecoderMixin` (GeomNodeTree, phobj)
  - `vertex_stream.py` — `VertexStreamMixin` (fp16, legacy B4)
  - `rendinst.py` — `RendInstDecoderMixin` (77F8232F, meshopt)
  - `dynmodel.py` — `DynModelDecoderMixin` (B4B7D9C4 with world transforms)
- `src/oodle.py` — Oodle Kraken decompressor (ctypes wrapper)

**Inheritance chain:** `GRPResourceParser` inherits from all mixins via MRO for unified method resolution.

## Usage

### Quick start (recommended)

Extract + convert + report in one step:

```bash
python extract_grp.py test_example/grp/usa_m60a1.grp --verbose
```

Output: `test_example/output/usa_m60a1.obj`

### Workflow options

**Option 1: Already-extracted resources**

```bash
# Using main.py wrapper
python main.py <extracted_dir> [output_dir] [--verbose]

# Or directly via cli.py
python src/cli.py <extracted_dir> [output_dir] [--verbose]
```

**Option 2: Batch via shell script**

Windows:
```batch
grp2obj.bat <extracted_dir> [output_dir] [--verbose]
```

Linux/WSL/macOS:
```bash
./grp2obj.sh <extracted_dir> [output_dir] [--verbose]
```

### Additional options

```bash
python extract_grp.py <input.grp> \
  [--extract-dir DIR]     # Override extract folder (default: input_dir/.extract) \
  [--output-dir DIR]      # Output OBJ folder (default: input_dir/output) \
  [--verbose]             # Enable debug logging \
  [--force-clean]         # Re-extract even if folder exists \
  [--scale FLOAT]         # Global scale multiplier for OBJ (default: 1.0) \
  [--mesh-types TYPES]    # rendinst,dynmodel,skeleton,collision,phobj,generic,all \\
  [--lods LIST]           # e.g. 0 or 0,1,2 or all (affects rendinst/dynmodel) \\
  [--split-variants]      # Export one OBJ per collection/variant (same as --split-collections) \\
  [--split-collections]   # Alias of --split-variants \\
  [--no-auto-scale-fp16]  # Disable fp16 upscaling heuristic
```

Default filtering is strict: `--mesh-types dynmodel --lods 0`.
If dynmodel yields no meshes, extraction automatically falls back to `rendinst` with the same LOD filter.
Use `--mesh-types all --lods all` to include everything.

## Examples

```bash
# Convert a vehicle model (1023 meshes)
python extract_grp.py test_example/grp/usa_m60a1.grp --verbose

# Aircraft with collision export
python extract_grp.py test_example/grp/jp_a6m2.grp --split-variants

# Split multi-collection pack (usa_fa_18) into one OBJ per plane collection
python extract_grp.py test_example/grp/usa_fa_18.grp --split-collections

# Prop/scenery (water decals)
python extract_grp.py test_example/grp/water_decals.grp --force-clean --scale 0.5
```

## What is implemented now

- Deterministic B4 strategy-0 framing:
  - Inner lengthflags at byte 20
  - Raw size at byte 24
  - Oodle payload from byte 28
- Deterministic table-first stream probing for B4:
  - Header dword candidates seed local FP16 stream search
  - Heuristic global scan remains as fallback
- Optional zstd (`flags == 1`) support when `zstandard` Python package is installed

## Extraction Report Rule

For every extraction run, add markdown report documenting:

- Extraction command and environment
- Extracted file list and class-id buckets
- Observed structure (header/resource/object grouping)
- Unknown or uncertain fields/patterns
- Follow-up verification plan

## Input / output

Input folder example:

```text
model__grp_extract/
  model.B4B7D9C4
  model_anim.40C586F9
  model_animtree.8F2A701A
```

Output:

- Default output directory: `output/` inside the extracted folder
- Single combined OBJ named after input directory (without `__grp_extract`) when split mode is off
- One OBJ file per decoded resource/variant when `--split-variants` is enabled

## Troubleshooting

If decode fails:

1. Run with `--verbose` and inspect which strategy was attempted.
2. Verify `oo2core_9_win64.dll` is reachable via `config.json` or default lookup paths.
3. Confirm extraction is from `dumpGrp -exp` and not mixed/partial files.

## Files

- `main.py`: canonical converter entrypoint (extracted folder -> OBJ)
- `grp_converter.py`: compatibility converter entrypoint wrapper
- `extract_grp.py`: extraction + conversion + report entrypoint
- `grp2obj.bat` / `grp2obj.sh`: shell wrappers
- `src/core/`: Python pipeline sources (`extract_report`, `cli`, `grp_converter`, `exporter`, `paths`)

## Notes on generated artifacts

Generated diagnostics are intentionally ignored via root `.gitignore`:

- `*_b4_sections.json`
- `*_dec.bin`
- `decoded_b4.bin`
- `__pycache__/`
