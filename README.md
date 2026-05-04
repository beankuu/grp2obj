# GRP Converter

Converts Dagor GRP resources to Wavefront OBJ format.

## Warning

- The converter is now Rust-only.
- `dumpGrp` is still required for container extraction (`dumpGrp-dev.exe` on Windows).

**Supported decoders:**

- RendInst: renderable instances with multi-LOD support
- DynModel: dynamic renderable scenes with rigid body placement
- Collision: collision mesh geometry (cls boxes, fences, etc.)
- Skeleton: GeomNodeTree bone hierarchies
- PhysObj: physics object wireframes (multi-box collision debug)

## Architecture

- `rust/main.rs` — Thin CLI entrypoint and shared type/module wiring
- `rust/pipeline.rs` — Extraction pipeline orchestration (input/output/temp/execution flow)
- `rust/settings.rs` — Root/config discovery and `dumpGrp` path resolution/bootstrap
- `rust/decode.rs` — Resource decode logic (ACE50000/B4B7D9C4/skeleton helpers)
- `rust/export.rs` — OBJ export and variant split output
- `Cargo.toml` — Rust package manifest (`oozextract` + `zstd`)
- `Cargo.lock` — Locked Rust dependency graph for reproducible builds

## Installation

### Requirements

- Rust toolchain (`cargo`, `rustc`)
- `dumpGrp` executable available via config path, env var, default `lib/` location, or `PATH`

### Setup

1. Clone the repository.
2. Install `dumpGrp` for your environment.
  - Windows: if missing, the tool can prompt to auto-download from the latest DagorEngine release.
  - Any OS: you can point to your binary through `config.json` (`dumpGrpPath`) or `GRP2OBJ_DUMPGRP`.
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
2. Confirm your binary exists and path resolution is valid (`config.json -> dumpGrpPath`/`dumpGrp` or `GRP2OBJ_DUMPGRP`).
3. Confirm extraction is from `dumpGrp -exp` and not mixed/partial files.

## Project structure

**Root entry points:**

- `cargo run -- <input.grp> [output_dir] [--verbose]`
- `target/release/extract_grp(.exe)` after `cargo build --release`

**Source:**

- `rust/main.rs` — CLI entry and module wiring
- `rust/pipeline.rs` — pipeline orchestration
- `rust/settings.rs` — tool path/config resolution
- `rust/decode.rs` — decode pipeline implementation
- `rust/export.rs` — OBJ export pipeline implementation

## Configuration

Edit `config.json` to customize paths for your environment.

**Config settings:**

- `"dumpGrpPath"` — Preferred path to GRP extraction tool (absolute or relative to repo root)
- `"dumpGrp"` — Backward-compatible alias for `dumpGrpPath`
- `GRP2OBJ_DUMPGRP` — Environment override for tool path (highest priority)

## License

This project is licensed under MIT. See the root `LICENSE` file.

## Notes on generated artifacts

See `THIRD_PARTY_NOTICES.txt` and `licenses/` for third-party license texts and attribution details.

Generated diagnostics are intentionally ignored via root `.gitignore`:

- `*_b4_sections.json`
- `*_dec.bin`
- `decoded_b4.bin`
