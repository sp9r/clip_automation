"""Deterministic Phase 2 planning core.

This module contains chunking, candidate normalization, scoring, decision, and
per-clip planning.  Providers are injected and can be replaced by deterministic
test doubles; no network, FFmpeg, editor API, or frontend is used here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Protocol

from planning_artifacts import (
    ARTIFACT_SCHEMAS,
    PlanningValidationError,
    require_valid_artifact,
)


SCORE_AXES = ("hook", "standalone", "interest", "coherence", "novelty", "confidence")
DEFAULT_SCORE_WEIGHTS = {
    "hook": 0.20,
    "standalone": 0.20,
    "interest": 0.20,
    "coherence": 0.15,
    "novelty": 0.10,
    "confidence": 0.15,
}


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 4000
    max_duration: float = 120.0
    overlap_duration: float = 10.0

    def validate(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars must be positive")
        if not math.isfinite(self.max_duration) or self.max_duration <= 0:
            raise ValueError("max_duration must be positive and finite")
        if not math.isfinite(self.overlap_duration) or self.overlap_duration < 0:
            raise ValueError("overlap_duration must be non-negative and finite")
        if self.overlap_duration >= self.max_duration:
            raise ValueError("overlap_duration must be less than max_duration")


@dataclass(frozen=True)
class DecisionSettings:
    minimum_score: float = 70.0
    max_selected_clips: int = 3
    min_duration: float = 5.0
    max_duration: float = 60.0
    overlap_suppression: float = 0.50
    near_duplicate_suppression: float = 0.80

    def as_dict(self) -> dict[str, Any]:
        return {
            "minimum_score": self.minimum_score,
            "max_selected_clips": self.max_selected_clips,
            "min_duration": self.min_duration,
            "max_duration": self.max_duration,
            "overlap_suppression": self.overlap_suppression,
            "near_duplicate_suppression": self.near_duplicate_suppression,
        }

    def validate(self) -> None:
        if not math.isfinite(self.minimum_score) or not 0 <= self.minimum_score <= 100:
            raise ValueError("minimum_score must be between 0 and 100")
        if self.max_selected_clips < 0:
            raise ValueError("max_selected_clips must be non-negative")
        if not math.isfinite(self.min_duration) or self.min_duration < 0:
            raise ValueError("min_duration must be non-negative")
        if not math.isfinite(self.max_duration) or self.max_duration < self.min_duration:
            raise ValueError("max_duration must be >= min_duration")
        for name, value in (
            ("overlap_suppression", self.overlap_suppression),
            ("near_duplicate_suppression", self.near_duplicate_suppression),
        ):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


class CandidateProvider(Protocol):
    name: str
    version: str

    def generate(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Return untrusted candidate objects using absolute timeline seconds."""


class EvaluationProvider(Protocol):
    name: str
    version: str

    def evaluate(self, candidate: dict[str, Any], context: str) -> dict[str, Any]:
        """Return untrusted component scores for one candidate."""


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{_hash_json(value)[:16]}"


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PlanningValidationError(f"{label} must be finite") from error
    if not math.isfinite(number):
        raise PlanningValidationError(f"{label} must be finite")
    return number


