"""Offline-safe orchestration for candidate-to-contract planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from edit_project_adapter import EditProjectBuildError, build_edit_project_from_artifacts
from edit_project_io import write_edit_project
from job_control import raise_if_cancelled
from planning_artifacts import artifact_hash, sha256_file, write_artifact
from planning_core import (
    CandidateProvider,
    ChunkingConfig,
    DecisionSettings,
    EvaluationProvider,
    decide_clips,
    evaluate_candidates,
    generate_candidates,
    plan_clips,
)
from planning_manifest import (
    planning_input_fingerprint,
    new_manifest,
    record_stage,
    write_manifest,
)
from planning_providers import OllamaCandidateProvider, OllamaEvaluationProvider


class PlanningRunError(RuntimeError):
    def __init__(self, message: str, manifest: dict[str, Any]):
        self.manifest = manifest
        super().__init__(message)


@dataclass(frozen=True)
class PlanningRunResult:
    planning_dir: Path
    manifest: dict[str, Any]
    candidates: dict[str, Any]
    evaluations: dict[str, Any]
    decision: dict[str, Any]
    plans: tuple[dict[str, Any], ...]
    edit_projects: tuple[dict[str, Any], ...]


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _required_inputs(run_dir: Path) -> dict[str, Path]:
    return {
        "clean_transcript.json": run_dir / "clean_transcript.json",
        "caption_project.v2.json": run_dir / "caption_project.v2.json",
        "transcript_words.json": run_dir / "transcript_words.json",
        "edl.json": run_dir / "edl.json",
    }


def _failed_evaluations(input_hashes: dict[str, str], message: str) -> dict[str, Any]:
    return {
        "schema_version": "highlight_evaluations.v1",
        "generator": {"name": "clip_automation.planning.candidate_evaluation", "version": "1"},
        "input_hashes": dict(sorted(input_hashes.items())),
        "status": "failed",
        "diagnostics": [{"severity": "error", "code": "upstream_failed", "message": message}],
        "candidate_ids": [],
        "weights": {"hook": 0.20, "standalone": 0.20, "interest": 0.20, "coherence": 0.15, "novelty": 0.10, "confidence": 0.15},
        "evaluations": [],
    }


def run_planning(
    run_dir: str | Path,
    *,
    planning_dir: str | Path,
    source_media_probe: dict[str, Any],
    candidate_provider: CandidateProvider | None = None,
    evaluator_provider: EvaluationProvider | None = None,
    chunking: ChunkingConfig = ChunkingConfig(),
    decision_settings: DecisionSettings = DecisionSettings(),
    score_weights: dict[str, float] | None = None,
    target_output: dict[str, Any] | None = None,
    output_preset: dict[str, Any] | None = None,
    progress_callback=None,
    cancel_event=None,
) -> PlanningRunResult:
    """Run Phase 2 into an explicit evaluation/planning directory.

    The source run is read-only.  Each selected candidate receives its own
    ``clips/<clip_id>/clip_plan.v1.json`` and ``edit_project.json``.
    """

    source_dir = Path(run_dir).resolve()
    output_dir = Path(planning_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs = _required_inputs(source_dir)
    missing = [name for name, path in inputs.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing planning input: " + ", ".join(missing))
    transcript = _load_json(inputs["clean_transcript.json"])
    input_hashes = {name: sha256_file(path) for name, path in inputs.items()}
    candidate_provider = candidate_provider or OllamaCandidateProvider()
    evaluator_provider = evaluator_provider or OllamaEvaluationProvider()
    settings = {
        "chunking": {"max_chars": chunking.max_chars, "max_duration": chunking.max_duration, "overlap_duration": chunking.overlap_duration},
        "decision": decision_settings.as_dict(),
        "score_weights": dict(sorted((score_weights or {}).items())),
    }
    manifest = new_manifest(
        input_fingerprint=planning_input_fingerprint(input_hashes, settings),
        provider_versions={
            "candidate": f"{getattr(candidate_provider, 'name', 'ollama')}:{getattr(candidate_provider, 'version', 'default')}",
            "evaluator": f"{getattr(evaluator_provider, 'name', 'ollama')}:{getattr(evaluator_provider, 'version', 'default')}",
        },
    )
    manifest_path = output_dir / "job_manifest.json"

    raise_if_cancelled(cancel_event)
    if progress_callback:
        progress_callback(0.05, "候補を生成中")
    candidates = generate_candidates(transcript, input_hashes=input_hashes, provider=candidate_provider, config=chunking)
    candidates_path = output_dir / "highlight_candidates.v1.json"
    write_artifact(candidates_path, "candidates", candidates)
    record_stage(
        manifest,
        "candidate_generation",
        state=candidates["status"] if candidates["status"] in {"fallback", "failed"} else "completed",
        artifact_path=candidates_path,
        schema_version=candidates["schema_version"],
        provider={"name": getattr(candidate_provider, "name", type(candidate_provider).__name__), "version": getattr(candidate_provider, "version", "unknown")},
    )
    write_manifest(manifest_path, manifest)

    raise_if_cancelled(cancel_event)
    if progress_callback:
        progress_callback(0.25, "候補を評価中")
    if candidates["status"] == "failed":
        evaluations = _failed_evaluations({"highlight_candidates.v1.json": artifact_hash(candidates)}, "candidate generation failed")
    else:
        evaluations = evaluate_candidates(
            candidates,
            input_hashes={**input_hashes, "highlight_candidates.v1.json": artifact_hash(candidates)},
            evaluator=evaluator_provider,
            weights=score_weights,
        )
    evaluations_path = output_dir / "highlight_evaluations.v1.json"
    write_artifact(evaluations_path, "evaluations", evaluations)
    record_stage(
        manifest,
        "candidate_evaluation",
        state=evaluations["status"] if evaluations["status"] == "failed" else ("fallback" if evaluations["status"] == "fallback" else "completed"),
        artifact_path=evaluations_path,
        schema_version=evaluations["schema_version"],
        provider={"name": getattr(evaluator_provider, "name", type(evaluator_provider).__name__), "version": getattr(evaluator_provider, "version", "unknown")},
    )
    write_manifest(manifest_path, manifest)

    raise_if_cancelled(cancel_event)
    if progress_callback:
        progress_callback(0.45, "採否を決定中")
    if evaluations["status"] == "failed":
        raise PlanningRunError("candidate evaluation failed", manifest)
    decision = decide_clips(
        candidates,
        evaluations,
        input_hashes={**input_hashes, "highlight_evaluations.v1.json": artifact_hash(evaluations)},
        settings=decision_settings,
    )
    decision_path = output_dir / "clip_decision.v1.json"
    write_artifact(decision_path, "decision", decision)
    record_stage(
        manifest,
        "clip_decision",
        state=decision["status"] if decision["status"] in {"fallback", "degraded"} else "completed",
        artifact_path=decision_path,
        schema_version=decision["schema_version"],
    )
    write_manifest(manifest_path, manifest)

    raise_if_cancelled(cancel_event)
    if progress_callback:
        progress_callback(0.65, "クリップ計画を生成中")
    plans = plan_clips(
        candidates,
        decision,
        input_hashes={**input_hashes, "clip_decision.v1.json": artifact_hash(decision)},
        target_output=target_output or {"width": 1080, "height": 1920, "fps": 30},
        output_preset=output_preset,
    )
    plan_paths: list[Path] = []
    for plan in plans:
        raise_if_cancelled(cancel_event)
        plan_path = output_dir / "clips" / plan["clip_id"] / "clip_plan.v1.json"
        write_artifact(plan_path, "plan", plan)
        plan_paths.append(plan_path)
    planning_state = plans[0]["status"] if plans and plans[0]["status"] in {"fallback", "degraded"} else "completed"
    record_stage(
        manifest,
        "clip_planning",
        state=planning_state,
        artifacts=[
            {
                "clip_id": plan["clip_id"],
                "path": str(plan_path),
                "schema_version": plan["schema_version"],
                "state": plan["status"] if plan["status"] in {"fallback", "degraded"} else "completed",
            }
            for plan, plan_path in zip(plans, plan_paths)
        ],
        schema_version="clip_plan.v1",
    )
    write_manifest(manifest_path, manifest)

    edit_projects: list[dict[str, Any]] = []
    contract_entries: list[dict[str, Any]] = []
    for plan in plans:
        raise_if_cancelled(cancel_event)
        edit_project_path = output_dir / "clips" / plan["clip_id"] / "edit_project.json"
        try:
            result = build_edit_project_from_artifacts(
                source_dir,
                source_media_probe=source_media_probe,
                regions=plan["regions"],
                source_run_id=f"{source_dir.name}/{plan['clip_id']}",
                project_id=plan["clip_id"],
                project_name=f"{source_dir.name} {plan['clip_id']}",
                additional_input_hashes={"planning:clip_plan.v1": artifact_hash(plan)},
                sequence_output=plan["target_output"],
                contract_path=edit_project_path,
            )
            write_edit_project(edit_project_path, result.project)
            edit_projects.append(result.project)
            contract_entries.append(
                {
                    "clip_id": plan["clip_id"],
                    "path": str(edit_project_path),
                    "schema_version": "edit_project.v1",
                    "state": "completed",
                }
            )
            record_stage(
                manifest,
                "contract_generation",
                state="completed",
                artifacts=contract_entries,
                schema_version="edit_project.v1",
            )
            write_manifest(manifest_path, manifest)
            if progress_callback:
                progress_callback(0.65 + 0.35 * (len(edit_projects) / max(1, len(plans))), f"contract {len(edit_projects)}/{len(plans)}")
        except (EditProjectBuildError, ValueError) as error:
            contract_entries.append(
                {
                    "clip_id": plan["clip_id"],
                    "path": str(edit_project_path),
                    "schema_version": "edit_project.v1",
                    "state": "failed",
                }
            )
            record_stage(
                manifest,
                "contract_generation",
                state="failed",
                artifacts=contract_entries,
                schema_version="edit_project.v1",
                error={"type": type(error).__name__, "message": str(error)},
            )
            write_manifest(manifest_path, manifest)
            raise PlanningRunError("edit_project generation failed", manifest) from error
    contract_state = plans[0]["status"] if plans and plans[0]["status"] in {"fallback", "degraded"} else "completed"
    record_stage(
        manifest,
        "contract_generation",
        state=contract_state,
        artifacts=contract_entries,
        schema_version="edit_project.v1",
    )
    write_manifest(manifest_path, manifest)
    return PlanningRunResult(output_dir, manifest, candidates, evaluations, decision, tuple(plans), tuple(edit_projects))
