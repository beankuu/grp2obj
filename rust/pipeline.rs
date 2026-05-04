use anyhow::{Context, Result};
use std::fs;
use tempfile::tempdir;

use crate::settings::{discover_root, load_config, resolve_dumpgrp_path};
use crate::{decode_meshes, export_split_variants, run_dumpgrp, Cli};

pub(crate) fn run(cli: Cli) -> Result<()> {
    let grp_path = fs::canonicalize(&cli.grp_file)
        .with_context(|| format!("GRP file not found: {}", cli.grp_file.display()))?;

    let output_dir = match cli.output_dir {
        Some(p) => p,
        None => {
            let parent = grp_path.parent().context("Input GRP has no parent directory")?;
            parent.join(
                grp_path
                    .file_stem()
                    .context("Input GRP has no file stem")?,
            )
        }
    };
    fs::create_dir_all(&output_dir)
        .with_context(|| format!("Failed to create output dir: {}", output_dir.display()))?;

    let root = discover_root();
    let cfg = load_config(&root);
    let dumpgrp_path = resolve_dumpgrp_path(&root, &cfg)?;

    let tmp = tempdir().context("Failed to create temporary extraction dir")?;
    let extract_dir = tmp.path();

    run_dumpgrp(&dumpgrp_path, &grp_path, extract_dir, cli.verbose)?;

    let meshes = decode_meshes(extract_dir, cli.verbose)?;
    if meshes.is_empty() {
        println!(
            "[{}] no meshes decoded.",
            grp_path
                .file_stem()
                .and_then(|x| x.to_str())
                .unwrap_or("grp")
        );
        println!("  hint: Rust port currently supports ACE50000 and B4B7D9C4 partial decode paths");
        return Ok(());
    }

    let out_files = export_split_variants(&meshes, &output_dir)?;
    println!(
        "[{}] {} collections -> {}",
        grp_path
            .file_stem()
            .and_then(|x| x.to_str())
            .unwrap_or("grp"),
        out_files.len(),
        output_dir.display()
    );

    Ok(())
}
