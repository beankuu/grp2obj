#!/usr/bin/env python3
"""Path and config helpers for converter pipeline."""

import json
from pathlib import Path
from typing import Any, Dict, Optional


def project_root() -> Path:
    """Best-effort project root detection for flexible source layout."""
    here = Path(__file__).resolve().parent
    for p in [here] + list(here.parents):
        if (p / "config.json").exists() and (p / "lib").exists():
            return p
    if here.name == "src":
        return here.parent
    return here


def _load_config() -> Dict[str, Any]:
    config_path = project_root() / "config.json"
    if not config_path.exists():
        return {}
    try:
        parsed = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_cfg_path(value: str) -> str:
    p = Path(value)
    if p.is_absolute():
        return str(p.resolve())
    return str((project_root() / p).resolve())


def resolve_oodle_from_config() -> Optional[str]:
    cfg = _load_config()
    oo = cfg.get("oo2core")
    if not isinstance(oo, str) or not oo:
        return None
    return _resolve_cfg_path(oo)


def resolve_dumpgrp_from_config() -> Optional[str]:
    cfg = _load_config()
    dg = cfg.get("dumpGrp")
    if not isinstance(dg, str) or not dg:
        return None
    return _resolve_cfg_path(dg)