def _transcript_records(transcript: list[dict]) -> tuple[list[dict[str, Any]], float]:
    if not isinstance(transcript, list) or not transcript:
        raise ValueError("transcript must be a non-empty array")
    records: list[dict[str, Any]] = []
    source_index = 0
    for index, segment in enumerate(transcript):
        if not isinstance(segment, dict):
            raise ValueError(f"transcript[{index}] must be an object")
        try:
            segment_start = _finite(segment["start"], f"transcript[{index}].start")
            segment_end = _finite(segment["end"], f"transcript[{index}].end")
        except (KeyError, PlanningValidationError) as error:
            raise ValueError(f"transcript[{index}] requires finite start/end") from error
        if segment_start < 0 or segment_end <= segment_start:
            raise ValueError(f"transcript[{index}] has invalid interval")
        words = segment.get("words")
        if isinstance(words, list) and words:
            for word_index, word in enumerate(words):
                if not isinstance(word, dict):
                    raise ValueError(f"transcript[{index}].words[{word_index}] must be an object")
                start = _finite(word.get("start", word.get("timeline_start")), "word.start")
                end = _finite(word.get("end", word.get("timeline_end")), "word.end")
                if start < 0 or end < start:
                    raise ValueError("transcript word has invalid interval")
                records.append({
                    "start": start,
                    "end": end,
                    "text": str(word.get("word", word.get("text", ""))),
                    "source_index": source_index,
                })
                source_index += 1
        else:
            records.append({
                "start": segment_start,
                "end": segment_end,
                "text": str(segment.get("text", "")),
                "source_index": source_index,
            })
            source_index += 1
    records.sort(key=lambda item: (item["start"], item["source_index"]))
    duration = max(max(item["end"] for item in records), 0.0)
    return records, duration


def _record_intersects_window(record: dict[str, Any], window_start: float, window_end: float) -> bool:
    """Return whether a positive interval or point record belongs to a window."""
    start = float(record["start"])
    end = float(record["end"])
    if end == start:
        # Point records are inclusive at both window edges. Overlap windows may
        # therefore repeat a point, but no boundary point is silently dropped.
        return window_start - 1e-9 <= start <= window_end + 1e-9
    return end > window_start + 1e-9 and start < window_end - 1e-9


def chunk_transcript(transcript: list[dict], config: ChunkingConfig = ChunkingConfig()) -> list[dict[str, Any]]:
    """Split the complete analysis timeline into deterministic overlapping chunks.

    Chunk boundaries are timeline windows, not a prefix sample.  A chunk's text
    is assembled from all transcript records intersecting that window and is
    never silently truncated.  If a single record exceeds max_chars, it is kept
    as an explicit oversized chunk so coverage and provenance are preserved.
    """

    config.validate()
    records, duration = _transcript_records(transcript)
    chunks: list[dict[str, Any]] = []
    start = 0.0
    while start < duration - 1e-9 or not chunks:
        hard_end = min(duration, start + config.max_duration)
        selected: list[dict[str, Any]] = [
            record for record in records if _record_intersects_window(record, start, hard_end)
        ]
        # The chunk interval itself is a coverage window.  Transcript records
        # may cross its right edge; retaining their text while keeping the
        # window bounded avoids gaps and never exceeds max_duration.
        end = hard_end
        text = "".join(record["text"] for record in selected)
        base_chunk = {
            "timeline": {"start": round(start, 6), "end": round(end, 6)},
            "record_count": len(selected),
            "provenance": {"source": "transcript", "record_indexes": [record["source_index"] for record in selected]},
        }
        text_parts = [text[index:index + config.max_chars] for index in range(0, len(text), config.max_chars)] or [""]
        for part_index, text_part in enumerate(text_parts):
            chunk_id = _stable_id("chunk", {"index": len(chunks), "part": part_index, **base_chunk["timeline"]})
            chunks.append({"chunk_id": chunk_id, **base_chunk, "text": text_part})
        if end >= duration - 1e-9:
            break
        next_start = max(start + 1e-6, end - config.overlap_duration)
        if next_start <= start + 1e-9:
            next_start = end
        start = min(next_start, duration)
    return chunks


def _candidate_key(start: float, end: float, reason: str) -> tuple[float, float, str]:
    return round(start, 3), round(end, 3), reason.strip()


def _deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda item: (item["timeline"]["start"], item["timeline"]["end"], item["reason"], item["chunk_id"]))
    result: list[dict[str, Any]] = []
    for candidate in ordered:
        start = candidate["timeline"]["start"]
        end = candidate["timeline"]["end"]
        duplicate = False
        for existing in result:
            existing_start = existing["timeline"]["start"]
            existing_end = existing["timeline"]["end"]
            intersection = max(0.0, min(end, existing_end) - max(start, existing_start))
            shorter = min(end - start, existing_end - existing_start)
            if shorter > 0 and intersection / shorter >= 0.80:
                duplicate = True
                break
        if not duplicate:
            result.append(candidate)
    for candidate in result:
        candidate["candidate_id"] = _stable_id("candidate", _candidate_key(candidate["timeline"]["start"], candidate["timeline"]["end"], candidate["reason"]))
    return result


