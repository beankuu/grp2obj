use anyhow::{bail, Context, Result};
use clap::Parser;
use oozextract::Extractor;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Cursor, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use tempfile::tempdir;

const ACE50000_MAGIC: u32 = 0xACE50001;
const ACE50000_FLAG_COMPRESSED: u32 = 0x4000_0000;
const ACE50000_SIZE_MASK: u32 = 0x3fff_ffff;
const MAX_ABS_COORD: f32 = 500_000.0;

#[derive(Parser, Debug)]
#[command(author, version, about = "GRP extractor (Rust migration WIP)")]
struct Cli {
    /// Input .grp file
    grp_file: PathBuf,

    /// Output directory (default: next to grp, named by stem)
    output_dir: Option<PathBuf>,

    /// Verbose logging
    #[arg(short, long)]
    verbose: bool,
}

#[derive(Debug, Default, Deserialize)]
struct Config {
    _oodle: Option<String>,
    #[serde(rename = "dumpGrp")]
    dump_grp: Option<String>,
}

#[derive(Debug, Clone)]
struct Mesh {
    name: String,
    variant: String,
    vertices: Vec<(f32, f32, f32)>,
    faces: Vec<(u32, u32, u32)>,
    lines: Vec<(u32, u32)>,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    run(cli)
}

fn run(cli: Cli) -> Result<()> {
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
    let dumpgrp_path = resolve_dumpgrp_path(&root, cfg.dump_grp.as_deref())?;

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

fn discover_root() -> PathBuf {
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd);
    }

    for p in candidates {
        if p.join("lib").exists() {
            return p;
        }
    }

    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn load_config(root: &Path) -> Config {
    let cfg_path = root.join("config.json");
    let txt = match fs::read_to_string(&cfg_path) {
        Ok(v) => v,
        Err(_) => return Config::default(),
    };
    serde_json::from_str(&txt).unwrap_or_default()
}

fn resolve_dumpgrp_path(root: &Path, from_cfg: Option<&str>) -> Result<PathBuf> {
    let p = if let Some(raw) = from_cfg {
        let candidate = PathBuf::from(raw);
        if candidate.is_absolute() {
            candidate
        } else {
            root.join(candidate)
        }
    } else {
        root.join("lib").join("dumpGrp-dev.exe")
    };

    if !p.exists() {
        maybe_download_dumpgrp(root, &p)?;
    }

    if !p.exists() {
        bail!(
            "dumpGrp executable not found: {}\nexpected default path: {}",
            p.display(),
            root.join("lib").join("dumpGrp-dev.exe").display()
        );
    }
    Ok(p)
}

fn maybe_download_dumpgrp(root: &Path, target_path: &Path) -> Result<()> {
    println!(
        "dumpGrp executable not found at {}.",
        target_path.display()
    );
    print!(
        "Download latest tools-prebuild windows archive from DagorEngine releases now? [Y/n] "
    );
    io::stdout().flush().ok();

    let mut answer = String::new();
    io::stdin()
        .read_line(&mut answer)
        .context("Failed to read user input for dumpGrp download prompt")?;
    let answer = answer.trim().to_ascii_lowercase();
    let allow = answer.is_empty() || answer == "y" || answer == "yes";
    if !allow {
        return Ok(());
    }

    if !cfg!(target_os = "windows") {
        bail!(
            "Automatic dumpGrp download is only implemented on Windows. Please install dumpGrp-dev.exe manually."
        );
    }

    if let Some(parent) = target_path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("Failed to create directory {}", parent.display()))?;
    }

    let script = r#"
$ErrorActionPreference = 'Stop'
$root = $env:GRP2OBJ_ROOT
$target = $env:GRP2OBJ_DUMPGRP_TARGET
if (-not $root) { throw 'GRP2OBJ_ROOT env var is empty.' }
if (-not $target) { throw 'GRP2OBJ_DUMPGRP_TARGET env var is empty.' }
$tmp = Join-Path $root '.tmp_dumpgrp_bootstrap'
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Path $tmp | Out-Null

$api = 'https://api.github.com/repos/GaijinEntertainment/DagorEngine/releases/latest'
$headers = @{ 'User-Agent' = 'grp2obj-rust' }
$rel = Invoke-RestMethod -Uri $api -Headers $headers
$body = [string]$rel.body

$candidates = [regex]::Matches($body, 'https?://\S+', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase) |
    ForEach-Object {
        # Trim common markdown/punctuation wrappers around URLs from release text.
        $_.Value.TrimEnd(')', ']', '>', '"', "'", '.', ',')
    } |
    Where-Object {
        $_ -match 'tools-prebuild|tools-prebuilt' -and
        $_ -match 'windows' -and
        $_ -match '\.7z($|\?)'
    } |
    Select-Object -Unique

if (-not $candidates -or $candidates.Count -eq 0) {
  throw 'Could not find tools-prebuild windows .7z URL in latest release description body.'
}

# Prefer windows-x86_64 (or amd64), avoid arm64 unless no other option exists.
$url = $candidates |
    Where-Object { $_ -match 'windows[-_]?x86_64|windows[-_]?amd64' } |
    Select-Object -First 1
if (-not $url) {
    $url = $candidates |
        Where-Object { $_ -notmatch 'arm64|aarch64' } |
        Select-Object -First 1
}
if (-not $url) {
    $url = $candidates | Select-Object -First 1
}

$archive = Join-Path $tmp 'tools-prebuild-windows.7z'
Invoke-WebRequest -Uri $url -OutFile $archive -Headers $headers

$extract = Join-Path $tmp 'extract'
New-Item -ItemType Directory -Path $extract | Out-Null
tar -xf $archive -C $extract

$allExe = Get-ChildItem -Path $extract -Recurse -File | Where-Object { $_.Name -ieq 'dumpGrp-dev.exe' }
if (-not $allExe -or $allExe.Count -eq 0) {
  throw 'dumpGrp-dev.exe not found in extracted archive.'
}

# Choose x86_64/x64 binary if archive contains multiple architectures.
$exe = $allExe |
    Where-Object { $_.FullName -match 'x86_64|amd64|x64' } |
    Select-Object -First 1
if (-not $exe) {
    $exe = $allExe |
        Where-Object { $_.FullName -notmatch 'arm64|aarch64' } |
        Select-Object -First 1
}
if (-not $exe) {
    $exe = $allExe | Select-Object -First 1
}

$hostArch = [string]$env:PROCESSOR_ARCHITECTURE
if ($exe.FullName -match 'arm64|aarch64' -and $hostArch -notmatch 'ARM64') {
    throw ('Selected dumpGrp candidate is ARM64 on non-ARM host: ' + $exe.FullName)
}

Copy-Item -Path $exe.FullName -Destination $target -Force
Write-Host ('Installed dumpGrp to ' + $target)
"#;

    let status = Command::new("powershell")
        .env("GRP2OBJ_ROOT", root.display().to_string())
        .env(
            "GRP2OBJ_DUMPGRP_TARGET",
            target_path.display().to_string(),
        )
        .arg("-NoProfile")
        .arg("-ExecutionPolicy")
        .arg("Bypass")
        .arg("-Command")
        .arg(script)
        .status()
        .context("Failed to launch PowerShell for dumpGrp bootstrap")?;

    if !status.success() {
        bail!(
            "Automatic dumpGrp bootstrap failed. Please download tools-prebuild windows archive manually from DagorEngine releases and place dumpGrp-dev.exe at {}",
            target_path.display()
        );
    }

    Ok(())
}

