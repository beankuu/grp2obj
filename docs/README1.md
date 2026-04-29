# small_grps Compact Regression Pack

This folder was reduced to a compact `.grp` regression pack for fast format checks.

Selection goals:
- Keep one or two representatives for each useful failure mode
- Preserve coverage across minimal containers, logic-only assets, organic meshes, terrain, built structures, and vehicles
- Remove family alternates that mostly repeat the same structural signal

## Kept Files (12)

### Minimal / Edge Containers
- splines.grp
- water_decals.grp

### Logic / Material
- apmat.grp
- gm_logic.grp

### Organic Mesh Stress
- bushes.grp
- wood_vine.grp

### Terrain / Irregular Topology
- vietnam_rocks.grp

### Built Structure
- bunker_entrence.grp

### Large Aggregate Environment
- avg_volokolamsk.grp

### Vehicle Classes
- jp_a6m2_n.grp
- usa_towed_at_m40.grp
- fr_pships_weaponry.grp

## Why the rest were removed

The removed files were useful during discovery, but most became redundant once the folder already had:
- one tiny container with almost no scene content
- one tiny decal-style asset
- one material/template-like sample
- one logic-style sample
- one dense foliage sample
- one thin organic/strand-like sample
- one irregular rock/terrain sample
- one compact structure sample
- one large aggregate world sample
- multiple vehicle classes

This 12-file set is the fastest pack that still gives broad signal for regression testing.

## Extraction-Validated Priorities

`dumpGrp -exp` was run against all 12 files.

Best files for current `grp_converter` learning work (`.B4B7D9C4` present after extraction):
- fr_pships_weaponry.grp
- jp_a6m2_n.grp
- usa_towed_at_m40.grp
- water_decals.grp

Highest-value geometry sample in this compact set:
- fr_pships_weaponry.grp: 9 `.B4B7D9C4` blobs + 9 skeleton resources

The other 8 files still passed extraction, but they are more useful for container/resource-typing coverage than for the current B4 geometry pipeline.

See `TEST_MATRIX.md` for the per-file extraction results.

Current converter status on the B4 subset:
- Native geometry samples: `fr_pships_weaponry.grp`, `jp_a6m2_n.grp`, `usa_towed_at_m40.grp`
- Synthetic fallback sample: `water_decals.grp`

Reference structure notes:
- Positive repeated-layout sample: `example/model/fr_pships_weaponry__grp_extract/STRUCTURE.md`
- Tiny special-case sample: `example/model/water_decals__grp_extract/STRUCTURE.md`

See `B4_CONVERTER_MATRIX.md` for the geometry-quality matrix.
