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
- `src/oodle.py` — Oodle-family decompressor (uses open-source `ooz-wasm` via Node.js wrapper)

**Inheritance chain:** `GRPResourceParser` inherits from all mixins via MRO for unified method resolution.

## Installation

### Requirements

- Python 3.9+
- Node.js 16+ (for `ooz-wasm` decompression)

### Setup

1. Clone the repository
2. Create Python virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/macOS: or .venv\Scripts\Activate.ps1 on Windows
   ```

3. Install `ooz-wasm` npm package for decompression:

   ```bash
   npm install ooz-wasm
   ```

4. Download `tools-prebuild.windows-x86_64.7z` from:

  <https://github.com/GaijinEntertainment/DagorEngine/releases>

- Unzip the archive into `lib/` so this path exists:

  `lib/tools/dagor_cdk/windows-x86_64/dumpGrp-dev.exe`

- Update `config.json` if you use a non-default `dumpGrp` location

## Usage

### Quick start (recommended)

Extract + convert in one step:

```bash
python extract_grp.py test_example/grp/usa_m60a1.grp --verbose
```

Output: `test_example/output/usa_m60a1/*.obj` (split by collection/variant by default)

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
  [--split-variants]      # Alias for split output mode (default on) \\
  [--split-collections]   # Alias for split output mode (default on) \\
  [--no-split-collections]# Disable default split mode and export one combined OBJ \\
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
- One OBJ file per decoded resource/variant by default
- Single combined OBJ named after input directory (without `__grp_extract`) when `--no-split-collections` is used

## Troubleshooting

If decode fails:

1. Run with `--verbose` and inspect which strategy was attempted.
2. Verify Node.js is installed: `node --version`
3. Verify `ooz-wasm` is installed: `npm ls ooz-wasm`
4. Check that `lib/ooz-wasm-decompress.mjs` exists and is accessible
5. If `lib/tools` is missing, download `tools-prebuild.windows-x86_64.7z` from DagorEngine releases and unzip to `lib/`
6. Confirm extraction is from `dumpGrp -exp` and not mixed/partial files.

## Project structure

**Root entry points:**

- `extract_grp.py`: Extract GRP file and convert in one step (grp file → OBJ)

**Source (`src/`):**

- `extract_report.py` — GRP extraction pipeline
- `grp_converter.py` — Core decoder class (`GRPResourceParser`)
- `oodle.py` — Oodle decompression wrapper
- `exporter.py` — OBJ file writer
- `paths.py` — Config and path resolution
- `decoders/` — Resource type decoders (collision, skeleton, DynModel, RendInst, etc.)

## Configuration

Edit `config.json` to customize paths for your environment.

**Required settings:**

- `"oodle"` — Path to `lib/ooz-wasm-decompress.mjs` (Oodle decompression via Node.js)
- `"dumpGrp"` — Path to GRP extraction tool (typically bundled in `lib/tools/dagor_cdk/`)

## Notes on generated artifacts

Generated diagnostics are intentionally ignored via root `.gitignore`:

- `*_b4_sections.json`
- `*_dec.bin`
- `decoded_b4.bin`
- `__pycache__/`
