"""Atomic render_manifest.v1 IO for Phase 5 execution artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA_VERSION = "render_manifest.v1"
CLIP_STATES = {"pending", "running", "completed", "failed", "skipped"}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portable_relative(path: str | Path, base: str | Path) -> str:
    relative = os.path.relpath(Path(path).resolve(), Path(base).resolve())
    return Path(relative).as_posix()


def new_render_manifest(
    *,
    planning_dir: str | Path,
    planning_manifest_path: str | Path,
    renderer: dict[str, Any],
) -> dict[str, Any]:
    planning_root = Path(planning_dir).resolve()
    planning_path = Path(planning_manifest_path).resolve()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "planning_manifest": {
            "path": portable_relative(planning_path, planning_root),
            "sha256": sha256_file(planning_path),
        },
        "renderer": dict(renderer),
        "clips": [],
        "summary": {"total": 0, "completed": 0, "failed": 0, "skipped": 0},
    }


def upsert_clip(manifest: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    clip_id = entry.get("clip_id")
    if not isinstance(clip_id, str) or not clip_id:
        raise ValueError("render manifest clip_id must be a non-empty string")
    state = entry.get("state")
    if state not in CLIP_STATES:
        raise ValueError(f"unsupported render clip state: {state}")
    clips = [item for item in manifest.setdefault("clips", []) if item.get("clip_id") != clip_id]
    clips.append(dict(entry))
    clips.sort(key=lambda item: item["clip_id"])
    manifest["clips"] = clips
    _update_summary(manifest)
    return manifest


def _update_summary(manifest: dict[str, Any]) -> None:
    clips = manifest.get("clips", [])
    manifest["summary"] = {
        "total": len(clips),
        "completed": sum(item.get("state") == "completed" for item in clips),
        "failed": sum(item.get("state") == "failed" for item in clips),
        "skipped": sum(item.get("state") == "skipped" for item in clips),
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported render manifest")
    planning = manifest.get("planning_manifest")
    if not isinstance(planning, dict) or not isinstance(planning.get("path"), str) or not isinstance(planning.get("sha256"), str):
        raise ValueError("render manifest planning_manifest is invalid")
    if not isinstance(manifest.get("renderer"), dict):
        raise ValueError("render manifest renderer is invalid")
    clips = manifest.get("clips")
    if not isinstance(clips, list):
        raise ValueError("render manifest clips must be an array")
    seen: set[str] = set()
    for item in clips:
        if not isinstance(item, dict) or not isinstance(item.get("clip_id"), str) or not item["clip_id"]:
            raise ValueError("render manifest clip entry is invalid")
        if item["clip_id"] in seen:
            raise ValueError(f"duplicate render manifest clip: {item['clip_id']}")
        seen.add(item["clip_id"])
        if item.get("state") not in CLIP_STATES:
            raise ValueError(f"invalid render manifest clip state: {item['clip_id']}")
        edit_project = item.get("edit_project")
        if not isinstance(edit_project, dict) or not isinstance(edit_project.get("path"), str):
            raise ValueError(f"render manifest edit_project is invalid: {item['clip_id']}")
        if edit_project.get("sha256") is not None and not isinstance(edit_project["sha256"], str):
            raise ValueError(f"render manifest edit_project hash is invalid: {item['clip_id']}")
        output = item.get("output")
        if not isinstance(output, dict) or not isinstance(output.get("path"), str):
            raise ValueError(f"render manifest output is invalid: {item['clip_id']}")
        if output.get("sha256") is not None and not isinstance(output["sha256"], str):
            raise ValueError(f"render manifest output hash is invalid: {item['clip_id']}")
    expected = {
        "total": len(clips),
        "completed": sum(item.get("state") == "completed" for item in clips),
        "failed": sum(item.get("state") == "failed" for item in clips),
        "skipped": sum(item.get("state") == "skipped" for item in clips),
    }
    if manifest.get("summary") != expected:
        raise ValueError("render manifest summary is inconsistent")


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    validate_manifest(manifest)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_manifest(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    validate_manifest(manifest)
    return manifest
