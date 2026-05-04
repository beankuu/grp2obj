use anyhow::{bail, Context, Result};
use serde::Deserialize;
use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Default, Deserialize)]
pub(crate) struct Config {
    _oodle: Option<String>,
    #[serde(rename = "dumpGrp")]
    dump_grp: Option<String>,
    #[serde(rename = "dumpGrpPath")]
    dump_grp_path: Option<String>,
}

impl Config {
    fn configured_dumpgrp(&self) -> Option<&str> {
        self.dump_grp_path
            .as_deref()
            .or(self.dump_grp.as_deref())
    }
}

pub(crate) fn discover_root() -> PathBuf {
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

pub(crate) fn load_config(root: &Path) -> Config {
    let cfg_path = root.join("config.json");
    let txt = match fs::read_to_string(&cfg_path) {
        Ok(v) => v,
        Err(_) => return Config::default(),
    };
    serde_json::from_str(&txt).unwrap_or_default()
}

pub(crate) fn resolve_dumpgrp_path(root: &Path, cfg: &Config) -> Result<PathBuf> {
    let from_env = env::var("GRP2OBJ_DUMPGRP").ok();
    let configured = from_env.as_deref().or(cfg.configured_dumpgrp());

    if let Some(raw) = configured {
        let p = resolve_path(root, raw);
        if p.exists() {
            return Ok(p);
        }
        if cfg!(target_os = "windows") {
            maybe_download_dumpgrp(root, &p)?;
            if p.exists() {
                return Ok(p);
            }
        }
        bail!(
            "Configured dumpGrp executable not found at {}. Set config.json dumpGrpPath (or dumpGrp) or GRP2OBJ_DUMPGRP to a valid executable path.",
            p.display()
        );
    }

    for candidate in default_candidates(root) {
        if candidate.exists() {
            return Ok(candidate);
        }
    }

    if let Some(in_path) = find_dumpgrp_on_path() {
        return Ok(in_path);
    }

    let default_target = if cfg!(target_os = "windows") {
        root.join("lib").join("dumpGrp-dev.exe")
    } else {
        root.join("lib").join("dumpGrp-dev")
    };

    if cfg!(target_os = "windows") {
        maybe_download_dumpgrp(root, &default_target)?;
        if default_target.exists() {
            return Ok(default_target);
        }
    }

    bail!(
        "dumpGrp executable not found. Checked config.json dumpGrpPath/dumpGrp, GRP2OBJ_DUMPGRP, default lib paths, and PATH. Expected default path: {}",
        default_target.display()
    )
}

fn resolve_path(root: &Path, raw: &str) -> PathBuf {
    let candidate = PathBuf::from(raw);
    if candidate.is_absolute() {
        candidate
    } else {
        root.join(candidate)
    }
}

fn default_candidates(root: &Path) -> Vec<PathBuf> {
    vec![
        root.join("lib").join("dumpGrp-dev.exe"),
        root.join("lib").join("dumpGrp-dev"),
        root.join("lib").join("dumpGrp"),
    ]
}

fn find_dumpgrp_on_path() -> Option<PathBuf> {
    let names: &[&str] = if cfg!(target_os = "windows") {
        &["dumpGrp-dev.exe", "dumpGrp.exe", "dumpGrp-dev", "dumpGrp"]
    } else {
        &["dumpGrp-dev", "dumpGrp", "dumpgrp"]
    };

    let path_var = env::var_os("PATH")?;
    for dir in env::split_paths(&path_var) {
        for name in names {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
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
