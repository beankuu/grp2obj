# small_grps Extraction Test Matrix

Toolchain used:
- `dumpGrp-dev.exe -exp`
- Source set: `example/model/small_grps`

## Summary

- Total tested: `12`
- Extraction passes: `12`
- Extraction failures: `0`
- Files that produced `.B4B7D9C4` geometry resources: `4`

## Matrix

| GRP | Status | Extracted Files | `.dag` | `.B4B7D9C4` | Extracted Resource Types | Learning Value |
|---|---|---:|---:|---:|---|---|
| `apmat.grp` | PASS | 20 | 0 | 0 | `.39B5B09E=20` | Material/container edge case; not useful for current B4 geometry work |
| `avg_volokolamsk.grp` | PASS | 12 | 0 | 0 | `.03FB59C4=5, .77F8232F=1, .ACE50000=1, .blk=5` | Aggregate world/container coverage; no B4 geometry |
| `bunker_entrence.grp` | PASS | 6 | 0 | 0 | `.77F8232F=3, .ACE50000=3` | Compact structure/resource mix; no B4 geometry |
| `bushes.grp` | PASS | 10 | 0 | 0 | `.77F8232F=6, .ACE50000=4` | Dense foliage/resource mix; no B4 geometry |
| `fr_pships_weaponry.grp` | PASS | 18 | 0 | 9 | `.56F81B6D=9, .B4B7D9C4=9` | Best current geometry-learning sample in this pack |
| `gm_logic.grp` | PASS | 47 | 0 | 0 | `.56F81B6D=7, .ACE50000=40` | Logic/skeleton-heavy coverage; useful for non-B4 structure typing |
| `jp_a6m2_n.grp` | PASS | 8 | 0 | 2 | `.40C586F9=1, .56F81B6D=2, .8F2A701A=1, .A6F87A9B=1, .ACE50000=1, .B4B7D9C4=2` | Good compact vehicle sample with full multi-resource mix |
| `splines.grp` | PASS | 1 | 0 | 0 | `.39B5B09E=1` | Minimal container edge case |
| `usa_towed_at_m40.grp` | PASS | 10 | 0 | 3 | `.56F81B6D=4, .855A1BE6=2, .ACE50000=1, .B4B7D9C4=3` | Good ground-vehicle geometry sample |
| `vietnam_rocks.grp` | PASS | 8 | 0 | 0 | `.77F8232F=4, .ACE50000=4` | Irregular terrain/resource case; no B4 geometry |
| `water_decals.grp` | PASS | 2 | 0 | 1 | `.56F81B6D=1, .B4B7D9C4=1` | Tiny B4-bearing edge case |
| `wood_vine.grp` | PASS | 8 | 0 | 0 | `.77F8232F=4, .ACE50000=4` | Thin organic/resource case; no B4 geometry |

## Recommended Focus

If the goal is improving the current `grp_converter` B4 path, prioritize these extracted outputs:
- `fr_pships_weaponry__grp_extract`
- `usa_towed_at_m40__grp_extract`
- `jp_a6m2_n__grp_extract`
- `water_decals__grp_extract`

Recommended order:
1. `fr_pships_weaponry.grp` for breadth and repeated B4 patterns
2. `jp_a6m2_n.grp` for multi-resource vehicle structure
3. `usa_towed_at_m40.grp` for another vehicle class with several B4 blobs
4. `water_decals.grp` for a tiny fast repro case
