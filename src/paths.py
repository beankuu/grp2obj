#!/usr/bin/env python3
"""Path and config helpers for converter pipeline."""

import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional


def _frozen_exe_dir() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    try:
        return Path(sys.executable).resolve().parent
    except Exception:
        return None


def _frozen_bundle_dir() -> Optional[Path]:
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    try:
        return Path(base).resolve()
    except Exception:
        return None


def project_root() -> Path:
    """Best-effort project root detection for flexible source layout."""
    here = Path(__file__).resolve().parent

    if getattr(sys, "frozen", False):
        exe_dir = _frozen_exe_dir()
        cwd = Path.cwd().resolve()
        if exe_dir is not None and (exe_dir / "lib").exists():
            return exe_dir
        if (cwd / "lib").exists():
            return cwd

    candidates: List[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = _frozen_exe_dir()
        if exe_dir is not None:
            candidates.append(exe_dir)
        candidates.append(Path.cwd().resolve())
    candidates.extend([here] + list(here.parents))

    for p in candidates:
        if (p / "config.json").exists() and (p / "lib").exists():
            return p
    for p in candidates:
        if (p / "lib").exists():
            return p
    if getattr(sys, "frozen", False):
        exe_dir = _frozen_exe_dir()
        if exe_dir is not None:
            return exe_dir
    if here.name == "src":
        return here.parent
    return here


def _load_config() -> Dict[str, Any]:
    candidates: List[Path] = []
    exe_dir = _frozen_exe_dir()
    bundle_dir = _frozen_bundle_dir()

    if exe_dir is not None:
        candidates.append(exe_dir / "config.json")
    candidates.append(Path.cwd().resolve() / "config.json")
    if bundle_dir is not None:
        candidates.append(bundle_dir / "config.json")
    candidates.append(project_root() / "config.json")

    seen: set[str] = set()
    for config_path in candidates:
        key = str(config_path)
        if key in seen:
            continue
        seen.add(key)
        if not config_path.exists():
            continue
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _resolve_cfg_path(value: str) -> str:
    p = Path(value)
    if p.is_absolute():
        return str(p.resolve())
    return str((project_root() / p).resolve())


def resolve_oodle_from_config() -> Optional[str]:
    """Resolve path to ooz-wasm decompressor wrapper."""
    cfg = _load_config()
    oodle_path = cfg.get("oodle")
    if isinstance(oodle_path, str) and oodle_path:
        return _resolve_cfg_path(oodle_path)
    default = project_root() / "lib" / "oo2core_9_win64.dll"
    return str(default.resolve())


def resolve_dumpgrp_from_config() -> Optional[str]:
    cfg = _load_config()
    dg = cfg.get("dumpGrp")
    if isinstance(dg, str) and dg:
        return _resolve_cfg_path(dg)
    default = project_root() / "lib" / "dumpGrp-dev.exe"
    return str(default.resolve())
