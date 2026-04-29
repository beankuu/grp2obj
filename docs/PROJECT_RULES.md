# Project Rules

## Layout

- Root contains entrypoints only:
  - `extract_grp.py`
  - top-level docs/config/data files
- Python implementation files live under `src/`.
- Imported external reference assets live under `reference/wt_skinmodder/`.
- Temporary throwaway scripts live under `scratch/`.

## Import / Path Rules

- Implementation modules must not assume they run from repository root.
- Config and library path lookup should resolve from project root discovery, not from current working directory only.
- New tools should be added under `src/` first.
- Every extraction run must produce markdown report with structure notes and unknown/uncertain parts.
- Preferred extraction command: `python extract_grp.py <input.grp> ...`.

## Naming

- Root entrypoints should expose stable command names.
- Internal utility scripts should not be added directly to root.
