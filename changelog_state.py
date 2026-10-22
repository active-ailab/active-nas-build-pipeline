"""Changelog build state shared by the trigger and document processes.

Each project/version has its own file. Never merge runtime fields belonging to
different builds, even when both projects use the same Jenkins job.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, Optional


RUNTIME_KEYS = (
    "project", "version", "job_url", "build_number", "build_url", "queue_url",
    "status", "result", "triggered_at", "updated_at", "finished_at", "link",
    "parameters",
)


def belongs_to(state: Dict[str, Any], project: str, version: str) -> bool:
    return (
        bool(project) and bool(version)
        and str(state.get("project") or "").strip() == project
        and str(state.get("version") or "").strip() == version
    )


def state_path(project: str, version: str, output_dir: Optional[Path] = None) -> Path:
    root = output_dir if output_dir is not None else Path(__file__).with_name("output")
    # Hash the original identity as well, so sanitized names cannot collide.
    digest = hashlib.sha256(json.dumps([project, version]).encode("utf-8")).hexdigest()[:16]
    label = re.sub(r"[^a-zA-Z0-9_.-]", "_", f"{project}_{version}")[:100]
    return root / "changelog" / f"{label}_{digest}.json"


def _read(path: Path) -> Dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def load_state(project: str, version: str, output_dir: Optional[Path] = None) -> Dict[str, Any]:
    path = state_path(project, version, output_dir)
    if path.exists():
        state = _read(path)
    else:
        # Read old installations only when the complete owner matches. Never
        # overwrite the legacy file: another running project may still use it.
        state = _read(path.parent.parent / "changelog_job.json")
    return state if belongs_to(state, project, version) else {}


def save_state(state: Dict[str, Any], output_dir: Optional[Path] = None) -> None:
    project = str(state.get("project") or "").strip()
    version = str(state.get("version") or "").strip()
    if not project or not version:
        raise ValueError("Changelog state requires project and version")
    path = state_path(project, version, output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readers must see a complete JSON file while another process writes.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.stem + "_", suffix=".tmp", delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        except BaseException:
            f.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def select_state(project: str, version: str, config_state: Dict[str, Any],
                 output_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Select a whole build record; config and disk must never form a hybrid."""
    saved = load_state(project, version, output_dir)
    if not belongs_to(config_state, project, version) or not config_state.get("build_number"):
        return saved
    if saved.get("build_number") == config_state.get("build_number"):
        return saved  # Disk carries subsequent polling results and the link.
    if saved and str(saved.get("updated_at") or "") > str(config_state.get("updated_at") or ""):
        return saved
    return {k: config_state[k] for k in RUNTIME_KEYS if k in config_state}
