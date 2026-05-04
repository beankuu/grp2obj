use anyhow::{bail, Context, Result};
use clap::Parser;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

mod decode;
mod export;
mod pipeline;
mod settings;

#[derive(Parser, Debug)]
#[command(author, version, about = "GRP extractor (Rust migration WIP)")]
pub(crate) struct Cli {
    /// Input .grp file
    grp_file: PathBuf,

    /// Output directory (default: next to grp, named by stem)
    output_dir: Option<PathBuf>,

    /// Verbose logging
    #[arg(short, long)]
    verbose: bool,
}

#[derive(Debug, Clone)]
pub(crate) struct Mesh {
    name: String,
    variant: String,
    vertices: Vec<(f32, f32, f32)>,
    faces: Vec<(u32, u32, u32)>,
    lines: Vec<(u32, u32)>,
}

pub(crate) use decode::decode_meshes;
pub(crate) use export::export_split_variants;

fn main() -> Result<()> {
    let cli = Cli::parse();
    pipeline::run(cli)
}

pub(crate) fn run_dumpgrp(dumpgrp: &Path, grp: &Path, extract_dir: &Path, verbose: bool) -> Result<()> {
    let mut cmd = Command::new(dumpgrp);
    cmd.arg(grp)
        .arg(format!("-exp:{}", extract_dir.display()));

    if !verbose {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
    }

    let status = cmd
        .status()
        .with_context(|| format!("Failed to launch dumpGrp: {}", dumpgrp.display()))?;

    if !status.success() {
        bail!("dumpGrp failed with status: {}", status);
    }

    Ok(())
}
