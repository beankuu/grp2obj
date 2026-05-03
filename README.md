# GRP Converter

Converts Dagor GRP resources to Wavefront OBJ format.

## Warning

- Without oo2core dll file the converter will still work for most resources, but some large DynModel blocks (e.g. `jap_battleship_fuso` main) may fail to decode and will be skipped.
- If `lib/oo2core_9_win64.dll` (or your configured DLL path) is missing, the converter automatically falls back to `ooz-wasm`.
- If you already have a local `oo2core` DLL, place it in `lib/` (or point `config.json -> oodle` to it) for best compatibility.

**Supported decoders:**

- RendInst: renderable instances with multi-LOD support
- DynModel: dynamic renderable scenes with rigid body placement
- Collision: collision mesh geometry (cls boxes, fences, etc.)
- Skeleton: GeomNodeTree bone hierarchies
- PhysObj: physics object wireframes (multi-box collision debug)

## Architecture

**Modular mixin-based design**:

- `src/grp_converter.py` — Thin orchestrator (`GRPResourceParser` class)
- `src/decoders/` — Functional mixins:
  - `mesh_utils.py` — `MeshBuilderMixin` (geometry construction)
  - `collision.py` — `CollisionDecoderMixin` (ACE50000, cls)
  - `skeleton.py` — `SkeletonDecoderMixin` (GeomNodeTree, phobj)
  - `vertex_stream.py` — `VertexStreamMixin` (fp16, legacy B4)
  - `rendinst.py` — `RendInstDecoderMixin` (77F8232F, meshopt)
  - `dynmodel.py` — `DynModelDecoderMixin` (B4B7D9C4 with world transforms)
- `src/oodle.py` — Oodle-family decompressor (user-supplied `oo2core` DLL by default, fallback `ooz-wasm` wrapper)

**Inheritance chain:** `GRPResourceParser` inherits from all mixins via MRO for unified method resolution.

## Installation

### Requirements

- Python 3.9+
- Node.js 16+ (for `ooz-wasm` fallback decompression)
- Optional but recommended: local `oo2core` DLL at `lib/oo2core_9_win64.dll` (or set another path in `config.json`)

### Setup

1. Clone the repository.
2. Create the Python virtual environment:

   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```

3. Install `ooz-wasm` npm dependency for fallback decompression:

  ```bash
  npm install
   ```

1. Download `tools-prebuild.windows-x86_64.7z` from <https://github.com/GaijinEntertainment/DagorEngine/releases>, extract `dumpGrp-dev.exe`, and place it at `lib/dumpGrp-dev.exe`. Update `config.json` if you use a non-default `dumpGrp` location.

- If you have a legal local DLL, use `lib/oo2core_9_win64.dll` (default config) or set `OODLE_DLL=C:\path\to\oo2core_9_win64.dll`.
- If DLL is missing, fallback is automatic via `ooz-wasm`.

## Usage

### Quick start (recommended)

Extract + convert in one step:

```bash
python extract_grp.py test_example/grp/usa_m60a1.grp
```

Output: `test_example/grp/usa_m60a1/*.obj` (next to the GRP file, split by collection/variant by default)

### Additional options

```bash
python extract_grp.py <input.grp> \
  [output_dir]            # Optional OBJ output dir \
  [--verbose]             # Enable debug logging \
  [--mesh-types TYPES]    # rendinst,dynmodel,skeleton,collision,phobj,generic,all \\
  [--lods LIST]           # e.g. 0 or 0,1,2 or all (affects rendinst/dynmodel) \\
  [--split-variants]      # Alias for split output mode (default on) \\
  [--split-collections]   # Alias for split output mode (default on) \\
  [--no-split-collections]# Disable default split mode and export one combined OBJ \\
  [--compare-file-specific]
```

Default filtering is strict: `--mesh-types dynmodel --lods 0`.
If dynmodel yields no meshes, extraction automatically falls back to `rendinst` with the same LOD filter.
If a `.B4B7D9C4` DynModel resource exists but its compressed vertex/index block is unsupported by the active Oodle backend, the variant is simply skipped (no synthetic collision-island fallback). Use `--mesh-types collision,skeleton` if you need the auxiliary geometry explicitly.
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
python extract_grp.py test_example/grp/water_decals.grp --verbose
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
- Oodle backend strategy:
  - User-supplied native `oo2core` DLL (via `OODLE_DLL` or `config.json`) is preferred and used first
  - `ooz-wasm` is the automatic fallback when DLL is missing or load/decode fails
  - Some large ship DynModel blocks (e.g. `jap_battleship_fuso` main DynModel) may still require native DLL compatibility
  - When neither backend can decode a B4 block, the affected DynModel variant is skipped instead of being filled with collision-island placeholders

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
2. Verify DLL path in `config.json` points to a real file (default: `lib/oo2core_9_win64.dll`).
3. Verify fallback dependency is installed: `npm install` and `node --version`.
4. For large DynModel blocks that fallback rejects (e.g. `jap_battleship_fuso` main), use a local legal `oo2core` DLL via `OODLE_DLL`.
5. If `lib/dumpGrp-dev.exe` is missing, download `tools-prebuild.windows-x86_64.7z` from DagorEngine releases and copy `dumpGrp-dev.exe` to `lib/`.
6. Confirm extraction is from `dumpGrp -exp` and not mixed/partial files.

## Project structure

**Root entry points:**

- `extract_grp.py`: Extract GRP file and convert in one step (grp file → OBJ)

**Source (`src/`):**

- `extract_report.py` — GRP extraction pipeline
- `grp_converter.py` — Core decoder class (`GRPResourceParser`)
- `oodle.py` — Oodle decompression wrapper (configured native DLL + `ooz-wasm` fallback)
- `exporter.py` — OBJ file writer
- `paths.py` — Config and path resolution
- `decoders/` — Resource type decoders (collision, skeleton, DynModel, RendInst, etc.)

## Configuration

Edit `config.json` to customize paths for your environment.

**Required settings:**

- `"oodle"` — Path to user-owned `oo2core*.dll` (default: `.\lib\oo2core_9_win64.dll`)
- `"dumpGrp"` — Path to GRP extraction tool (default: `.\lib\dumpGrp-dev.exe`)

Environment variables override the need to edit `config.json` for local-only files:

- `OODLE_DLL` — path to a legal local `oo2core` DLL. Best compatibility, cannot be redistributed.
- `OODLE_DLL` is optional; when not set or when the DLL path is missing/invalid, `ooz-wasm` fallback is attempted automatically.

## License

This project is licensed under GNU GPL v3.0. See the root `LICENSE` file.

## Notes on generated artifacts

See `THIRD_PARTY_NOTICES.txt` and `licenses/` for third-party license texts and attribution details.

Generated diagnostics are intentionally ignored via root `.gitignore`:

- `*_b4_sections.json`
- `*_dec.bin`
- `decoded_b4.bin`
- `__pycache__/`
