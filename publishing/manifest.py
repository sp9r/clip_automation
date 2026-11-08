"""Durable, atomic publish_manifest.v1 storage."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .errors import PublishPreparationError, redact_text
from .models import validate_metadata


SCHEMA_VERSION = "publish_manifest.v1"
CLIP_STATES = {
    "prepared", "uploading", "uploaded", "thumbnail_setting", "processing",
    "completed", "uploaded_with_thumbnail_error", "processing_pending", "failed", "canceled",
    "needs_republish_confirmation", "orphaned",
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def portable_relative(path: str | Path, base: str | Path) -> str:
    return Path(os.path.relpath(Path(path).resolve(), Path(base).resolve())).as_posix()


def resolve_relative(value: str, base: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute() or "\\" in value:
        raise ValueError("publish manifest paths must be portable relative paths with '/' separators")
    return (Path(base).resolve() / path).resolve()


def atomic_write_manifest(path: str | Path, manifest: dict[str, Any]) -> Path:
    validate_manifest(manifest)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
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
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PublishPreparationError(f"publish manifest could not be read: {redact_text(error)}") from error
    validate_manifest(data)
    return data


def _summary(clips: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(clips),
        "prepared": sum(item.get("state") == "prepared" for item in clips),
        "uploaded": sum(item.get("state") in {"uploaded", "processing", "processing_pending"} for item in clips),
        "completed": sum(item.get("state") in {"uploaded", "processing", "processing_pending", "completed", "uploaded_with_thumbnail_error"} for item in clips),
        "failed": sum(item.get("state") == "failed" for item in clips),
    }


def update_clip(manifest: dict[str, Any], clip_id: str, **updates: Any) -> dict[str, Any]:
    for item in manifest.get("clips", []):
        if item.get("clip_id") == clip_id:
            item.update(updates)
            manifest["summary"] = _summary(manifest["clips"])
            return manifest
    raise ValueError(f"publish manifest clip not found: {clip_id}")


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported publish manifest schema")
    if manifest.get("platform") != "youtube":
        raise ValueError("Phase 7 currently supports only platform=youtube")
    source = manifest.get("source_render_manifest")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not isinstance(source.get("sha256"), str):
        raise ValueError("source_render_manifest is invalid")
    clips = manifest.get("clips")
    if not isinstance(clips, list):
        raise ValueError("publish manifest clips must be an array")
    seen: set[str] = set()
    for item in clips:
        if not isinstance(item, dict) or not isinstance(item.get("clip_id"), str) or not item["clip_id"]:
            raise ValueError("publish clip_id is invalid")
        if item["clip_id"] in seen:
            raise ValueError(f"duplicate publish clip: {item['clip_id']}")
        seen.add(item["clip_id"])
        if item.get("state") not in CLIP_STATES:
            raise ValueError(f"invalid publish state: {item.get('clip_id')}")
        video = item.get("video")
        if not isinstance(video, dict) or not isinstance(video.get("path"), str) or not isinstance(video.get("sha256"), str):
            raise ValueError(f"video entry is invalid: {item['clip_id']}")
        validate_metadata(item.get("metadata", {}))
        thumbnail = item.get("thumbnail")
        if not isinstance(thumbnail, dict) or thumbnail.get("path") is not None and not isinstance(thumbnail.get("path"), str):
            raise ValueError(f"thumbnail entry is invalid: {item['clip_id']}")
        remote = item.get("remote")
        if not isinstance(remote, dict):
            raise ValueError(f"remote entry is invalid: {item['clip_id']}")
        previous_remote = item.get("previous_remote")
        if previous_remote is not None and not isinstance(previous_remote, dict):
            raise ValueError(f"previous_remote entry is invalid: {item['clip_id']}")
        diagnostics = item.get("diagnostics")
        if diagnostics is not None and not isinstance(diagnostics, dict):
            raise ValueError(f"diagnostics entry is invalid: {item['clip_id']}")
        error = item.get("error")
        if error is not None and not isinstance(error, dict):
            raise ValueError(f"error entry is invalid: {item['clip_id']}")
    if manifest.get("summary") != _summary(clips):
        raise ValueError("publish manifest summary is inconsistent")