def generate_candidates(
    transcript: list[dict],
    *,
    input_hashes: dict[str, str],
    provider: CandidateProvider,
    config: ChunkingConfig = ChunkingConfig(),
) -> dict[str, Any]:
    chunks = chunk_transcript(transcript, config)
    _, duration = _transcript_records(transcript)
    diagnostics: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    status = "normal"
    for chunk in chunks:
        try:
            raw_items = provider.generate(dict(chunk))
            if not isinstance(raw_items, list):
                raise ValueError("candidate provider must return an array")
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise ValueError("candidate provider returned a non-object")
                start = _finite(raw["start"], "candidate.start")
                end = _finite(raw["end"], "candidate.end")
                if start < chunk["timeline"]["start"] - 1e-6 or end > chunk["timeline"]["end"] + 1e-6:
                    raise ValueError("candidate is outside its source chunk")
                if start < 0 or end <= start or end > duration + 1e-6:
                    raise ValueError("candidate is outside transcript timeline")
                reason = str(raw.get("reason", "")).strip()
                candidates.append(
                    {
                        "candidate_id": "pending",
                        "timeline": {"start": round(start, 6), "end": round(end, 6)},
                        "duration": round(end - start, 6),
                        "chunk_id": chunk["chunk_id"],
                        "reason": reason,
                        "provenance": {
                            "status": "fallback" if getattr(provider, "status", "normal") == "fallback" else "normal",
                            "provider": getattr(provider, "name", type(provider).__name__),
                            "provider_version": getattr(provider, "version", "unknown"),
                        },
                    }
                )
        except Exception as error:
            status = "failed"
            diagnostics.append({"severity": "error", "code": "candidate_provider_failed", "message": str(error), "chunk_id": chunk["chunk_id"]})
            break
    if getattr(provider, "status", "normal") == "fallback":
        if status != "failed":
            status = "fallback"
    if status == "failed":
        candidates = []
    if not candidates and status == "normal":
        diagnostics.append({"severity": "warning", "code": "no_candidates", "message": "provider returned no candidates"})
    candidates = _deduplicate_candidates(candidates)
    artifact = {
        "schema_version": ARTIFACT_SCHEMAS["candidates"],
        "generator": {"name": "clip_automation.planning.candidate_generation", "version": "1"},
        "input_hashes": dict(sorted(input_hashes.items())),
        "status": status,
        "diagnostics": diagnostics,
        "timeline_duration": round(duration, 6),
        "chunking": {
            "max_chars": config.max_chars,
            "max_duration": config.max_duration,
            "overlap_duration": config.overlap_duration,
        },
        "chunks": chunks,
        "candidates": candidates,
    }
    return require_valid_artifact("candidates", artifact)


