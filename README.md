# GRP Converter

Converts extracted Dagor GRP resources into Wavefront OBJ.

This toolchain is currently focused on reliable `.B4B7D9C4` decode for aircraft and vehicle samples (including `usa_m60a1`, `ussr_i_185`, and `rafale_c_f3` test sets).

## Current workflow

1. Extract `.grp` with `dumpGrp -exp`.
2. Run `main.py` on the extracted folder.
3. Write extraction report markdown (structure + unknown/uncertain parts).

Project layout rule:

- Root: entrypoints and wrappers only (`main.py`, `grp_converter.py`, `*.bat`, `*.sh`)
- Python implementation: `src/core/`
- Reference imports from sibling project: `reference/wt_skinmodder/`

## Usage

Windows:

```batch
grp2obj.bat <extracted_dir> [output_dir] [--verbose]
```

Linux/WSL/macOS:

```bash
./grp2obj.sh <extracted_dir> [output_dir] [--verbose]
```

Direct Python (already extracted resources):

```bash
python main.py <extracted_dir> [output_dir] [--verbose]
```

Direct extraction + convert + report (recommended):

```bash
python extract_grp.py <input.grp> [extract_dir] [output_dir] [--verbose] [--force-clean]
```

Example:

```bash
python extract_grp.py test_example/small_grps/water_decals.grp --verbose --force-clean
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
