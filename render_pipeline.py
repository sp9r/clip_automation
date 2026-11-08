"""Phase 5 orchestration: render each planned edit_project independently."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from planning_pipeline import PlanningRunResult, run_planning
from planning_manifest import load_manifest as load_planning_manifest
from job_control import raise_if_cancelled
from render_manifest import (
    load_manifest,
    new_render_manifest,
    portable_relative,
    sha256_file,
    upsert_clip,
    write_manifest,
)
from video_editor_runner import (
    RenderInvocation,
    RenderResult,
    VideoEditorConfig,
    VideoEditorRunner,
    resolve_video_editor_config,
)


@dataclass(frozen=True)
class RenderPlanningResult:
    planning_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]
    results: tuple[RenderResult, ...]

    @property
    def failed(self) -> bool:
        return bool(self.manifest["summary"]["failed"] or self.manifest["summary"]["skipped"])


def _absolute_path(path_value: str, planning_dir: Path) -> Path:
    path = Path(path_value)
    return path.resolve() if path.is_absolute() else (planning_dir / path).resolve()


def _entry(
    *,
    planning_dir: Path,
    clip_id: str,
    state: str,
    edit_project_path: Path,
    edit_project_sha256: str | None,
    output_path: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
    result: RenderResult | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    completed = result is not None and result.state == "completed"
    return {
        "clip_id": clip_id,
        "state": state,
        "edit_project": {
            "path": portable_relative(edit_project_path, planning_dir),
            "sha256": edit_project_sha256,
        },
        "output": {
            "path": portable_relative(output_path, planning_dir),
            "sha256": result.output_sha256 if completed else None,
            "size": result.output_size if completed else 0,
        },
        "logs": {
            "stdout": portable_relative(stdout_log_path, planning_dir),
            "stderr": portable_relative(stderr_log_path, planning_dir),
        },
        "exit_code": result.exit_code if result is not None else None,
        "stdout_json": result.stdout_json if completed else None,
        "error": error if error is not None else (result.error if result is not None else None),
    }


def _contract_entries(planning_dir: Path) -> list[dict[str, Any]]:
    planning_manifest = load_planning_manifest(planning_dir / "job_manifest.json")
    stage = planning_manifest["stages"]["contract_generation"]
    entries = stage.get("artifacts", [])
    return [dict(entry) for entry in entries]


def render_planning_outputs(
    planning_dir: str | Path,
    *,
    config: VideoEditorConfig | None = None,
    runner: VideoEditorRunner | None = None,
    explicit_command: Sequence[str] | None = None,
    video_editor_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    progress_callback=None,
    cancel_event=None,
) -> RenderPlanningResult:
    """Render every completed contract in planning order, never concatenate them."""

    planning_root = Path(planning_dir).resolve()
    planning_manifest_path = planning_root / "job_manifest.json"
    if not planning_manifest_path.is_file():
        raise FileNotFoundError(f"planning manifest is missing: {planning_manifest_path}")
    if runner is None:
        resolved = config or resolve_video_editor_config(
            explicit_command=explicit_command,
            video_editor_root=video_editor_root,
            repo_root=repo_root,
        )
        runner = VideoEditorRunner(resolved)
    manifest_path = planning_root / "render_manifest.v1.json"
    runner.preflight()
    manifest = new_render_manifest(
        planning_dir=planning_root,
        planning_manifest_path=planning_manifest_path,
        renderer=runner.config.provenance,
    )
    write_manifest(manifest_path, manifest)
    results: list[RenderResult] = []

    artifacts = _contract_entries(planning_root)
    total = len(artifacts)
    for index, artifact in enumerate(artifacts, start=1):
        raise_if_cancelled(cancel_event)
        clip_id = str(artifact.get("clip_id") or "")
        edit_project_path = _absolute_path(str(artifact.get("path", "")), planning_root)
        clip_dir = planning_root / "clips" / clip_id
        automation_dir = clip_dir / "automation"
        output_path = automation_dir / "rendered.mp4"
        stdout_log_path = automation_dir / "render.stdout.log"
        stderr_log_path = automation_dir / "render.stderr.log"
        staging_path = automation_dir / f".render-{uuid.uuid4().hex}.mp4"
        edit_project_sha256 = sha256_file(edit_project_path) if edit_project_path.is_file() else None
        state = str(artifact.get("state", "failed"))

        if state != "completed":
            upsert_clip(
                manifest,
                _entry(
                    planning_dir=planning_root,
                    clip_id=clip_id,
                    state="skipped",
                    edit_project_path=edit_project_path,
                    edit_project_sha256=edit_project_sha256,
                    output_path=output_path,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                    error="edit_project generation did not complete",
                ),
            )
            write_manifest(manifest_path, manifest)
            if progress_callback:
                progress_callback(index / max(1, total), f"クリップ {index}/{total} はplanning失敗のためskip")
            continue

        if not edit_project_path.is_file():
            result = RenderResult(
                clip_id=clip_id,
                state="failed",
                exit_code=None,
                output_path=output_path,
                output_sha256=None,
                output_size=0,
                stdout_json=None,
                stderr_tail="",
                error=f"edit_project is missing: {edit_project_path}",
            )
        else:
            invocation = RenderInvocation(
                clip_id=clip_id,
                edit_project_path=edit_project_path,
                output_path=output_path,
                staging_path=staging_path,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
                cancel_event=cancel_event,
            )
            upsert_clip(
                manifest,
                _entry(
                    planning_dir=planning_root,
                    clip_id=clip_id,
                    state="running",
                    edit_project_path=edit_project_path,
                    edit_project_sha256=edit_project_sha256,
                    output_path=output_path,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                ),
            )
            write_manifest(manifest_path, manifest)
            try:
                result = runner.render(invocation)
            except PipelineCancelled:
                raise
            except Exception as error:  # each clip is isolated; later clips still run
                result = RenderResult(
                    clip_id=clip_id,
                    state="failed",
                    exit_code=None,
                    output_path=output_path,
                    output_sha256=None,
                    output_size=0,
                    stdout_json=None,
                    stderr_tail="",
                    error=str(error),
                )
        results.append(result)
        upsert_clip(
            manifest,
            _entry(
                planning_dir=planning_root,
                clip_id=clip_id,
                state=result.state,
                edit_project_path=edit_project_path,
                edit_project_sha256=edit_project_sha256,
                output_path=output_path,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
                result=result,
            ),
        )
        write_manifest(manifest_path, manifest)
        if progress_callback:
            progress_callback(index / max(1, total), f"クリップ {index}/{total} を処理しました")

    return RenderPlanningResult(planning_root, manifest_path, load_manifest(manifest_path), tuple(results))


def run_phase5(
    run_dir: str | Path,
    *,
    planning_dir: str | Path,
    source_media_probe: dict[str, Any],
    candidate_provider: Any | None = None,
    evaluator_provider: Any | None = None,
    video_editor_root: str | Path | None = None,
    explicit_command: Sequence[str] | None = None,
    repo_root: str | Path | None = None,
    **planning_options: Any,
) -> tuple[PlanningRunResult, RenderPlanningResult]:
    """Run deterministic planning first, then independent external renders."""

    cancel_event = planning_options.pop("cancel_event", None)
    planning_result = run_planning(
        run_dir,
        planning_dir=planning_dir,
        source_media_probe=source_media_probe,
        candidate_provider=candidate_provider,
        evaluator_provider=evaluator_provider,
        cancel_event=cancel_event,
        **planning_options,
    )
    render_result = render_planning_outputs(
        planning_dir,
        explicit_command=explicit_command,
        video_editor_root=video_editor_root,
        repo_root=repo_root,
        cancel_event=cancel_event,
    )
    return planning_result, render_result