def evaluate_candidates(
    candidates_artifact: dict[str, Any],
    *,
    input_hashes: dict[str, str],
    evaluator: EvaluationProvider,
    weights: dict[str, float] | None = None,
    context: str = "",
) -> dict[str, Any]:
    weights = dict(weights or DEFAULT_SCORE_WEIGHTS)
    if set(weights) != set(SCORE_AXES):
        raise ValueError("weights must define exactly the six evaluation axes")
    total_weight = sum(float(value) for value in weights.values())
    if not math.isfinite(total_weight) or total_weight <= 0:
        raise ValueError("weights must have a positive finite total")
    diagnostics: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    status = "normal"
    candidate_ids = [candidate["candidate_id"] for candidate in candidates_artifact["candidates"]]
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates_artifact["candidates"]}
    for candidate in candidates_artifact["candidates"]:
        try:
            raw = evaluator.evaluate(dict(candidate), context)
            if not isinstance(raw, dict):
                raise ValueError("evaluation provider must return an object")
            candidate_id = raw["candidate_id"]
            if candidate_id != candidate["candidate_id"] or candidate_id not in by_id:
                raise ValueError("evaluation candidate_id does not match input candidate")
            scores = raw.get("scores")
            if not isinstance(scores, dict):
                raise ValueError("evaluation scores must be an object")
            normalized_scores = {axis: _finite(scores[axis], f"scores.{axis}") for axis in SCORE_AXES}
            if any(value < 0 or value > 100 for value in normalized_scores.values()):
                raise ValueError("evaluation score must be between 0 and 100")
            total = sum(normalized_scores[axis] * float(weights[axis]) for axis in SCORE_AXES) / total_weight
            evaluations.append(
                {
                    "candidate_id": candidate_id,
                    "scores": {axis: round(normalized_scores[axis], 6) for axis in SCORE_AXES},
                    "total_score": round(total, 6),
                    "reason": str(raw.get("reason", "")).strip(),
                    "provider": {"name": getattr(evaluator, "name", type(evaluator).__name__), "version": getattr(evaluator, "version", "unknown")},
                }
            )
        except Exception as error:
            status = "failed"
            diagnostics.append({"severity": "error", "code": "evaluation_failed", "message": str(error), "candidate_id": candidate["candidate_id"]})
            break
    if getattr(evaluator, "status", "normal") == "fallback":
        if status != "failed":
            status = "fallback"
    if status == "failed":
        evaluations = []
    artifact = {
        "schema_version": ARTIFACT_SCHEMAS["evaluations"],
        "generator": {"name": "clip_automation.planning.candidate_evaluation", "version": "1"},
        "input_hashes": dict(sorted(input_hashes.items())),
        "status": status,
        "diagnostics": diagnostics,
        "candidate_ids": candidate_ids,
        "weights": {key: float(weights[key]) for key in SCORE_AXES},
        "evaluations": evaluations,
    }
    return require_valid_artifact("evaluations", artifact)


def _overlap_ratio(first: dict, second: dict) -> tuple[float, float]:
    first_start, first_end = first["timeline"]["start"], first["timeline"]["end"]
    second_start, second_end = second["timeline"]["start"], second["timeline"]["end"]
    intersection = max(0.0, min(first_end, second_end) - max(first_start, second_start))
    if intersection <= 0:
        return 0.0, 0.0
    union = max(first_end, second_end) - min(first_start, second_start)
    shorter = min(first_end - first_start, second_end - second_start)
    return intersection / union, intersection / shorter


