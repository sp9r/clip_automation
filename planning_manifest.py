"""Deterministic manifest for the standalone Phase 2 planning path."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from planning_artifacts import artifact_hash, sha256_file


MANIFEST_SCHEMA_VERSION = "planning_manifest.v1"
STAGES = (
    "candidate_generation",
    "candidate_evaluation",
    "clip_decision",
    "clip_planning",
    "contract_generation",
)
STAGE_STATES = {"pending", "running", "completed", "degraded", "fallback", "failed"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def new_manifest(*, input_fingerprint: str, provider_versions: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "provider_versions": dict(sorted((provider_versions or {}).items())),
        "stages": {
            stage: {
                "state": "pending",
                "schema_version": None,
                "artifact_path": None,
                "artifact_sha256": None,
                "artifacts": [],
                "input_fingerprint": input_fingerprint,
                "provider": None,
                "status": "pending",
                "error": None,
            }
            for stage in STAGES
        },
    }


def record_stage(
    manifest: dict[str, Any],
    stage: str,
    *,
    state: str,
    artifact_path: str | os.PathLike | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    schema_version: str | None = None,
    input_fingerprint: str | None = None,
    provider: dict[str, str] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"unknown planning stage: {stage}")
    if state not in STAGE_STATES:
        raise ValueError(f"unsupported planning stage state: {state}")
    item = manifest["stages"][stage]
    if artifacts is not None and artifact_path is not None:
        raise ValueError("artifact_path and artifacts are mutually exclusive")
    if artifacts is None:
        artifact_entries = []
        if artifact_path is not None:
            artifact_entries.append(
                {
                    "clip_id": None,
                    "path": str(artifact_path),
                    "schema_version": schema_version,
                    "state": state,
                }
            )
    else:
        artifact_entries = [dict(entry) for entry in artifacts]
    normalized_artifacts: list[dict[str, Any]] = []
    for entry in artifact_entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("manifest artifact path must be a non-empty string")
        entry_schema = entry.get("schema_version", schema_version)
        entry_state = entry.get("state", state)
        if entry_state not in STAGE_STATES:
            raise ValueError(f"unsupported manifest artifact state: {entry_state}")
        digest = entry.get("sha256")
        if digest is None and Path(path).is_file():
            digest = sha256_file(path)
        normalized_artifacts.append(
            {
                "clip_id": entry.get("clip_id"),
                "path": path,
                "sha256": digest,
                "schema_version": entry_schema,
                "state": entry_state,
            }
        )
    first = normalized_artifacts[0] if normalized_artifacts else None
    item.update(
        {
            "state": state,
            "status": state,
            "schema_version": schema_version,
            "artifact_path": first["path"] if first else None,
            "artifact_sha256": first["sha256"] if first else None,
            "artifacts": normalized_artifacts,
            "input_fingerprint": input_fingerprint or manifest["input_fingerprint"],
            "provider": dict(provider) if provider else None,
            "error": error,
        }
    )
    return manifest


def write_manifest(path: str | os.PathLike, manifest: dict[str, Any]) -> Path:
    require_valid_manifest(manifest)
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


def load_manifest(path: str | os.PathLike) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported planning manifest")
    for stage in STAGES:
        if stage not in manifest.get("stages", {}):
            raise ValueError(f"planning manifest is missing stage: {stage}")
        item = manifest["stages"][stage]
        # v1 manifests written before multi-artifact support are upgraded in memory.
        if "artifacts" not in item:
            item["artifacts"] = []
            if item.get("artifact_path"):
                item["artifacts"].append(
                    {
                        "clip_id": None,
                        "path": item["artifact_path"],
                        "sha256": item.get("artifact_sha256"),
                        "schema_version": item.get("schema_version"),
                        "state": item.get("state", item.get("status", "completed")),
                    }
                )
    require_valid_manifest(manifest)
    return manifest


def require_valid_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported planning manifest")
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("planning manifest stages must be an object")
    for stage in STAGES:
        if stage not in stages:
            raise ValueError(f"planning manifest is missing stage: {stage}")
        item = stages[stage]
        if not isinstance(item, dict):
            raise ValueError(f"planning manifest stage must be an object: {stage}")
        state = item.get("state", item.get("status"))
        if state not in STAGE_STATES:
            raise ValueError(f"unsupported planning stage state: {stage}")
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError(f"planning manifest artifacts must be an array: {stage}")
        for entry in artifacts:
            if not isinstance(entry, dict):
                raise ValueError(f"planning manifest artifact must be an object: {stage}")
            if not isinstance(entry.get("path"), str) or not entry["path"]:
                raise ValueError(f"planning manifest artifact path is invalid: {stage}")
            if entry.get("clip_id") is not None and (not isinstance(entry["clip_id"], str) or not entry["clip_id"]):
                raise ValueError(f"planning manifest artifact clip_id is invalid: {stage}")
            if entry.get("schema_version") is not None and not isinstance(entry["schema_version"], str):
                raise ValueError(f"planning manifest artifact schema_version is invalid: {stage}")
            if entry.get("state") not in STAGE_STATES:
                raise ValueError(f"unsupported planning manifest artifact state: {stage}")
            digest = entry.get("sha256")
            if digest is not None and (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)):
                raise ValueError(f"planning manifest artifact sha256 is invalid: {stage}")
        first = artifacts[0] if artifacts else None
        if item.get("artifact_path") != (first["path"] if first else None):
            raise ValueError(f"planning manifest legacy artifact_path is inconsistent: {stage}")
        if item.get("artifact_sha256") != (first["sha256"] if first else None):
            raise ValueError(f"planning manifest legacy artifact_sha256 is inconsistent: {stage}")


def planning_input_fingerprint(input_hashes: dict[str, str], settings: dict[str, Any]) -> str:
    return artifact_hash({"input_hashes": dict(sorted(input_hashes.items())), "settings": settings})
