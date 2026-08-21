#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared JSON config loader with optional base_config inheritance."""

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged: Dict[str, Any] = {}
        for key in base:
            if key in override:
                merged[key] = _deep_merge(base[key], override[key])
            else:
                merged[key] = base[key]
        for key in override:
            if key not in merged:
                merged[key] = override[key]
        return merged
    return override


def _read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Config JSON must be an object: {path}")
    return obj


def _normalize_base_paths(value: Any) -> List[Path]:
    paths: List[Path] = []
    if isinstance(value, str):
        value = value.strip()
        if value:
            paths.append(Path(value))
        return paths

    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                item = item.strip()
                if item:
                    paths.append(Path(item))
    return paths


def read_json_with_base(path: Path, visited: Optional[Set[Path]] = None) -> Dict[str, Any]:
    path = path.resolve()
    if visited is None:
        visited = set()
    if path in visited:
        raise RuntimeError(f"Circular base_config reference detected: {path}")
    visited.add(path)

    cfg = _read_json(path)
    base_files: List[Path] = []

    base_config = cfg.get("base_config")
    if base_config is not None:
        base_files.extend(_normalize_base_paths(base_config))

    base_configs = cfg.get("base_configs")
    if base_configs is not None:
        base_files.extend(_normalize_base_paths(base_configs))

    if not base_files:
        return cfg

    merged: Dict[str, Any] = {}
    cfg_dir = path.parent
    for base_file in base_files:
        base_path = base_file if base_file.is_absolute() else (cfg_dir / base_file).resolve()
        if not base_path.exists():
            raise RuntimeError(f"Base config not found: {base_path} (referenced by {path})")
        base_cfg = read_json_with_base(base_path, visited=visited)
        merged = _deep_merge(merged, base_cfg)

    child_cfg = dict(cfg)
    child_cfg.pop("base_config", None)
    child_cfg.pop("base_configs", None)
    return _deep_merge(merged, child_cfg)