def decide_clips(
    candidates_artifact: dict[str, Any],
    evaluations_artifact: dict[str, Any],
    *,
    input_hashes: dict[str, str],
    settings: DecisionSettings = DecisionSettings(),
) -> dict[str, Any]:
    settings.validate()
    candidates = {item["candidate_id"]: item for item in candidates_artifact["candidates"]}
    evaluations = {item["candidate_id"]: item for item in evaluations_artifact["evaluations"]}
    rejected: dict[str, str] = {}
    eligible: list[dict[str, Any]] = []
    for candidate_id in sorted(candidates):
        candidate = candidates[candidate_id]
        evaluation = evaluations.get(candidate_id)
        if evaluation is None:
            rejected[candidate_id] = "missing_evaluation"
            continue
        duration = candidate["duration"]
        if evaluation["total_score"] < settings.minimum_score:
            rejected[candidate_id] = "below_minimum_score"
        elif duration < settings.min_duration:
            rejected[candidate_id] = "below_min_duration"
        elif duration > settings.max_duration:
            rejected[candidate_id] = "above_max_duration"
        else:
            eligible.append({**candidate, "total_score": evaluation["total_score"]})
    eligible.sort(key=lambda item: (-item["total_score"], item["timeline"]["start"], item["timeline"]["end"], item["candidate_id"]))
    selected: list[dict[str, Any]] = []
    for candidate in eligible:
        blocked = False
        for other in selected:
            iou, shorter_overlap = _overlap_ratio(candidate, other)
            if shorter_overlap >= settings.near_duplicate_suppression:
                rejected[candidate["candidate_id"]] = "near_duplicate_suppressed"
                blocked = True
                break
            if iou >= settings.overlap_suppression:
                rejected[candidate["candidate_id"]] = "overlap_suppressed"
                blocked = True
                break
        if blocked:
            continue
        if len(selected) >= settings.max_selected_clips:
            rejected[candidate["candidate_id"]] = "max_selected_clips"
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: (item["timeline"]["start"], item["timeline"]["end"], item["candidate_id"]))
    selected_ids = [item["candidate_id"] for item in selected]
    for candidate_id in candidates:
        if candidate_id not in selected_ids and candidate_id not in rejected:
            rejected[candidate_id] = "not_selected"
    if not candidates:
        decision_status = "no_candidates"
    elif not selected:
        decision_status = "no_candidates_above_threshold" if all(value in {"below_minimum_score", "missing_evaluation"} for value in rejected.values()) else "no_candidates_after_constraints"
    else:
        decision_status = "selected"
    upstream_statuses = {candidates_artifact.get("status"), evaluations_artifact.get("status")}
    decision_artifact_status = "fallback" if "fallback" in upstream_statuses else ("degraded" if "degraded" in upstream_statuses else "normal")
    artifact = {
        "schema_version": ARTIFACT_SCHEMAS["decision"],
        "generator": {"name": "clip_automation.planning.clip_decision", "version": "1"},
        "input_hashes": dict(sorted(input_hashes.items())),
        "status": decision_artifact_status,
        "diagnostics": [],
        "decision_status": decision_status,
        "selected_candidate_ids": selected_ids,
        "rejected_candidate_ids": sorted(rejected),
        "rejection_reasons": dict(sorted(rejected.items())),
        "ordering": selected_ids,
        "settings": settings.as_dict(),
        "decision_version": "1",
    }
    return require_valid_artifact("decision", artifact)


def plan_clips(
    candidates_artifact: dict[str, Any],
    decision_artifact: dict[str, Any],
    *,
    input_hashes: dict[str, str],
    target_output: dict[str, Any],
    caption_policy: dict[str, Any] | None = None,
    output_preset: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates = {item["candidate_id"]: item for item in candidates_artifact["candidates"]}
    duration = float(candidates_artifact["timeline_duration"])
    plans: list[dict[str, Any]] = []
    plan_status = decision_artifact.get("status", "normal")
    for index, candidate_id in enumerate(decision_artifact["ordering"], 1):
        candidate = candidates[candidate_id]
        start = float(candidate["timeline"]["start"])
        end = float(candidate["timeline"]["end"])
        regions: list[dict[str, Any]] = []
        if start > 0:
            regions.append({"id": "delete_head", "start": 0.0, "end": round(start, 6), "mode": "delete"})
        regions.append({"id": "keep_candidate", "start": round(start, 6), "end": round(end, 6), "mode": "keep"})
        if end < duration:
            regions.append({"id": "delete_tail", "start": round(end, 6), "end": round(duration, 6), "mode": "delete"})
        plan = {
            "schema_version": ARTIFACT_SCHEMAS["plan"],
            "generator": {"name": "clip_automation.planning.clip_planning", "version": "1"},
            "input_hashes": dict(sorted(input_hashes.items())),
            "status": plan_status,
            "diagnostics": [],
            "clip_id": f"clip_{index:06d}",
            "candidate_id": candidate_id,
            "candidate_timeline": {"start": start, "end": end},
            "timeline_duration": duration,
            "regions": regions,
            "caption_policy": dict(caption_policy or {"mode": "preserve_word_boundaries", "unsafe_split": "unsupported"}),
            "target_output": dict(target_output),
            "output_preset": dict(output_preset or {}),
            "source": {"candidate_id": candidate_id, "chunk_id": candidate["chunk_id"]},
        }
        plans.append(require_valid_artifact("plan", plan))
    return plans
