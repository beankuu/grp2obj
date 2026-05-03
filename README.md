# GRP Converter

Converts Dagor GRP resources to Wavefront OBJ format.

## Warning

- The converter is now Rust-only.
- `dumpGrp-dev.exe` is still required for container extraction.

**Supported decoders:**

- RendInst: renderable instances with multi-LOD support
- DynModel: dynamic renderable scenes with rigid body placement
- Collision: collision mesh geometry (cls boxes, fences, etc.)
- Skeleton: GeomNodeTree bone hierarchies
- PhysObj: physics object wireframes (multi-box collision debug)

## Architecture

- `rust/main.rs` — Rust CLI for extraction orchestration, DynModel decode, collision decode, skeleton WTM application, and OBJ export
- `Cargo.toml` — Rust package manifest (`oozextract` + `zstd`)
- `Cargo.lock` — Locked Rust dependency graph for reproducible builds

## Installation

### Requirements

- Rust toolchain (`cargo`, `rustc`)
- `lib/dumpGrp-dev.exe`

### Setup

1. Clone the repository.
2. Download `tools-prebuild.windows-x86_64.7z` from <https://github.com/GaijinEntertainment/DagorEngine/releases>, extract `dumpGrp-dev.exe`, and place it at `lib/dumpGrp-dev.exe`.
3. Build the Rust CLI:

  ```powershell
  cargo build
  ```

## Usage

### Quick start (recommended)

Extract + convert in one step:

```powershell
cargo run -- test_example/grp/usa_m60a1.grp
```

Output: `test_example/grp/usa_m60a1/*.obj` (next to the GRP file, split by collection/variant by default)

### Additional options

```powershell
cargo run -- <input.grp> [output_dir] [--verbose]
```

The current Rust path exports one OBJ per variant/collection and keeps DynModel output to LOD0, matching the established output shape for validated samples such as `usa_m60a1.grp`.

## Examples

```powershell
# Convert a vehicle model
cargo run -- test_example/grp/usa_m60a1.grp --verbose

# Write to a custom output directory
cargo run -- test_example/grp/usa_m60a1.grp output/m60a1 --verbose
```

## What is implemented now

- Rust extraction orchestration through `dumpGrp-dev.exe`
- Rust DynModel (`B4B7D9C4`) decode through `oozextract` and zstd
- Rust ACE50000 collision decode, including embedded `cls_*` AABB wire fallback
- Skeleton WTM application for DynModel rigid nodes
- OBJ export split by variant/collection

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

1. Run with `--verbose` and inspect the resource-specific log lines.
2. Confirm `lib/dumpGrp-dev.exe` exists or `config.json -> dumpGrp` points to a valid path.
3. Confirm extraction is from `dumpGrp -exp` and not mixed/partial files.

## Project structure

**Root entry points:**

- `cargo run -- <input.grp> [output_dir] [--verbose]`
- `target/release/extract_grp(.exe)` after `cargo build --release`

**Source:**

- `rust/main.rs` — CLI, extraction orchestration, decoding, and OBJ export

## Configuration

Edit `config.json` to customize paths for your environment.

**Config settings:**

- `"dumpGrp"` — Path to GRP extraction tool (default: `.\lib\dumpGrp-dev.exe`)

## License

This project is licensed under MIT. See the root `LICENSE` file.

## Notes on generated artifacts

See `THIRD_PARTY_NOTICES.txt` and `licenses/` for third-party license texts and attribution details.

Generated diagnostics are intentionally ignored via root `.gitignore`:

- `*_b4_sections.json`
- `*_dec.bin`
- `decoded_b4.bin`
