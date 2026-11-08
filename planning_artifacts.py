"""Phase 2 planning artifacts: schemas, validation, and deterministic IO.

The planning path is deliberately independent from FFmpeg, the legacy editor,
and the job manager implementation.  It accepts untrusted transcript/provider
data and publishes only validated, versioned artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable


ARTIFACT_SCHEMAS = {
    "candidates": "highlight_candidates.v1",
    "evaluations": "highlight_evaluations.v1",
    "decision": "clip_decision.v1",
    "plan": "clip_plan.v1",
}
PLANNING_ARTIFACT_SCHEMA_VERSION = "planning_artifacts.v1"
DIAGNOSTIC_SEVERITIES = {"warning", "unsupported", "error"}
PROVENANCE_STATUSES = {"normal", "degraded", "fallback", "failed"}
TIME_EPSILON = 1e-6


class PlanningValidationError(ValueError):
    def __init__(self, message: str, issues: Iterable[str] | None = None):
        self.issues = tuple(issues or (message,))
        super().__init__(message)


def _finite(value: Any, label: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PlanningValidationError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise PlanningValidationError(f"{label} must be a finite number")
    if minimum is not None and number < minimum - TIME_EPSILON:
        raise PlanningValidationError(f"{label} must be >= {minimum}")
    return number


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanningValidationError(f"{label} must be a non-empty string")
    return value


def _diagnostics(value: Any, label: str, issues: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        issues.append(f"{label} must be an array")
        return
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            issues.append(f"{label}[{index}] must be an object")
            continue
        if item.get("severity") not in DIAGNOSTIC_SEVERITIES:
            issues.append(f"{label}[{index}].severity is unsupported")
        for field in ("code", "message"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                issues.append(f"{label}[{index}].{field} must be a non-empty string")


def _common_issues(artifact: dict, schema_version: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(artifact, dict):
        return ["artifact must be an object"]
    if artifact.get("schema_version") != schema_version:
        issues.append(f"schema_version must be {schema_version}")
    generator = artifact.get("generator")
    if not isinstance(generator, dict):
        issues.append("generator must be an object")
    else:
        for field in ("name", "version"):
            if not isinstance(generator.get(field), str) or not generator[field].strip():
                issues.append(f"generator.{field} must be a non-empty string")
    input_hashes = artifact.get("input_hashes")
    if not isinstance(input_hashes, dict) or not input_hashes:
        issues.append("input_hashes must be a non-empty object")
    elif any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
        for value in input_hashes.values()
    ):
        issues.append("input_hashes values must be SHA-256 hex strings")
    _diagnostics(artifact.get("diagnostics"), "diagnostics", issues)
    status = artifact.get("status", "normal")
    if status not in PROVENANCE_STATUSES:
        issues.append("status must be normal, degraded, fallback, or failed")
    return issues


def _interval(item: Any, label: str, *, max_duration: float | None = None) -> list[str]:
    issues: list[str] = []
    if not isinstance(item, dict):
        return [f"{label} must be an object"]
    try:
        start = _finite(item["start"], f"{label}.start", minimum=0.0)
        end = _finite(item["end"], f"{label}.end", minimum=0.0)
    except (KeyError, PlanningValidationError) as error:
        return [str(error) if isinstance(error, PlanningValidationError) else f"{label} requires start/end"]
    if end <= start + TIME_EPSILON:
        issues.append(f"{label} must have end > start")
    if max_duration is not None and end > max_duration + TIME_EPSILON:
        issues.append(f"{label} is outside transcript timeline")
    return issues


def validate_candidates(artifact: dict) -> list[str]:
    issues = _common_issues(artifact, ARTIFACT_SCHEMAS["candidates"])
    if not isinstance(artifact, dict):
        return issues
    try:
        duration = _finite(artifact.get("timeline_duration"), "timeline_duration", minimum=0.0)
    except PlanningValidationError as error:
        issues.append(str(error))
        duration = None
    candidates = artifact.get("candidates")
    if not isinstance(candidates, list):
        issues.append("candidates must be an array")
        return issues
    seen: set[str] = set()
    for index, candidate in enumerate(candidates):
        label = f"candidates[{index}]"
        if not isinstance(candidate, dict):
            issues.append(f"{label} must be an object")
            continue
        try:
            candidate_id = _nonempty_string(candidate.get("candidate_id"), f"{label}.candidate_id")
            if candidate_id in seen:
                issues.append(f"duplicate candidate_id: {candidate_id}")
            seen.add(candidate_id)
        except PlanningValidationError as error:
            issues.append(str(error))
        issues.extend(_interval(candidate.get("timeline"), f"{label}.timeline", max_duration=duration))
        try:
            timeline = candidate.get("timeline", {})
            start = float(timeline["start"])
            end = float(timeline["end"])
            if not math.isclose(float(candidate.get("duration")), end - start, abs_tol=TIME_EPSILON):
                issues.append(f"{label}.duration does not match timeline")
        except (KeyError, TypeError, ValueError):
            issues.append(f"{label}.duration must match timeline")
        for field in ("reason", "chunk_id"):
            if not isinstance(candidate.get(field), str):
                issues.append(f"{label}.{field} must be a string")
        provenance = candidate.get("provenance")
        if not isinstance(provenance, dict):
            issues.append(f"{label}.provenance must be an object")
    return issues


def validate_evaluations(artifact: dict) -> list[str]:
    issues = _common_issues(artifact, ARTIFACT_SCHEMAS["evaluations"])
    if not isinstance(artifact, dict):
        return issues
    weights = artifact.get("weights")
    if not isinstance(weights, dict) or not weights:
        issues.append("weights must be a non-empty object")
    else:
        total_weight = 0.0
        for key, value in weights.items():
            try:
                total_weight += _finite(value, f"weights.{key}", minimum=0.0)
            except PlanningValidationError as error:
                issues.append(str(error))
        if total_weight <= TIME_EPSILON:
            issues.append("weights must have a positive total")
    evaluations = artifact.get("evaluations")
    if not isinstance(evaluations, list):
        issues.append("evaluations must be an array")
        return issues
    seen: set[str] = set()
    allowed_scores = ("hook", "standalone", "interest", "coherence", "novelty", "confidence")
    for index, evaluation in enumerate(evaluations):
        label = f"evaluations[{index}]"
        if not isinstance(evaluation, dict):
            issues.append(f"{label} must be an object")
            continue
        candidate_id = evaluation.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            issues.append(f"{label}.candidate_id must be a non-empty string")
        elif candidate_id in seen:
            issues.append(f"duplicate evaluation candidate_id: {candidate_id}")
        else:
            seen.add(candidate_id)
        scores = evaluation.get("scores")
        if not isinstance(scores, dict):
            issues.append(f"{label}.scores must be an object")
            scores = {}
        for axis in allowed_scores:
            try:
                value = _finite(scores[axis], f"{label}.scores.{axis}")
                if not 0.0 <= value <= 100.0:
                    issues.append(f"{label}.scores.{axis} must be between 0 and 100")
            except (KeyError, PlanningValidationError) as error:
                issues.append(str(error) if isinstance(error, PlanningValidationError) else f"{label}.scores.{axis} is required")
        try:
            total = _finite(evaluation["total_score"], f"{label}.total_score")
            if not 0.0 <= total <= 100.0:
                issues.append(f"{label}.total_score must be between 0 and 100")
        except (KeyError, PlanningValidationError) as error:
            issues.append(str(error) if isinstance(error, PlanningValidationError) else f"{label}.total_score is required")
        if not isinstance(evaluation.get("reason"), str):
            issues.append(f"{label}.reason must be a string")
    candidate_ids = artifact.get("candidate_ids")
    if not isinstance(candidate_ids, list) or any(not isinstance(item, str) for item in candidate_ids):
        issues.append("candidate_ids must be an array of strings")
    return issues


def validate_decision(artifact: dict) -> list[str]:
    issues = _common_issues(artifact, ARTIFACT_SCHEMAS["decision"])
    if not isinstance(artifact, dict):
        return issues
    settings = artifact.get("settings")
    if not isinstance(settings, dict):
        issues.append("settings must be an object")
    else:
        for field in ("minimum_score", "min_duration", "max_duration"):
            try:
                _finite(settings[field], f"settings.{field}", minimum=0.0)
            except (KeyError, PlanningValidationError) as error:
                issues.append(str(error) if isinstance(error, PlanningValidationError) else f"settings.{field} is required")
        try:
            if float(settings["max_duration"]) < float(settings["min_duration"]):
                issues.append("settings.max_duration must be >= min_duration")
        except (KeyError, TypeError, ValueError):
            pass
        try:
            if int(settings["max_selected_clips"]) < 0:
                issues.append("settings.max_selected_clips must be >= 0")
        except (KeyError, TypeError, ValueError):
            issues.append("settings.max_selected_clips must be an integer")
    selected = artifact.get("selected_candidate_ids")
    rejected = artifact.get("rejected_candidate_ids")
    for name, value in (("selected_candidate_ids", selected), ("rejected_candidate_ids", rejected)):
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            issues.append(f"{name} must be an array of strings")
    if isinstance(selected, list) and isinstance(rejected, list):
        if set(selected) & set(rejected):
            issues.append("selected and rejected candidates overlap")
        if len(selected) != len(set(selected)) or len(rejected) != len(set(rejected)):
            issues.append("decision candidate IDs must be unique")
    reasons = artifact.get("rejection_reasons")
    if not isinstance(reasons, dict):
        issues.append("rejection_reasons must be an object")
    return issues


def validate_plan(artifact: dict) -> list[str]:
    issues = _common_issues(artifact, ARTIFACT_SCHEMAS["plan"])
    if not isinstance(artifact, dict):
        return issues
    for field in ("clip_id", "candidate_id"):
        if not isinstance(artifact.get(field), str) or not artifact[field].strip():
            issues.append(f"{field} must be a non-empty string")
    issues.extend(_interval(artifact.get("candidate_timeline"), "candidate_timeline"))
    try:
        timeline_duration = _finite(artifact["timeline_duration"], "timeline_duration", minimum=0.0)
    except (KeyError, PlanningValidationError) as error:
        issues.append(str(error) if isinstance(error, PlanningValidationError) else "timeline_duration is required")
        timeline_duration = None
    ranges = artifact.get("regions")
    if not isinstance(ranges, list) or not ranges:
        issues.append("regions must be a non-empty array")
    else:
        previous_end = 0.0
        seen_ids: set[str] = set()
        for index, region in enumerate(ranges):
            label = f"regions[{index}]"
            if not isinstance(region, dict):
                issues.append(f"{label} must be an object")
                continue
            region_id = region.get("id")
            if not isinstance(region_id, str) or not region_id.strip() or region_id in seen_ids:
                issues.append(f"{label}.id must be unique non-empty string")
            seen_ids.add(region_id)
            if region.get("mode") not in {"keep", "delete"}:
                issues.append(f"{label}.mode must be keep or delete")
            issues.extend(_interval(region, label))
            try:
                start = float(region["start"])
                end = float(region["end"])
                if start < previous_end - TIME_EPSILON:
                    issues.append(f"{label} overlaps or reverses preceding region")
                if start > previous_end + TIME_EPSILON:
                    issues.append(f"{label} leaves an uncovered timeline gap")
                previous_end = max(previous_end, end)
            except (KeyError, TypeError, ValueError):
                pass
        if timeline_duration is not None and abs(previous_end - timeline_duration) > TIME_EPSILON:
            issues.append("regions must cover the complete analysis timeline")
    policy = artifact.get("caption_policy")
    if not isinstance(policy, dict):
        issues.append("caption_policy must be an object")
    target = artifact.get("target_output")
    if not isinstance(target, dict):
        issues.append("target_output must be an object")
    return issues


def validate_artifact(kind: str, artifact: dict) -> list[str]:
    validators = {
        "candidates": validate_candidates,
        "evaluations": validate_evaluations,
        "decision": validate_decision,
        "plan": validate_plan,
    }
    if kind not in validators:
        raise ValueError(f"unknown planning artifact kind: {kind}")
    return validators[kind](artifact)


def require_valid_artifact(kind: str, artifact: dict) -> dict:
    issues = validate_artifact(kind, artifact)
    if issues:
        raise PlanningValidationError(f"invalid {kind} artifact", issues)
    return artifact


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_hash(artifact: dict) -> str:
    encoded = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_artifact(path: str | os.PathLike, kind: str, artifact: dict) -> Path:
    require_valid_artifact(kind, artifact)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(artifact, file, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_artifact(path: str | os.PathLike, kind: str) -> dict:
    with Path(path).open("r", encoding="utf-8") as file:
        artifact = json.load(file)
    return require_valid_artifact(kind, artifact)
