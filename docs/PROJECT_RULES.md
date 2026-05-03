# Project Rules

## Layout

- Rust implementation lives in `rust/`.
- The main CLI is `extract_grp`, built from `rust/main.rs`.
- Top-level files are limited to docs, config, build metadata, and stable entry/build scripts.
- Temporary throwaway scripts live under `scratch/`.

## Path Rules

- Implementation code must not assume it runs from repository root.
- Config and library path lookup should resolve from project root discovery, not from current working directory only.
- Preferred extraction command: `cargo run -- <input.grp> [output_dir] [--verbose]`.

## Naming

- Root entrypoints should expose stable command names.
- Internal utility scripts should not be added directly to root.