fn run_dumpgrp(dumpgrp: &Path, grp: &Path, extract_dir: &Path, verbose: bool) -> Result<()> {
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

fn decode_meshes(extract_dir: &Path, verbose: bool) -> Result<Vec<Mesh>> {
    let mut out = Vec::new();

    // First pass: decode skeleton files and build WTM cache.
    let mut skeleton_cache: std::collections::HashMap<
        String,
        std::collections::HashMap<String, [f32; 12]>,
    > = std::collections::HashMap::new();
    for ent in fs::read_dir(extract_dir)
        .with_context(|| format!("Failed to read {}", extract_dir.display()))?
    {
        let ent = ent?;
        let p = ent.path();
        if !p.is_file() {
            continue;
        }
        let ext = p.extension().and_then(|x| x.to_str()).unwrap_or_default();
        if !ext.eq_ignore_ascii_case("56F81B6D") {
            continue;
        }
        let data = fs::read(&p).with_context(|| format!("Failed to read {}", p.display()))?;
        // Build skeleton WTM cache only; do not add bone line meshes to output
        if verbose {
            decode_geom_node_tree_meshes(&p, &data, verbose)?;
        }
        let filename = p.file_name().and_then(|x| x.to_str()).unwrap_or("");
        let skel_stem = filename.split('.').next().unwrap_or("").to_string();
        let mut cache_key = skel_stem.clone();
        if cache_key.ends_with("_skeleton") {
            cache_key = cache_key[..cache_key.len() - 9].to_string();
        }
        if let Some(wtm) = build_skeleton_wtm_map(&data) {
            skeleton_cache.insert(cache_key.to_lowercase(), wtm);
        }
    }

    // Second pass: decode mesh resources.
    for ent in fs::read_dir(extract_dir)
        .with_context(|| format!("Failed to read {}", extract_dir.display()))?
    {
        let ent = ent?;
        let p = ent.path();
        if !p.is_file() {
            continue;
        }
        let ext = p.extension().and_then(|x| x.to_str()).unwrap_or_default();
        let data = fs::read(&p).with_context(|| format!("Failed to read {}", p.display()))?;

        if ext.eq_ignore_ascii_case("ACE50000") {
            if let Some(mesh) = decode_ace50000_mesh(&p, &data, verbose)? {
                out.push(mesh);
            }
            continue;
        }

        if ext.eq_ignore_ascii_case("B4B7D9C4") {
            if let Some(mesh_list) = decode_b4_dynmodel(&p, &data, &skeleton_cache, verbose)? {
                out.extend(mesh_list);
            }
            continue;
        }

        if ext.eq_ignore_ascii_case("56F81B6D") {
            // Already decoded in first pass.
            continue;
        }
    }
    Ok(out)
}


fn decode_ace50000_mesh(path: &Path, data: &[u8], verbose: bool) -> Result<Option<Mesh>> {
    let payload = decode_ace_payload(data)?;

    let filename = path.file_name().and_then(|x| x.to_str()).unwrap_or("mesh.ACE50000");
    let stem = filename.split('.').next().unwrap_or("mesh").to_string();
    let variant = if stem.ends_with("_collision") {
        stem[..stem.len() - 10].to_string()
    } else {
        stem.clone()
    };

    // Try multi-section vertex/index decode first
    if let Some((verts, faces)) = try_multi_section_collision(&payload) {
        if verbose {
            println!("[GRP] ACE50000 collision decode hit (multi-section, {} vertices, {} faces): {}", verts.len(), faces.len(), filename);
        }
        return Ok(Some(Mesh { name: stem.clone(), variant, vertices: verts, faces, lines: Vec::new() }));
    }

    // Try structured vertex+index decode
    if let Some((verts, faces)) = try_structured_collision(&payload) {
        if verbose {
            println!("[GRP] ACE50000 collision decode hit (structured, {} vertices, {} faces): {}", verts.len(), faces.len(), filename);
        }
        return Ok(Some(Mesh { name: stem.clone(), variant, vertices: verts, faces, lines: Vec::new() }));
    }

    // Fallback: embedded cls-token AABB wireframe box
    if let Some(mut mesh) = try_embedded_cls_aabb_wire(&payload, &stem, verbose) {
        mesh.variant = variant;
        return Ok(Some(mesh));
    }

    Ok(None)
}

fn try_embedded_cls_aabb_wire(blob: &[u8], stem: &str, verbose: bool) -> Option<Mesh> {
    if blob.len() < 28 { return None; }
    let prefix = &blob[..blob.len().min(512)];
    let cls_token = extract_collision_cls_token(prefix)?;
    let min_x = le_f32(blob, 0).ok()?;
    let min_y = le_f32(blob, 4).ok()?;
    let min_z = le_f32(blob, 8).ok()?;
    let max_x = le_f32(blob, 16).ok()?;
    let max_y = le_f32(blob, 20).ok()?;
    let max_z = le_f32(blob, 24).ok()?;
    if !min_x.is_finite() || !min_y.is_finite() || !min_z.is_finite()
        || !max_x.is_finite() || !max_y.is_finite() || !max_z.is_finite() { return None; }
    let ex = max_x - min_x;
    let ey = max_y - min_y;
    let ez = max_z - min_z;
    if ex <= 0.0 || ey <= 0.0 || ez <= 0.0 { return None; }
    if ex > 1_000_000.0 || ey > 1_000_000.0 || ez > 1_000_000.0 { return None; }
    let vertices = vec![
        (min_x, min_y, min_z), (max_x, min_y, min_z),
        (max_x, max_y, min_z), (min_x, max_y, min_z),
        (min_x, min_y, max_z), (max_x, min_y, max_z),
        (max_x, max_y, max_z), (min_x, max_y, max_z),
    ];
    let lines = vec![
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ];
    if verbose {
        println!("[GRP] ACE50000 collision decode hit (embedded-cls-aabb-wire, {}, 8 vertices, 12 lines)", cls_token);
    }
    Some(Mesh {
        name: stem.to_string(),
        variant: stem.to_string(),
        vertices,
        faces: Vec::new(),
        lines,
    })
}

fn extract_collision_cls_token(prefix: &[u8]) -> Option<String> {
    let b_cls = b"_cls";
    let cls_b = b"cls_";
    let hit1 = prefix.windows(4).position(|w| w == b_cls);
    let hit2 = prefix.windows(4).position(|w| w == cls_b);
    let (hit, needle_len) = match (hit1, hit2) {
        (Some(a), Some(b)) => if a <= b { (a, 4) } else { (b, 4) },
        (Some(a), None) => (a, 4),
        (None, Some(b)) => (b, 4),
        (None, None) => return None,
    };
    let mut start = hit;
    while start > 0 {
        let c = prefix[start - 1];
        if c.is_ascii_alphanumeric() || c == b'_' { start -= 1; } else { break; }
    }
    let mut end = hit + needle_len;
    while end < prefix.len() {
        let c = prefix[end];
        if c.is_ascii_alphanumeric() || c == b'_' { end += 1; } else { break; }
    }
    if end - start < 5 { return None; }
    let s = std::str::from_utf8(&prefix[start..end]).ok()?;
    if s.is_ascii() { Some(s.to_lowercase()) } else { None }
}


fn build_skeleton_wtm_map(
    data: &[u8],
) -> Option<std::collections::HashMap<String, [f32; 12]>> {
    const STRIDE: usize = 160;
    const BASE: usize = 8;
    if data.len() < BASE + STRIDE {
        return None;
    }
    let num_nodes_raw = le_u32(data, 4).ok()?;
    if (num_nodes_raw & 0x8000_0000) != 0 {
        return None;
    }
    let num_nodes = (num_nodes_raw & 0x7fff_ffff) as usize;
    if num_nodes < 1 || num_nodes > 4096 {
        return None;
    }
    let ofs_packed = le_u32(data, 0).ok()?;
    let invalid_tm_ofs = (ofs_packed & 0x000f_ffff) as usize;
    let nodes_end = BASE + num_nodes * STRIDE;
    let names_end = BASE + invalid_tm_ofs;
    if nodes_end > data.len() || names_end > data.len() || names_end < nodes_end {
        return None;
    }

    let mut map = std::collections::HashMap::new();
    for i in 0..num_nodes {
        let base = BASE + i * STRIDE;
        let m_off = base + 64;
        let mut col: [f32; 16] = [0.0; 16];
        let mut ok = true;
        for (k, c) in col.iter_mut().enumerate() {
            match le_f32(data, m_off + k * 4) {
                Ok(v) => *c = v,
                Err(_) => {
                    ok = false;
                    break;
                }
            }
        }
        if !ok {
            continue;
        }
        // columns 0,1,2,3 first-3-floats = affine 3x4
        let wtm: [f32; 12] = [
            col[0], col[1], col[2],  // col0 x,y,z
            col[4], col[5], col[6],  // col1 x,y,z
            col[8], col[9], col[10], // col2 x,y,z
            col[12], col[13], col[14], // col3 x,y,z (translation)
        ];
        let name_ofs = le_u32(data, base + 152).ok()? as usize;
        let pos = BASE + name_ofs;
        if pos < nodes_end || pos >= data.len() {
            continue;
        }
        let mut end = pos;
        while end < names_end && end < data.len() && data[end] != 0 {
            end += 1;
        }
        if let Ok(name) = std::str::from_utf8(&data[pos..end]) {
            if !name.is_empty() {
                map.entry(name.to_string()).or_insert(wtm);
            }
        }
    }
    if map.is_empty() {
        None
    } else {
        Some(map)
    }
}

// VSDT byte sizes used by Dagor vertex declarations.
fn vsdt_size(vsdt: u8) -> usize {
    match vsdt {
        0x00 => 4, 0x01 => 8, 0x02 => 12, 0x03 => 16,
        0x04 => 4, 0x05 => 4, 0x06 => 4, 0x07 => 8,
        0x09 => 4, 0x0A => 8, 0x0B => 4, 0x0C => 8,
        0x0D => 4, 0x0E => 4, 0x0F => 4, 0x10 => 8,
        _ => 0,
    }
}

const VDATA_PACKED_IB: u32 = 0x200;
const VDATA_I32: u32 = 0x40;

fn decode_meshopt_index_sequence(buf: &[u8], index_count: usize) -> Option<Vec<u32>> {
    if index_count == 0 || buf.len() < 1 + index_count + 4 {
        return None;
    }
    if (buf[0] & 0xF0) != 0xD0 {
        return None;
    }
    let safe_end = buf.len() - 4;
    let mut pos = 1usize;
    let mut last = [0u32, 0u32];
    let mut out = Vec::with_capacity(index_count);
    for _ in 0..index_count {
        if pos >= safe_end {
            return None;
        }
        let lead = buf[pos] as u32;
        pos += 1;
        let v = if lead < 128 {
            lead
        } else {
            let mut v = lead & 0x7F;
            let mut shift = 7u32;
            let mut ok = false;
            for _ in 0..4 {
                if pos >= buf.len() {
                    return None;
                }
                let g = buf[pos] as u32;
                pos += 1;
                v |= (g & 0x7F) << shift;
                shift += 7;
                if g < 128 {
                    ok = true;
                    break;
                }
            }
            if !ok {
                return None;
            }
            v
        };
        let cur = (v & 1) as usize;
        let v = v >> 1;
        let d = ((v >> 1) ^ (v & 1).wrapping_neg()) as i32;
        let idx = (last[cur] as i64 + d as i64).rem_euclid(0x1_0000_0000) as u32;
        last[cur] = idx;
        out.push(idx);
    }
    if pos != safe_end {
        return None;
    }
    Some(out)
}

fn decode_b4_dynmodel(
    path: &Path,
    data: &[u8],
    skeleton_cache: &std::collections::HashMap<
        String,
        std::collections::HashMap<String, [f32; 12]>,
    >,
    verbose: bool,
) -> Result<Option<Vec<Mesh>>> {
    let filename = path
        .file_name()
        .and_then(|x| x.to_str())
        .unwrap_or("mesh.B4B7D9C4");
    let stem = filename.split('.').next().unwrap_or("mesh").to_string();

    if data.len() < 24 {
        return Ok(None);
    }

    let res_sz = le_u32(data, 0)? as usize;
    let t0 = le_u32(data, 4)?;
    let t1 = le_u32(data, 8)?;
    let vdata_count = le_u32(data, 12)? as usize;
    let hdr_sz_raw = le_u32(data, 16)?;

    if res_sz < 16 || res_sz > 4096 { return Ok(None); }
    if vdata_count == 0 || vdata_count > 64 { return Ok(None); }

    let mut pos = 20usize;

    let mut compr_type = 0u8;
    let mut mat_vdata_hdr_sz = hdr_sz_raw;
    if (mat_vdata_hdr_sz & 0xE000_0000) == 0xC000_0000 {
        compr_type = 3;
        mat_vdata_hdr_sz &= !0xC000_0000;
    } else if mat_vdata_hdr_sz & 0x8000_0000 != 0 {
        compr_type = 1;
        mat_vdata_hdr_sz = (!mat_vdata_hdr_sz).wrapping_add(1);
    } else if mat_vdata_hdr_sz & 0x4000_0000 != 0 {
        compr_type = 2;
        mat_vdata_hdr_sz &= !0x4000_0000;
    }
    let mat_vdata_hdr_sz = mat_vdata_hdr_sz as usize;
    if mat_vdata_hdr_sz < 32 || mat_vdata_hdr_sz > 64 * 1024 {
        return Ok(None);
    }
    if compr_type != 3 {
        if verbose {
            println!("[GRP] B4 skip (unsupported compr_type={}): {}", compr_type, filename);
        }
        return Ok(None);
    }

    // Skip tex pool if present
    if !(t0 == 0xFFFF_FFFF && t1 == 0xFFFF_FFFF) {
        if pos + 4 > data.len() { return Ok(None); }
        let tex_sz = le_u32(data, pos)? as usize;
        pos += 4;
        if tex_sz > 0 {
            if pos + tex_sz > data.len() { return Ok(None); }
            pos += tex_sz;
        }
    }

    // Read block header
    if pos + 4 > data.len() { return Ok(None); }
    let block_hdr = le_u32(data, pos)?;
    pos += 4;
    let block_tag = ((block_hdr >> 30) & 0x3) as u8;
    let block_len = (block_hdr & 0x3FFF_FFFF) as usize;
    let block_start = pos;
    let block_end = pos + block_len;
    if block_end > data.len() { return Ok(None); }

    let full: Vec<u8> = match block_tag {
        2 => {
            // Oodle
            if block_len < 4 { return Ok(None); }
            let raw_size = le_u32(data, block_start)? as usize;
            if raw_size == 0 || raw_size > 256 * 1024 * 1024 { return Ok(None); }
            let comp = &data[block_start + 4..block_end];
            let mut out_buf = vec![0u8; raw_size];
            let mut ex = Extractor::new();
            match ex.read_from_slice(comp, &mut out_buf) {
                Ok(n) if n > 0 => { out_buf.truncate(n); out_buf }
                _ => {
                    if verbose {
                        println!("[GRP] B4 skip (oozextract failed): {}", filename);
                    }
                    return Ok(None);
                }
            }
        }
        1 => {
            // zstd
            let body = &data[block_start..block_end];
            match zstd::stream::decode_all(Cursor::new(body)) {
                Ok(v) => v,
                Err(e) => {
                    if verbose {
                        println!("[GRP] B4 skip (zstd failed: {}): {}", e, filename);
                    }
                    return Ok(None);
                }
            }
        }
        _ => {
            if verbose {
                println!("[GRP] B4 skip (unsupported block_tag={}): {}", block_tag, filename);
            }
            return Ok(None);
        }
    };
    pos = block_end;

    if full.len() < mat_vdata_hdr_sz { return Ok(None); }

    // Parse vdata table pointer from header
    if full.len() < 24 { return Ok(None); }
    let vdata_dptr = le_u64(&full, 16)?;
    let vdata_ofs = (vdata_dptr & 0xFFFF_FFFF) as usize;
    let vd_count = ((vdata_dptr >> 32) & 0xFFFF_FFFF) as usize;
    if vd_count != vdata_count { return Ok(None); }
    if vdata_ofs + vd_count * 32 > full.len() { return Ok(None); }

    // Per-vdata: parse header, vertex/index streams
    struct VdataEntry {
        vert_num: usize,
        vert_stride: usize,
        pos_off: usize,
        pos_vsdt: u8,
        vb: Vec<u8>,
        indices: Option<Vec<u32>>,
    }
    let mut vb_buffers: Vec<VdataEntry> = Vec::with_capacity(vdata_count);

    let mut fpos = mat_vdata_hdr_sz;
    for i in 0..vd_count {
        let off = vdata_ofs + i * 32;
        let vert_num = le_u32(&full, off)? as usize;
        let w1 = le_u32(&full, off + 4)?;
        let w2 = le_u32(&full, off + 8)?;
        let flags = le_u32(&full, off + 12)?;
        let decl_ofs = le_u32(&full, off + 16)? as usize;
        let decl_cnt = le_u32(&full, off + 20)? as usize;

        let vert_stride = (w1 & 0xFF) as usize;
        let packed_idx_lo = ((w1 >> 8) & 0xFF_FFFF) as usize;
        let idx_size = (w2 & 0x0FFF_FFFF) as usize;
        let packed_idx_hi = ((w2 >> 28) & 0xF) as usize;
        let packed_idx_size = packed_idx_lo | (packed_idx_hi << 24);

        if vert_num > (1 << 24) || vert_stride < 4 || vert_stride > 128 {
            return Ok(None);
        }

        // Scan vertex declaration for position channel
        let mut pos_off_ch = 0usize;
        let mut pos_vsdt: u8 = 0x02; // default VSDT_FLOAT3
        let mut cur_off = 0usize;
        let mut found_pos = false;
        if decl_ofs > 0 && decl_ofs < full.len() && decl_cnt > 0 && decl_cnt < 16 {
            for k in 0..decl_cnt {
                let o = decl_ofs + k * 12;
                if o + 8 > full.len() { break; }
                let t_val = le_u32(&full, o).unwrap_or(0);
                let usage = le_i32(&full, o + 4).unwrap_or(-1);
                let vsdt = ((t_val >> 16) & 0xFF) as u8;
                let ch_size = vsdt_size(vsdt);
                if usage == 0 && !found_pos {
                    pos_off_ch = cur_off;
                    pos_vsdt = vsdt;
                    found_pos = true;
                }
                cur_off += ch_size;
            }
        }

        let vb_size = vert_num * vert_stride;
        if fpos + vb_size > full.len() { return Ok(None); }
        let vb = full[fpos..fpos + vb_size].to_vec();
        fpos += vb_size;

        let ib_bytes = if (flags & VDATA_PACKED_IB) != 0 { packed_idx_size } else { idx_size };
        let elem_size: usize = if (flags & VDATA_I32) != 0 { 4 } else { 2 };
        let idx_count = if elem_size > 0 { idx_size / elem_size } else { 0 };

        if fpos + ib_bytes > full.len() { return Ok(None); }
        let ib_buf = &full[fpos..fpos + ib_bytes];
        fpos += ib_bytes;

        let indices: Option<Vec<u32>> = if idx_count > 0 && ib_bytes > 0 {
            if (flags & VDATA_PACKED_IB) != 0 {
                decode_meshopt_index_sequence(ib_buf, idx_count)
            } else if elem_size == 2 {
                let mut v = Vec::with_capacity(idx_count);
                let mut ok = true;
                for k in 0..idx_count {
                    match le_u16(ib_buf, k * 2) {
                        Ok(x) => v.push(x as u32),
                        Err(_) => { ok = false; break; }
                    }
                }
                if ok { Some(v) } else { None }
            } else {
                let mut v = Vec::with_capacity(idx_count);
                let mut ok = true;
                for k in 0..idx_count {
                    match le_u32(ib_buf, k * 4) {
                        Ok(x) => v.push(x),
                        Err(_) => { ok = false; break; }
                    }
                }
                if ok { Some(v) } else { None }
            }
        } else {
            None
        };

        vb_buffers.push(VdataEntry { vert_num, vert_stride, pos_off: pos_off_ch, pos_vsdt, vb, indices });
    }

    // LodsResource dump
    if pos + res_sz > data.len() { return Ok(None); }
    let lods_dump = &data[pos..pos + res_sz];
    pos += res_sz;

    let lods_dptr = le_u64(lods_dump, 0)?;
    let lods_ofs = (lods_dptr & 0xFFFF_FFFF) as usize;
    let lod_count = ((lods_dptr >> 32) & 0xFFFF_FFFF) as usize;
    if lod_count < 1 || lod_count > 8 { return Ok(None); }
    if lods_ofs + lod_count * 16 > lods_dump.len() { return Ok(None); }

    let mut lod_descs: Vec<(usize, f32)> = Vec::with_capacity(lod_count);
    for i in 0..lod_count {
        let off = lods_ofs + i * 16;
        let scene_sz = le_u32(lods_dump, off)? as usize;
        let rng = le_f32(lods_dump, off + 8).unwrap_or(0.0);
        lod_descs.push((scene_sz, rng));
    }

    // Bound-pack params
    let mut bp_ofs = [0.0f32; 3];
    let mut bp_mul = [0.0f32; 3];
    if lods_dump.len() >= 72 {
        for k in 0..3 {
            bp_ofs[k] = le_f32(lods_dump, 40 + k * 4).unwrap_or(0.0);
            bp_mul[k] = le_f32(lods_dump, 56 + k * 4).unwrap_or(0.0);
        }
    }
    let bound_pack_used = bp_mul.iter().any(|v| *v != 0.0 && v.is_finite());

    // Node-name map
    if pos + 4 > data.len() { return Ok(None); }
    let nm_sz = le_u32(data, pos)? as usize;
    pos += 4;
    if nm_sz == 0 || nm_sz > 1024 * 1024 || pos + nm_sz > data.len() { return Ok(None); }
    let nm_dump = &data[pos..pos + nm_sz];
    pos += nm_sz;

    let mut nodeid_to_name: std::collections::HashMap<u16, String> = std::collections::HashMap::new();
    if nm_dump.len() >= 24 {
        let map_dptr = le_u64(nm_dump, 0).unwrap_or(0);
        let id_dptr = le_u64(nm_dump, 16).unwrap_or(0);
        let map_ofs = (map_dptr & 0xFFFF_FFFF) as usize;
        let map_count = ((map_dptr >> 32) & 0xFFFF_FFFF) as usize;
        let id_ofs = (id_dptr & 0xFFFF_FFFF) as usize;
        let id_count = ((id_dptr >> 32) & 0xFFFF_FFFF) as usize;

        let mut names_by_ord: Vec<String> = Vec::with_capacity(map_count);
        if map_count <= 4096 && map_ofs + map_count * 8 <= nm_dump.len() {
            for i in 0..map_count {
                let ptr_off = map_ofs + i * 8;
                let str_ofs = (le_u64(nm_dump, ptr_off).unwrap_or(0) & 0xFFFF_FFFF) as usize;
                if str_ofs >= nm_dump.len() {
                    names_by_ord.push(format!("node_{}", i));
                    continue;
                }
                let mut end = str_ofs;
                while end < nm_dump.len() && nm_dump[end] != 0 { end += 1; }
                let name = std::str::from_utf8(&nm_dump[str_ofs..end])
                    .unwrap_or("")
                    .to_string();
                names_by_ord.push(if name.is_empty() { format!("node_{}", i) } else { name });
            }
        }
        if id_count <= 4096 && id_ofs + id_count * 2 <= nm_dump.len() {
            for i in 0..id_count.min(names_by_ord.len()) {
                let nid = le_u16(nm_dump, id_ofs + i * 2).unwrap_or(0xFFFF);
                let name = &names_by_ord[i];
                if !name.is_empty() {
                    nodeid_to_name.entry(nid).or_insert_with(|| name.clone());
                }
            }
        }
    }

    // Look up skeleton WTM for this stem
    let wtm_lookup = skeleton_cache.get(&stem.to_lowercase());

    let mut meshes_out: Vec<Mesh> = Vec::new();
    for (lod_idx, (scene_sz, _lod_range)) in lod_descs.iter().enumerate() {
        if lod_idx != 0 {
            // Skip non-LOD0; advance pos over scene data
            if pos + scene_sz > data.len() { break; }
            let scene_dump = &data[pos..pos + scene_sz];
            let rigids_dptr = le_u64(scene_dump, 0).unwrap_or(0);
            let rigids_ofs = (rigids_dptr & 0xFFFF_FFFF) as usize;
            let rigids_count = ((rigids_dptr >> 32) & 0xFFFF_FFFF) as usize;
            pos += scene_sz;
            if rigids_ofs + rigids_count * 32 <= scene_dump.len() {
                for r in 0..rigids_count {
                    let roff = rigids_ofs + r * 32;
                    let mesh_sz = le_u32(scene_dump, roff).unwrap_or(0) as usize;
                    if pos + mesh_sz > data.len() { break; }
                    pos += mesh_sz;
                }
            }
            continue;
        }

        if *scene_sz == 0 || *scene_sz > 16 * 1024 * 1024 { continue; }
        if pos + scene_sz > data.len() { break; }
        let scene_dump = &data[pos..pos + scene_sz];
        pos += scene_sz;

        let rigids_dptr = match le_u64(scene_dump, 0) { Ok(v) => v, Err(_) => break };
        let rigids_ofs = (rigids_dptr & 0xFFFF_FFFF) as usize;
        let rigids_count = ((rigids_dptr >> 32) & 0xFFFF_FFFF) as usize;
        if rigids_count > 4096 { break; }
        if rigids_ofs + rigids_count * 32 > scene_dump.len() { break; }

        let mut rigid_descs: Vec<(usize, i32)> = Vec::with_capacity(rigids_count);
        for r in 0..rigids_count {
            let roff = rigids_ofs + r * 32;
            let mesh_sz = le_u32(scene_dump, roff).unwrap_or(0) as usize;
            let node_id = le_i32(scene_dump, roff + 24).unwrap_or(-1);
            rigid_descs.push((mesh_sz, node_id));
        }

        let mut name_use_count: std::collections::HashMap<String, u32> = std::collections::HashMap::new();
        for (_r_idx, (mesh_sz, node_id)) in rigid_descs.iter().enumerate() {
            if *mesh_sz == 0 || *mesh_sz > 16 * 1024 * 1024 { continue; }
            if pos + mesh_sz > data.len() { break; }
            let mesh_dump = &data[pos..pos + mesh_sz];
            pos += mesh_sz;

            if mesh_dump.len() < 40 { continue; }
            let elems_dptr = le_u64(mesh_dump, 0).unwrap_or(0);
            let telem_dptr = le_u64(mesh_dump, 16).unwrap_or(0);
            let deprecated_flag = le_u32(mesh_dump, 32).unwrap_or(0);

            let mut elems_ofs = (elems_dptr & 0xFFFF_FFFF) as usize;
            let mut elems_count = ((elems_dptr >> 32) & 0xFFFF_FFFF) as usize;
            if (deprecated_flag & 0x8000_0000) == 0 {
                let telem_ofs = (telem_dptr & 0xFFFF_FFFF) as usize;
                let telem_count = ((telem_dptr >> 32) & 0xFFFF_FFFF) as usize;
                if elems_count == 0 && telem_count > 0 {
                    elems_ofs = telem_ofs;
                    elems_count = telem_count;
                } else if telem_count > 0 && elems_ofs + elems_count * 48 == telem_ofs {
                    elems_count += telem_count;
                }
            }
            if elems_count > 1024 { continue; }
            if elems_ofs + elems_count * 48 > mesh_dump.len() { continue; }

            let mut verts_combined: Vec<(f32, f32, f32)> = Vec::new();
            let mut faces_combined: Vec<(u32, u32, u32)> = Vec::new();
            let mut base_v = 0u32;

            for e in 0..elems_count {
                let eoff = elems_ofs + e * 48;
                let vd_dptr = le_u64(mesh_dump, eoff + 16).unwrap_or(0);
                let vd_idx = (vd_dptr & 0xFFFF_FFFF) as usize;
                let sv = le_i32(mesh_dump, eoff + 28).unwrap_or(0) as i32;
                let numv = le_i32(mesh_dump, eoff + 32).unwrap_or(0) as i32;
                let si = le_i32(mesh_dump, eoff + 36).unwrap_or(0) as i32;
                let numf = le_i32(mesh_dump, eoff + 40).unwrap_or(0) as i32;
                let base_vertex = le_i32(mesh_dump, eoff + 44).unwrap_or(0) as i32;

                if vd_idx >= vb_buffers.len() { continue; }
                let entry = &vb_buffers[vd_idx];
                let vert_num = entry.vert_num;
                let v_stride = entry.vert_stride;
                let pos_off_ch = entry.pos_off;
                let pos_vsdt = entry.pos_vsdt;
                let vb = &entry.vb;
                let indices = &entry.indices;

                let vstart = (base_vertex + sv) as usize;
                if numv <= 0 || vstart + numv as usize > vert_num { continue; }

                let pos_size: usize = if pos_vsdt == 0x0A { 8 } else { 12 };
                let effective_vsdt = if pos_vsdt == 0x02 || pos_vsdt == 0x0A { pos_vsdt }
                    else if v_stride >= 12 { 0x02 } else { 0x0A };

                let mut local_verts: Vec<(f32, f32, f32)> = Vec::with_capacity(numv as usize);
                let mut ok = true;
                for vi in 0..numv as usize {
                    let po = (vstart + vi) * v_stride + pos_off_ch;
                    if po + pos_size > vb.len() { ok = false; break; }
                    let (x, y, z) = if effective_vsdt == 0x0A {
                        let xi = le_i16(vb, po).unwrap_or(0) as f32;
                        let yi = le_i16(vb, po + 2).unwrap_or(0) as f32;
                        let zi = le_i16(vb, po + 4).unwrap_or(0) as f32;
                        if bound_pack_used {
                            (xi * bp_mul[0] / 32767.0 + bp_ofs[0],
                             yi * bp_mul[1] / 32767.0 + bp_ofs[1],
                             zi * bp_mul[2] / 32767.0 + bp_ofs[2])
                        } else {
                            (xi / 32767.0, yi / 32767.0, zi / 32767.0)
                        }
                    } else {
                        (le_f32(vb, po).unwrap_or(0.0),
                         le_f32(vb, po + 4).unwrap_or(0.0),
                         le_f32(vb, po + 8).unwrap_or(0.0))
                    };
                    if !x.is_finite() || !y.is_finite() || !z.is_finite() { ok = false; break; }
                    local_verts.push((x, y, z));
                }
                if !ok || local_verts.is_empty() { continue; }

                if let Some(idx_list) = indices {
                    if numf > 0 && si >= 0 {
                        let si = si as usize;
                        let numf = numf as usize;
                        if si + numf * 3 <= idx_list.len() {
                            for f in 0..numf {
                                let i0 = idx_list[si + f * 3] as i64 - sv as i64;
                                let i1 = idx_list[si + f * 3 + 1] as i64 - sv as i64;
                                let i2 = idx_list[si + f * 3 + 2] as i64 - sv as i64;
                                if i0 >= 0 && i1 >= 0 && i2 >= 0
                                    && (i0 as usize) < local_verts.len()
                                    && (i1 as usize) < local_verts.len()
                                    && (i2 as usize) < local_verts.len()
                                {
                                    faces_combined.push((
                                        base_v + i0 as u32,
                                        base_v + i1 as u32,
                                        base_v + i2 as u32,
                                    ));
                                }
                            }
                        }
                    }
                }
                verts_combined.extend_from_slice(&local_verts);
                base_v += local_verts.len() as u32;
            }

            if verts_combined.is_empty() { continue; }

            let nid = (*node_id).max(0) as u16;
            let node_name = nodeid_to_name
                .get(&nid)
                .cloned()
                .unwrap_or_else(|| format!("node{}", node_id));

            // Apply skeleton WTM if available
            if let Some(wtm_map) = wtm_lookup {
                let w = wtm_map.get(&node_name)
                    .or_else(|| {
                        if node_name.starts_with('@') {
                            wtm_map.get(&node_name[1..])
                        } else {
                            None
                        }
                    });
                if let Some(w) = w {
                    let [c0x, c0y, c0z, c1x, c1y, c1z, c2x, c2y, c2z, c3x, c3y, c3z] = *w;
                    for v in verts_combined.iter_mut() {
                        let (vx, vy, vz) = *v;
                        *v = (
                            c0x * vx + c1x * vy + c2x * vz + c3x,
                            c0y * vx + c1y * vy + c2y * vz + c3y,
                            c0z * vx + c1z * vy + c2z * vz + c3z,
                        );
                    }
                }
            }

            let use_n = name_use_count.entry(node_name.clone()).or_insert(0);
            let disambig = if *use_n > 0 { format!("_{}", use_n) } else { String::new() };
            *use_n += 1;
            let obj_name = format!("{}_lod{}_{}{}", stem, lod_idx, node_name, disambig);

            meshes_out.push(Mesh {
                name: obj_name,
                variant: stem.clone(),
                vertices: verts_combined,
                faces: faces_combined,
                lines: Vec::new(),
            });
        }
    }

    if meshes_out.is_empty() {
        if verbose {
            println!("[GRP] B4 DynModel: parsed but no meshes: {}", filename);
        }
        return Ok(None);
    }
    if verbose {
        println!(
            "[GRP] DynModel decode hit ({} LODs, {} rigid meshes): {}",
            lod_count,
            meshes_out.len(),
            filename
        );
    }
    Ok(Some(meshes_out))
}

fn le_i16(data: &[u8], off: usize) -> Result<i16> {
    let end = off + 2;
    if end > data.len() {
        bail!("short read i16 at {}", off);
    }
    let mut b = [0u8; 2];
    b.copy_from_slice(&data[off..end]);
    Ok(i16::from_le_bytes(b))
}

fn decode_ace_payload(data: &[u8]) -> Result<Vec<u8>> {
    if data.len() < 8 {
        bail!("ACE50000 blob too small");
    }
    let magic = le_u32(data, 0)?;
    if magic != ACE50000_MAGIC {
        bail!("Invalid ACE50000 magic");
    }
    let sf = le_u32(data, 4)?;
    let payload_size = (sf & ACE50000_SIZE_MASK) as usize;
    let compressed = (sf & ACE50000_FLAG_COMPRESSED) != 0;

    let start = 8usize;
    let end = (start + payload_size).min(data.len());
    let body = &data[start..end];
    if !compressed {
        return Ok(body.to_vec());
    }

    let out = zstd::stream::decode_all(Cursor::new(body))
        .context("Failed to zstd-decompress ACE50000 payload")?;
    Ok(out)
}

fn try_multi_section_collision(
    data: &[u8],
) -> Option<(Vec<(f32, f32, f32)>, Vec<(u32, u32, u32)>)> {
    if data.len() < 80 {
        return None;
    }
    let marker = [0x1B, 0x00, 0x04, 0x00];
    let mut all_verts: Vec<(f32, f32, f32)> = Vec::new();
    let mut all_faces: Vec<(u32, u32, u32)> = Vec::new();

    let mut i = 0usize;
    let n = data.len();
    while i + 80 < n {
        let mut found = None;
        let mut j = i;
        while j + 4 <= n {
            if data[j..j + 4] == marker {
                found = Some(j);
                break;
            }
            j += 1;
        }
        let Some(m) = found else {
            break;
        };

        let vcount_off = m + 60;
        if vcount_off + 4 > n {
            break;
        }
        let vcount = le_u32(data, vcount_off).ok()? as usize;
        if !(3..=200_000).contains(&vcount) {
            i = m + 4;
            continue;
        }

        let stride = 16usize;
        let vstart = vcount_off + 4;
        let vend = vstart.checked_add(vcount.checked_mul(stride)?)?;
        if vend + 4 > n {
            i = m + 4;
            continue;
        }

        let icount = le_u32(data, vend).ok()? as usize;
        if icount < 3 || icount > 5_000_000 || (icount % 3) != 0 {
            i = m + 4;
            continue;
        }
        let istart = vend + 4;
        let iend = istart.checked_add(icount.checked_mul(2)?)?;
        if iend > n {
            i = m + 4;
            continue;
        }

        let mut verts = Vec::with_capacity(vcount);
        let mut ok = true;
        for v in 0..vcount {
            let o = vstart + v * stride;
            let x = le_f32(data, o).ok()?;
            let y = le_f32(data, o + 4).ok()?;
            let z = le_f32(data, o + 8).ok()?;
            if !x.is_finite() || !y.is_finite() || !z.is_finite() {
                ok = false;
                break;
            }
            if x.abs() > MAX_ABS_COORD || y.abs() > MAX_ABS_COORD || z.abs() > MAX_ABS_COORD {
                ok = false;
                break;
            }
            verts.push((x, y, z));
        }
        if !ok {
            i = m + 4;
            continue;
        }

        let mut idx = Vec::with_capacity(icount);
        let mut max_idx = 0usize;
        for k in 0..icount {
            let v = le_u16(data, istart + k * 2).ok()? as usize;
            if v > max_idx {
                max_idx = v;
            }
            idx.push(v as u32);
        }
        if max_idx >= vcount {
            i = m + 4;
            continue;
        }

        let base = all_verts.len() as u32;
        all_verts.extend(verts);
        for k in (0..icount).step_by(3) {
            all_faces.push((base + idx[k], base + idx[k + 1], base + idx[k + 2]));
        }

        i = iend;
    }

    if all_verts.len() >= 8 && all_faces.len() >= 16 {
        Some((all_verts, all_faces))
    } else {
        None
    }
}

fn try_structured_collision(data: &[u8]) -> Option<(Vec<(f32, f32, f32)>, Vec<(u32, u32, u32)>)> {
    for stride in [16usize, 12usize] {
        if let Some(hit) = try_structured_with_stride(data, stride) {
            return Some(hit);
        }
    }
    None
}

fn try_structured_with_stride(
    data: &[u8],
    stride: usize,
) -> Option<(Vec<(f32, f32, f32)>, Vec<(u32, u32, u32)>)> {
    let n = data.len();
    if n < 36 {
        return None;
    }
    let limit = usize::min(512, n.saturating_sub(12));
    let mut off = 0usize;
    while off < limit {
        let vcount = le_u32(data, off).ok()? as usize;
        if !(4..=200_000).contains(&vcount) {
            off += 4;
            continue;
        }
        let vstart = off + 4;
        let vend = vstart.checked_add(vcount.checked_mul(stride)?)?;
        if vend + 4 > n {
            off += 4;
            continue;
        }
        let icount = le_u32(data, vend).ok()? as usize;
        if icount < 12 || icount > 1_000_000 || (icount % 3) != 0 {
            off += 4;
            continue;
        }
        let istart = vend + 4;
        let iend = istart.checked_add(icount.checked_mul(2)?)?;
        if iend != n {
            off += 4;
            continue;
        }

        let mut verts = Vec::with_capacity(vcount);
        let mut good = true;
        for i in 0..vcount {
            let base = vstart + i * stride;
            let x = le_f32(data, base).ok()?;
            let y = le_f32(data, base + 4).ok()?;
            let z = le_f32(data, base + 8).ok()?;
            if !x.is_finite() || !y.is_finite() || !z.is_finite() {
                good = false;
                break;
            }
            if x.abs() > MAX_ABS_COORD || y.abs() > MAX_ABS_COORD || z.abs() > MAX_ABS_COORD {
                good = false;
                break;
            }
            verts.push((x, y, z));
        }
        if !good {
            off += 4;
            continue;
        }

        let mut idx = Vec::with_capacity(icount);
        let mut max_idx = 0usize;
        for i in 0..icount {
            let v = le_u16(data, istart + i * 2).ok()? as usize;
            if v > max_idx {
                max_idx = v;
            }
            idx.push(v as u32);
        }
        if max_idx >= vcount {
            off += 4;
            continue;
        }

        let mut faces = Vec::with_capacity(icount / 3);
        for i in (0..icount).step_by(3) {
            faces.push((idx[i], idx[i + 1], idx[i + 2]));
        }
        return Some((verts, faces));
    }
    None
}

fn decode_geom_node_tree_meshes(path: &Path, data: &[u8], verbose: bool) -> Result<Vec<Mesh>> {
    const STRIDE: usize = 160;
    const BASE: usize = 8;

    if data.len() < BASE + STRIDE {
        return Ok(Vec::new());
    }

    let ofs_packed = le_u32(data, 0)?;
    let num_nodes_raw = le_u32(data, 4)?;
    let compressed = (num_nodes_raw & 0x8000_0000) != 0;
    if compressed {
        return Ok(Vec::new());
    }

    let num_nodes = (num_nodes_raw & 0x7fff_ffff) as usize;
    if num_nodes < 1 || num_nodes > 4096 {
        return Ok(Vec::new());
    }

    let invalid_tm_ofs = (ofs_packed & 0x000f_ffff) as usize;
    let nodes_end = BASE + num_nodes * STRIDE;
    let names_end = BASE + invalid_tm_ofs;
    if nodes_end > data.len() || names_end > data.len() || names_end < nodes_end {
        return Ok(Vec::new());
    }

    let read_name = |name_ofs: usize| -> String {
        let pos = BASE + name_ofs;
        if pos < nodes_end || pos >= data.len() {
            return String::new();
        }
        let mut end = pos;
        while end < names_end && end < data.len() && data[end] != 0 {
            end += 1;
        }
        if end <= pos || end > data.len() {
            return String::new();
        }
        match std::str::from_utf8(&data[pos..end]) {
            Ok(s) => s.to_string(),
            Err(_) => String::new(),
        }
    };

    let mut wpos: Vec<(f32, f32, f32)> = Vec::with_capacity(num_nodes);
    let mut names: Vec<String> = Vec::with_capacity(num_nodes);
    let mut parents: Vec<i32> = vec![-1; num_nodes];
    let mut child_first: Vec<i32> = vec![-1; num_nodes];
    let mut child_count: Vec<i32> = vec![0; num_nodes];

    for i in 0..num_nodes {
        let base = BASE + i * STRIDE;
        let x = le_f32(data, base + 64 + 48)?;
        let y = le_f32(data, base + 64 + 52)?;
        let z = le_f32(data, base + 64 + 56)?;
        if !x.is_finite() || !y.is_finite() || !z.is_finite() {
            return Ok(Vec::new());
        }
        if x.abs() > 1_000_000.0 || y.abs() > 1_000_000.0 || z.abs() > 1_000_000.0 {
            return Ok(Vec::new());
        }
        wpos.push((x, y, z));

        let child_packed = le_u64(data, base + 128)?;
        let parent_int = le_i32(data, base + 144)?;
        let name_ofs = le_u32(data, base + 152)? as usize;
        names.push(read_name(name_ofs));

        let cc = ((child_packed >> 32) & 0xffff_ffff) as i32;
        let cofs = (child_packed & 0xffff_ffff) as usize;
        if cc > 0 && cofs % STRIDE == 0 {
            let cf = (cofs / STRIDE) as i32;
            if cf >= 0 && (cf as usize + cc as usize) <= num_nodes {
                child_first[i] = cf;
                child_count[i] = cc;
            }
        }

        if parent_int >= 0 && (parent_int as usize) < num_nodes * STRIDE && (parent_int as usize) % STRIDE == 0 {
            parents[i] = (parent_int as usize / STRIDE) as i32;
        }
    }

    let mut derived = vec![-1i32; num_nodes];
    for i in 0..num_nodes {
        let cf = child_first[i];
        let cc = child_count[i];
        if cf < 0 || cc <= 0 {
            continue;
        }
        for k in 0..cc {
            let child = cf + k;
            if child >= 0 && (child as usize) < num_nodes {
                derived[child as usize] = i as i32;
            }
        }
    }
    if derived.iter().any(|&x| x >= 0) {
        for i in 0..num_nodes {
            if derived[i] >= 0 {
                parents[i] = derived[i];
            } else if parents[i] != -1 && i != 0 {
                parents[i] = -1;
            }
        }
    }

    let filename = path.file_name().and_then(|x| x.to_str()).unwrap_or("skeleton.56F81B6D");
    let skel_stem = filename.split('.').next().unwrap_or("skeleton").to_string();
    let variant = if skel_stem.ends_with("_skeleton") {
        skel_stem[..skel_stem.len() - 9].to_string()
    } else {
        skel_stem.clone()
    };

    let mut meshes = Vec::with_capacity(num_nodes);
    let mut used: BTreeMap<String, u32> = BTreeMap::new();
    for i in 0..num_nodes {
        let base_name = if !names[i].is_empty() {
            names[i].clone()
        } else if i == 0 {
            format!("{}_root", skel_stem)
        } else {
            format!("bone_{:03}", i)
        };
        let cnt = used.entry(base_name.clone()).or_insert(0);
        *cnt += 1;
        let uniq = if *cnt == 1 {
            base_name
        } else {
            format!("{}_{}", base_name, *cnt)
        };

        let self_pos = wpos[i];
        let p = parents[i];
        let parent_pos = if p >= 0 && (p as usize) < wpos.len() && (p as usize) != i {
            wpos[p as usize]
        } else {
            (self_pos.0, self_pos.1 + 0.01, self_pos.2)
        };

        meshes.push(Mesh {
            name: uniq,
            variant: variant.clone(),
            vertices: vec![self_pos, parent_pos],
            faces: Vec::new(),
            lines: vec![(0, 1)],
        });
    }

    if verbose && !meshes.is_empty() {
        println!(
            "[GRP] GeomNodeTree decode hit ({} nodes, {} bone meshes)",
            num_nodes,
            meshes.len()
        );
    }

    Ok(meshes)
}

fn export_split_variants(meshes: &[Mesh], output_dir: &Path) -> Result<Vec<PathBuf>> {
    fs::create_dir_all(output_dir)
        .with_context(|| format!("Failed to create output dir {}", output_dir.display()))?;

    let mut groups: BTreeMap<String, Vec<Mesh>> = BTreeMap::new();
    for m in meshes {
        groups.entry(m.variant.clone()).or_default().push(m.clone());
    }

    let mut out_files = Vec::new();
    for (variant, group) in groups {
        let out = output_dir.join(format!("{}.obj", variant));
        export_obj(&group, &out)?;
        out_files.push(out);
    }
    Ok(out_files)
}

fn export_obj(meshes: &[Mesh], out: &Path) -> Result<()> {
    let mut s = String::new();
    s.push_str("# Converted from Dagor GRP resources\n");
    s.push_str(&format!("# {} mesh objects\n\n", meshes.len()));

    let mut v_offset: u32 = 1;
    for m in meshes {
        s.push_str(&format!("o {}\n", m.name));
        s.push_str(&format!(
            "# Vertices: {}, Faces: {}, Lines: {}\n",
            m.vertices.len(),
            m.faces.len(),
            m.lines.len()
        ));

        for (x, y, z) in &m.vertices {
            s.push_str(&format!("v {} {} {}\n", x, y, z));
        }
        for (a, b, c) in &m.faces {
            s.push_str(&format!("f {} {} {}\n", a + v_offset, b + v_offset, c + v_offset));
        }
        for (a, b) in &m.lines {
            s.push_str(&format!("l {} {}\n", a + v_offset, b + v_offset));
        }
        s.push('\n');
        v_offset += m.vertices.len() as u32;
    }

    fs::write(out, s).with_context(|| format!("Failed to write {}", out.display()))?;
    Ok(())
}

fn le_u32(data: &[u8], off: usize) -> Result<u32> {
    let end = off + 4;
    if end > data.len() {
        bail!("short read u32 at {}", off);
    }
    let mut b = [0u8; 4];
    b.copy_from_slice(&data[off..end]);
    Ok(u32::from_le_bytes(b))
}

fn le_u16(data: &[u8], off: usize) -> Result<u16> {
    let end = off + 2;
    if end > data.len() {
        bail!("short read u16 at {}", off);
    }
    let mut b = [0u8; 2];
    b.copy_from_slice(&data[off..end]);
    Ok(u16::from_le_bytes(b))
}

fn le_u64(data: &[u8], off: usize) -> Result<u64> {
    let end = off + 8;
    if end > data.len() {
        bail!("short read u64 at {}", off);
    }
    let mut b = [0u8; 8];
    b.copy_from_slice(&data[off..end]);
    Ok(u64::from_le_bytes(b))
}

fn le_i32(data: &[u8], off: usize) -> Result<i32> {
    let end = off + 4;
    if end > data.len() {
        bail!("short read i32 at {}", off);
    }
    let mut b = [0u8; 4];
    b.copy_from_slice(&data[off..end]);
    Ok(i32::from_le_bytes(b))
}

fn le_f32(data: &[u8], off: usize) -> Result<f32> {
    let end = off + 4;
    if end > data.len() {
        bail!("short read f32 at {}", off);
    }
    let mut b = [0u8; 4];
    b.copy_from_slice(&data[off..end]);
    Ok(f32::from_le_bytes(b))
}
