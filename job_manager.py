import hashlib
import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from job_control import PipelineCancelled
from pipeline import (
    PROJECT_ROOT,
    STAGE_DEFINITIONS,
    stage_definitions_for,
    _create_run_dir,
    inspect_run,
    run_pipeline,
)
from subtitle_project import load_caption_project
from errors import PipelineUserError
from caption_settings import DEFAULT_MAX_LINES, validate_max_chars, validate_max_lines
from processing_device import DEFAULT_PROCESSING_DEVICE, validate_processing_device


MANIFEST_NAME = "job_manifest.json"
MANIFEST_SCHEMA_VERSION = 3

STAGE_ARTIFACTS = {
    "acquire": (),
    "transcribe": ("transcript.json",),
    "jumpcut": ("edl.json", "clean_transcript.json", "silence_analysis.json"),
    "captions": ("captions.srt", "captions.ass", "caption_project.json"),
    "render": ("final_jumpcut_video.mp4",),
    "highlight": ("highlight.json",),
}

V2_STAGE_ARTIFACTS = {
    "acquire": (),
    "transcribe": ("transcript.json",),
    "jumpcut": ("edl.json", "clean_transcript.json", "silence_analysis.json"),
    "captions": (
        "captions.srt", "captions.ass", "caption_project.json",
        "caption_project.v2.json", "transcript_words.json", "migration_report.json",
    ),
    "planning": (
        "planning/job_manifest.json", "planning/highlight_candidates.v1.json",
        "planning/highlight_evaluations.v1.json", "planning/clip_decision.v1.json",
    ),
    "render": ("planning/render_manifest.v1.json",),
}

SETTING_STAGE = {
    "max_chars": "captions",
    "max_lines": "captions",
    "use_llm_captions": "captions",
    "caption_model": "captions",
    "default_template_id": "captions",
    "output_width": "render",
    "output_height": "render",
    "output_fps": "render",
    "video_encoder": "render",
    "processing_device": "transcribe",
}

V2_SETTING_STAGE = {
    "max_chars": "captions",
    "max_lines": "captions",
    "use_llm_captions": "captions",
    "caption_model": "captions",
    "default_template_id": "captions",
    "output_width": "planning",
    "output_height": "planning",
    "output_fps": "planning",
    "processing_device": "transcribe",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    last_error = None
    for attempt in range(10):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.03 * (attempt + 1))
    try:
        temp_path.unlink(missing_ok=True)
    finally:
        raise last_error


def _new_stages(pipeline_version: str = "legacy") -> dict:
    return {
        stage_id: {
            "label": label,
            "weight": weight,
            "status": "pending",
            "progress": 0.0,
            "message": "待機中",
            "started_at": None,
            "finished_at": None,
            "input_fingerprint": None,
            "artifact_fingerprint": None,
        }
        for stage_id, label, weight in stage_definitions_for(pipeline_version)
    }


def _file_fingerprint(paths: list[Path]) -> str | None:
    digest = hashlib.sha256()
    found = False
    for path in paths:
        if not path.is_file():
            continue
        found = True
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest() if found else None


def _calculate_overall(stages: dict) -> float:
    return round(
        sum(float(item["weight"]) * float(item["progress"]) for item in stages.values())
        * 100,
        2,
    )


def _stage_number(stage_id: str | None, pipeline_version: str = "legacy") -> int:
    ids = [item[0] for item in stage_definitions_for(pipeline_version)]
    return ids.index(stage_id) + 1 if stage_id in ids else 0


def _request_key(source: str, is_local: bool, settings: dict, pipeline_version: str = "v2") -> str:
    source_text = str(source or "").strip()
    if is_local and source_text:
        source_text = os.path.normcase(os.path.abspath(source_text))
    payload = {
        "source": source_text,
        "is_local": bool(is_local),
        "settings": settings,
        "pipeline_version": pipeline_version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class JobManager:
    def __init__(self, runs_dir: str | os.PathLike | None = None, *, pipeline_runner=None):
        self.runs_dir = (
            Path(runs_dir).resolve()
            if runs_dir
            else (PROJECT_ROOT / "data" / "runs").resolve()
        )
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active: dict[str, dict] = {}
        self._pipeline_runner = pipeline_runner
        self._recover_stale_manifests()

    @staticmethod
    def _pipeline_version(manifest: dict) -> str:
        version = manifest.get("pipeline_version")
        return version if version in {"v2", "legacy"} else "legacy"

    @classmethod
    def _stage_definitions(cls, manifest: dict):
        return stage_definitions_for(cls._pipeline_version(manifest))

    @classmethod
    def _stage_artifact_paths(cls, manifest: dict, stage_id: str) -> list[Path]:
        run_dir = Path(manifest["run_dir"])
        version = cls._pipeline_version(manifest)
        table = V2_STAGE_ARTIFACTS if version == "v2" else STAGE_ARTIFACTS
        paths = [run_dir / item for item in table.get(stage_id, ())]
        if version == "v2" and stage_id in {"planning", "render"}:
            planning_dir = run_dir / "planning"
            if stage_id == "planning":
                paths.extend(planning_dir.glob("clips/*/clip_plan.v1.json"))
                paths.extend(planning_dir.glob("clips/*/edit_project.json"))
            else:
                paths.extend(planning_dir.glob("clips/*/automation/rendered.mp4"))
        return paths

    def _recover_stale_manifests(self):
        for path in self.runs_dir.glob(f"*/{MANIFEST_NAME}"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") not in {"running", "cancel_requested"}:
                    continue
                stage_id = data.get("current_stage")
                if stage_id in data.get("stages", {}):
                    data["stages"][stage_id]["status"] = "interrupted"
                data.update({
                    "status": "interrupted", "message": "前回のアプリ終了で中断されました。再開できます。",
                    "eta_seconds": None, "finished_at": _now_iso(),
                })
                _atomic_write(path, data)
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def _manifest_path(self, job_id: str) -> Path:
        run_dir = (self.runs_dir / job_id).resolve()
        if run_dir.parent != self.runs_dir:
            raise ValueError("run IDが不正です。")
        return run_dir / MANIFEST_NAME

    def _load(self, job_id: str) -> dict:
        manifest_path = self._manifest_path(job_id)
        if not manifest_path.is_file():
            return self.adopt_run(job_id)
        with manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)
        schema_version = int(manifest.get("schema_version", 1))
        if schema_version > MANIFEST_SCHEMA_VERSION:
            raise ValueError("未対応のジョブマニフェストです。")
        migrated = schema_version != MANIFEST_SCHEMA_VERSION or "pipeline_version" not in manifest
        manifest.setdefault("pipeline_version", "legacy")
        if manifest["pipeline_version"] not in {"v2", "legacy"}:
            raise ValueError("未対応のpipeline_versionです。")
        manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
        for stage in manifest.get("stages", {}).values():
            stage.setdefault("input_fingerprint", None)
            stage.setdefault("artifact_fingerprint", None)
        manifest.setdefault(
            "request_key",
            _request_key(
                manifest.get("source", ""),
                bool(manifest.get("is_local")),
                manifest.get("settings", {}),
                manifest.get("pipeline_version", "legacy"),
            ),
        )
        manifest.setdefault(
            "attempt_started_progress",
            float(manifest.get("overall_progress", 0.0)),
        )
        manifest.setdefault("progress_rate_per_second", None)
        if migrated:
            self._save(manifest)
        return manifest

    def _save(self, manifest: dict):
        manifest["updated_at"] = _now_iso()
        manifest["overall_progress"] = _calculate_overall(manifest["stages"])
        _atomic_write(self._manifest_path(manifest["job_id"]), manifest)

    def _base_manifest(
        self,
        *,
        job_id: str,
        source: str,
        is_local: bool,
        settings: dict,
        pipeline_version: str = "v2",
    ) -> dict:
        stage_definitions_for(pipeline_version)
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "pipeline_version": pipeline_version,
            "job_id": job_id,
            "run_dir": str((self.runs_dir / job_id).resolve()),
            "status": "pending",
            "source": source,
            "is_local": bool(is_local),
            "settings": settings,
            "request_key": _request_key(source, is_local, settings, pipeline_version),
            "current_stage": None,
            "stage_number": 0,
            "stage_count": len(stage_definitions_for(pipeline_version)),
            "stage_progress": 0.0,
            "overall_progress": 0.0,
            "message": "開始待ち",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": 0.0,
            "eta_seconds": None,
            "attempt_started_progress": 0.0,
            "progress_rate_per_second": None,
            "stages": _new_stages(pipeline_version),
            "artifacts": {},
            "results": None,
            "error": None,
            "force_render_pending": False,
        }

    def _invalidate_from(self, manifest: dict, stage_id: str):
        pipeline_version = self._pipeline_version(manifest)
        stage_ids = [item[0] for item in stage_definitions_for(pipeline_version)]
        target_index = stage_ids.index(stage_id)
        for current in stage_ids[target_index:]:
            for path in self._stage_artifact_paths(manifest, current):
                path.unlink(missing_ok=True)
            manifest["stages"][current].update(
                {
                    "status": "pending", "progress": 0.0, "message": "待機中",
                    "started_at": None, "finished_at": None,
                    "input_fingerprint": None, "artifact_fingerprint": None,
                }
            )
        manifest.update(
            {
                "status": "pending", "current_stage": stage_id,
                "stage_number": _stage_number(stage_id, pipeline_version), "stage_progress": 0.0,
                "results": None, "error": None, "eta_seconds": None,
            }
        )

    def _record_fingerprints(self, manifest: dict):
        run_dir = Path(manifest["run_dir"])
        pipeline_version = self._pipeline_version(manifest)
        stage_definitions = stage_definitions_for(pipeline_version)
        setting_stage = V2_SETTING_STAGE if pipeline_version == "v2" else SETTING_STAGE
        previous = hashlib.sha256(
            json.dumps(
                {"source": manifest.get("source"), "is_local": manifest.get("is_local")},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        for stage_id, _, _ in stage_definitions:
            relevant_settings = {
                key: value for key, value in manifest["settings"].items()
                if setting_stage.get(key) == stage_id
            }
            input_digest = hashlib.sha256(
                json.dumps(
                    {"upstream": previous, "settings": relevant_settings},
                    ensure_ascii=False, sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            artifact = _file_fingerprint(self._stage_artifact_paths(manifest, stage_id))
            manifest["stages"][stage_id]["input_fingerprint"] = input_digest
            manifest["stages"][stage_id]["artifact_fingerprint"] = artifact
            previous = artifact or input_digest

    def create_job(
        self,
        source: str,
        *,
        is_local: bool,
        settings: dict,
        auto_start: bool = True,
        pipeline_version: str = "v2",
    ) -> dict:
        with self._lock:
            stage_definitions_for(pipeline_version)
            settings = dict(settings)
            settings["max_chars"] = validate_max_chars(settings.get("max_chars"))
            settings["max_lines"] = validate_max_lines(
                settings.get("max_lines", DEFAULT_MAX_LINES)
            )
            settings["processing_device"] = validate_processing_device(
                settings.get("processing_device", DEFAULT_PROCESSING_DEVICE)
            )
            request_key = _request_key(str(source), is_local, settings, pipeline_version)
            for manifest_path in sorted(
                self.runs_dir.glob(f"*/{MANIFEST_NAME}"), reverse=True
            ):
                try:
                    candidate = self.get(manifest_path.parent.name)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    candidate.get("request_key") == request_key
                    and candidate.get("status")
                    in {"pending", "running", "cancel_requested"}
                ):
                    candidate["request_reused"] = True
                    return candidate

            run_dir = _create_run_dir(self.runs_dir.parent)
            job_id = run_dir.name
            manifest = self._base_manifest(
                job_id=job_id,
                source=str(source),
                is_local=is_local,
                settings=settings,
                pipeline_version=pipeline_version,
            )
            self._save(manifest)
            if auto_start:
                self.start(job_id, resume=False)
            result = self.get(job_id)
            result["request_reused"] = False
            return result

    def adopt_run(self, job_id_or_path: str | os.PathLike) -> dict:
        candidate = Path(job_id_or_path)
        run_dir = candidate.resolve() if candidate.is_dir() else (self.runs_dir / str(candidate)).resolve()
        if run_dir.parent != self.runs_dir or not run_dir.is_dir():
            raise ValueError("既存runが見つかりません。")
        job_id = run_dir.name
        manifest_path = run_dir / MANIFEST_NAME
        if manifest_path.is_file():
            with manifest_path.open("r", encoding="utf-8") as file:
                return json.load(file)

        snapshot = inspect_run(run_dir)
        source = snapshot.get("source_path") or ""
        settings = {
            "output_width": 1920,
            "output_height": 1080,
            "output_fps": 60,
            "max_chars": 32,
            "max_lines": DEFAULT_MAX_LINES,
            "use_llm_captions": True,
            "caption_model": "qwen2.5:14b",
            "default_template_id": "standard",
            "video_encoder": "auto",
            "processing_device": DEFAULT_PROCESSING_DEVICE,
        }
        project_path = Path(snapshot["paths"]["caption_project"])
        if project_path.is_file():
            try:
                project = load_caption_project(project_path)
                settings.update(
                    {
                        "output_width": int(project["output"]["width"]),
                        "output_height": int(project["output"]["height"]),
                        "output_fps": int(project["output"]["fps"]),
                        "max_chars": int(project["caption_settings"]["max_chars"]),
                        "max_lines": int(project["caption_settings"].get("max_lines", DEFAULT_MAX_LINES)),
                        "default_template_id": project["default_template_id"],
                    }
                )
            except (KeyError, OSError, ValueError):
                pass

        manifest = self._base_manifest(
            job_id=job_id,
            source=source,
            is_local=True,
            settings=settings,
            pipeline_version="legacy",
        )
        for stage_id, _, _ in stage_definitions_for("legacy"):
            completed = snapshot["completed"].get(stage_id, False)
            if completed:
                stage = manifest["stages"][stage_id]
                stage.update(
                    {
                        "status": "completed",
                        "progress": 1.0,
                        "message": "既存成果物を確認",
                        "finished_at": _now_iso(),
                    }
                )
        first_incomplete = next(
            (
                stage_id
                for stage_id, _, _ in stage_definitions_for("legacy")
                if not snapshot["completed"].get(stage_id, False)
            ),
            None,
        )
        manifest["status"] = "completed" if first_incomplete is None else "canceled"
        manifest["current_stage"] = first_incomplete
        manifest["stage_number"] = _stage_number(first_incomplete, "legacy")
        manifest["stage_progress"] = 0.0
        manifest["message"] = (
            "既存runは完了済みです。"
            if first_incomplete is None
            else f"{manifest['stages'][first_incomplete]['label']}から再開できます。"
        )
        manifest["artifacts"] = snapshot["paths"]
        self._save(manifest)
        return manifest

    def _update_progress(self, job_id: str, stage_id: str, progress: float, message: str):
        with self._lock:
            manifest = self._load(job_id)
            stage = manifest["stages"][stage_id]
            now = _now_iso()
            if stage["started_at"] is None:
                stage["started_at"] = now
            stage["status"] = "completed" if progress >= 1 else "running"
            stage["progress"] = round(max(0.0, min(1.0, float(progress))), 5)
            stage["message"] = message
            if progress >= 1:
                stage["finished_at"] = now
            manifest["status"] = "running"
            manifest["current_stage"] = stage_id
            manifest["stage_number"] = _stage_number(stage_id, self._pipeline_version(manifest))
            manifest["stage_progress"] = round(stage["progress"] * 100, 2)
            manifest["message"] = message
            if manifest["started_at"]:
                started = datetime.fromisoformat(manifest["started_at"])
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                manifest["elapsed_seconds"] = round(max(0.0, elapsed), 1)
                overall = _calculate_overall(manifest["stages"])
                baseline = float(manifest.get("attempt_started_progress", 0.0))
                gained = max(0.0, overall - baseline)
                if elapsed >= 1.0 and gained >= 0.05:
                    current_rate = gained / elapsed
                    previous_rate = manifest.get("progress_rate_per_second")

                    if previous_rate is None or elapsed < 5.0:
                        smoothed_rate = current_rate
                    else:
                        # 過去の傾向を重視しつつ、直近の変動をなだらかに取り入れる（alpha=0.2）
                        alpha = 0.2
                        smoothed_rate = alpha * current_rate + (1.0 - alpha) * float(previous_rate)

                    manifest["progress_rate_per_second"] = round(smoothed_rate, 6)
                    manifest["eta_seconds"] = round(
                        max(0.0, 100.0 - overall) / smoothed_rate,
                        1,
                    )
                else:
                    manifest["progress_rate_per_second"] = None
                    manifest["eta_seconds"] = None
            self._save(manifest)

    def _worker(
        self,
        job_id: str,
        resume: bool,
        force_render: bool,
        cancel_event: threading.Event,
    ):
        with self._lock:
            manifest = self._load(job_id)
            baseline_progress = _calculate_overall(manifest["stages"])
            manifest.update(
                {
                    "status": "running",
                    # ETAは過去の中断時間を含めず、今回の実行速度から計算する。
                    "started_at": _now_iso(),
                    "finished_at": None,
                    "elapsed_seconds": 0.0,
                    "eta_seconds": None,
                    "attempt_started_progress": baseline_progress,
                    "progress_rate_per_second": None,
                    "error": None,
                    "message": "処理を開始しました。",
                }
            )
            self._save(manifest)
        try:
            settings = manifest["settings"]
            pipeline_runner = self._pipeline_runner or run_pipeline
            results = pipeline_runner(
                manifest["source"],
                is_local_file=manifest["is_local"],
                existing_run_dir=manifest["run_dir"],
                pipeline_version=manifest.get("pipeline_version", "legacy"),
                resume=resume,
                output_width=settings["output_width"],
                output_height=settings["output_height"],
                output_fps=settings["output_fps"],
                max_chars=settings["max_chars"],
                max_lines=settings.get("max_lines", DEFAULT_MAX_LINES),
                use_llm_captions=settings["use_llm_captions"],
                caption_model=settings["caption_model"],
                default_template_id=settings["default_template_id"],
                video_encoder=settings.get("video_encoder", "auto"),
                processing_device=validate_processing_device(settings.get("processing_device", DEFAULT_PROCESSING_DEVICE)),
                force_render=force_render,
                progress_callback=lambda stage, value, message: self._update_progress(
                    job_id, stage, value, message
                ),
                cancel_event=cancel_event,
            )
        except PipelineCancelled as exc:
            with self._lock:
                manifest = self._load(job_id)
                stage_id = manifest.get("current_stage")
                if stage_id:
                    manifest["stages"][stage_id]["status"] = "canceled"
                manifest.update(
                    {
                        "status": "canceled",
                        "message": str(exc),
                        "finished_at": _now_iso(),
                        "eta_seconds": None,
                    }
                )
                self._save(manifest)
        except Exception as exc:
            with self._lock:
                manifest = self._load(job_id)
                stage_id = manifest.get("current_stage")
                if stage_id:
                    manifest["stages"][stage_id]["status"] = "failed"
                manifest.update(
                    {
                        "status": "failed",
                        "message": f"処理失敗: {exc}",
                        "error": exc.to_dict(traceback.format_exc()) if isinstance(exc, PipelineUserError) else {
                            "type": type(exc).__name__,
                            "message": str(exc),
                            "error_code": "UNEXPECTED_ERROR",
                            "stage": stage_id,
                            "retryable": False,
                            "user_action": "診断詳細を確認して、失敗段階から再開してください。",
                            "traceback": traceback.format_exc(),
                        },
                        "finished_at": _now_iso(),
                        "eta_seconds": None,
                    }
                )
                self._save(manifest)
        else:
            with self._lock:
                manifest = self._load(job_id)
                pipeline_version = self._pipeline_version(manifest)
                stage_definitions = stage_definitions_for(pipeline_version)
                result_status = results.get("status") if isinstance(results, dict) else None
                final_status = "failed" if result_status == "failed" else "completed"
                if final_status == "failed":
                    manifest["stages"][stage_definitions[-1][0]].update(
                        {"status": "failed", "message": "一部のクリップが失敗しました。"}
                    )
                manifest.update(
                    {
                        "status": final_status,
                        "current_stage": stage_definitions[-1][0],
                        "stage_number": len(stage_definitions),
                        "stage_progress": 100.0 if final_status == "completed" else manifest.get("stage_progress", 0.0),
                        "overall_progress": 100.0 if final_status == "completed" else _calculate_overall(manifest["stages"]),
                        "message": "全処理が完了しました。" if final_status == "completed" else "一部のクリップが失敗しました。成功した出力は保持されています。",
                        "results": results,
                        "finished_at": _now_iso(),
                        "eta_seconds": 0.0,
                        "force_render_pending": False,
                    }
                )
                inspected = inspect_run(manifest["run_dir"])
                manifest["artifacts"] = inspected.get("paths", {})
                if pipeline_version == "v2":
                    manifest["artifacts"].update({
                        "planning_manifest": inspected.get("planning_manifest_path"),
                        "render_manifest": inspected.get("render_manifest_path"),
                    })
                self._record_fingerprints(manifest)
                self._save(manifest)
        finally:
            with self._lock:
                self._active.pop(job_id, None)

    def start(
        self,
        job_id: str,
        *,
        resume: bool = True,
        force_render: bool | None = None,
    ) -> dict:
        with self._lock:
            active = self._active.get(job_id)
            if active and active["thread"].is_alive():
                return self._load(job_id)
            manifest = self._load(job_id)
            if force_render is None:
                force_render = bool(manifest.get("force_render_pending"))
            if manifest["status"] == "completed" and not force_render:
                return manifest
            if force_render:
                manifest["force_render_pending"] = True
                render_stage = manifest["stages"]["render"]
                render_stage.update(
                    {
                        "status": "pending",
                        "progress": 0.0,
                        "message": "再レンダリング待ち",
                        "started_at": None,
                        "finished_at": None,
                    }
                )
                manifest["status"] = "pending"
                manifest["message"] = "字幕編集を反映する再レンダリング待ち"
                self._save(manifest)
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._worker,
                args=(job_id, resume, bool(force_render), cancel_event),
                daemon=True,
                name=f"clip-job-{job_id}",
            )
            self._active[job_id] = {"thread": thread, "cancel_event": cancel_event}
            thread.start()
            return self._load(job_id)

    def rewind_and_start(self, job_id: str, stage_id: str) -> dict:
        with self._lock:
            manifest = self._load(job_id)
            active = self._active.get(job_id)
            if active and active["thread"].is_alive():
                raise RuntimeError("実行中のジョブはやり直せません。先に停止してください。")
            
            pipeline_version = self._pipeline_version(manifest)
            stage_ids = [item[0] for item in stage_definitions_for(pipeline_version)]
            if stage_id not in stage_ids:
                raise ValueError(f"無効な段階IDです: {stage_id}")
            if stage_id == "acquire":
                raise ValueError("動画取得(acquire)からのやり直しはサポートしていません。新しいジョブを作成してください。")
            
            target_index = stage_ids.index(stage_id)
            
            for i, s_id in enumerate(stage_ids):
                if i >= target_index:
                    for path in self._stage_artifact_paths(manifest, s_id):
                        path.unlink(missing_ok=True)
                    
                    manifest["stages"][s_id].update({
                        "status": "pending",
                        "progress": 0.0,
                        "message": "待機中",
                        "started_at": None,
                        "finished_at": None,
                    })
            
            manifest["status"] = "canceled"
            manifest["current_stage"] = stage_id
            manifest["stage_number"] = target_index + 1
            manifest["stage_progress"] = 0.0
            manifest["message"] = f"{manifest['stages'][stage_id]['label']}以降をリセットしました。再開します。"
            manifest["eta_seconds"] = None
            manifest["force_render_pending"] = False
            self._save(manifest)
            
        return self.start(job_id, resume=True)

    def request_rerender(self, job_id: str, *, auto_start: bool = True) -> dict:
        """字幕編集を保持したまま、render以降だけを再実行可能にする。"""
        with self._lock:
            manifest = self._load(job_id)
            active = self._active.get(job_id)
            if active and active["thread"].is_alive():
                raise RuntimeError("実行中のジョブは再レンダリングできません。先に停止してください。")

            pipeline_version = self._pipeline_version(manifest)
            stages = ("render",) if pipeline_version == "v2" else ("render", "highlight")
            for stage_id in stages:
                if pipeline_version == "v2" and stage_id == "render":
                    # render-only retryは同じedit_projectのexecution retryである。
                    # 既存の正常MP4を先に削除すると、rendererのstaging -> validate
                    # -> atomic replaceによる失敗時保持契約を壊してしまう。
                    # 新しいrender manifestはrender実行開始時にatomicに再生成する。
                    (Path(manifest["run_dir"]) / "planning" / "render_manifest.v1.json").unlink(
                        missing_ok=True
                    )
                else:
                    for path in self._stage_artifact_paths(manifest, stage_id):
                        path.unlink(missing_ok=True)
                manifest["stages"][stage_id].update(
                    {
                        "status": "pending",
                        "progress": 0.0,
                        "message": "再レンダリング待ち" if stage_id == "render" else "待機中",
                        "started_at": None,
                        "finished_at": None,
                    }
                )
            manifest.update(
                {
                    "status": "pending",
                    "current_stage": "render",
                    "stage_number": _stage_number("render", pipeline_version),
                    "stage_progress": 0.0,
                    "message": "字幕編集を反映する再レンダリング待ち",
                    "eta_seconds": None,
                    "force_render_pending": True,
                    "results": None,
                    "error": None,
                }
            )
            self._save(manifest)

        return self.start(job_id, resume=True, force_render=True) if auto_start else self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        with self._lock:
            manifest = self._load(job_id)
            active = self._active.get(job_id)
            if not active or not active["thread"].is_alive():
                if manifest["status"] not in {"completed", "failed", "canceled"}:
                    manifest["status"] = "canceled"
                    manifest["message"] = "停止済みです。再開できます。"
                    self._save(manifest)
                return manifest
            active["cancel_event"].set()
            manifest["status"] = "cancel_requested"
            manifest["message"] = "安全な停止を要求しました。"
            self._save(manifest)
            return manifest

    def update_settings(self, job_id: str, updates: dict, *, invalidate: bool = True) -> dict:
        with self._lock:
            manifest = self._load(job_id)
            active = self._active.get(job_id)
            if active and active["thread"].is_alive():
                raise RuntimeError("実行中の設定は変更できません。先に停止してください。")
            updates = dict(updates)
            if "max_chars" in updates:
                updates["max_chars"] = validate_max_chars(updates["max_chars"])
            if "max_lines" in updates:
                updates["max_lines"] = validate_max_lines(updates["max_lines"])
            if "processing_device" in updates:
                updates["processing_device"] = validate_processing_device(updates["processing_device"])
            changed = [key for key, value in updates.items() if manifest["settings"].get(key) != value]
            manifest["settings"].update(updates)
            if invalidate and changed:
                pipeline_version = self._pipeline_version(manifest)
                stage_ids = [item[0] for item in stage_definitions_for(pipeline_version)]
                setting_stage = V2_SETTING_STAGE if pipeline_version == "v2" else SETTING_STAGE
                affected = [setting_stage[key] for key in changed if key in setting_stage]
                if affected:
                    first_stage = min(affected, key=stage_ids.index)
                    self._invalidate_from(manifest, first_stage)
                    manifest["message"] = f"設定変更により{manifest['stages'][first_stage]['label']}以降を無効化しました。"
            manifest["request_key"] = _request_key(
                manifest.get("source", ""),
                bool(manifest.get("is_local")),
                manifest["settings"],
                self._pipeline_version(manifest),
            )
            self._save(manifest)
            return manifest

    def get(self, job_id: str) -> dict:
        with self._lock:
            manifest = self._load(job_id)
            active = self._active.get(job_id)
            if manifest["status"] in {"running", "cancel_requested"} and not (
                active and active["thread"].is_alive()
            ):
                stage_id = manifest.get("current_stage")
                if stage_id:
                    manifest["stages"][stage_id]["status"] = "canceled"
                manifest["status"] = "canceled"
                manifest["message"] = "前回のアプリ終了で中断されました。未完了段階から再開できます。"
                manifest["eta_seconds"] = None
                self._save(manifest)
            manifest["can_stop"] = bool(active and active["thread"].is_alive())
            manifest["can_resume"] = manifest["status"] in {"canceled", "failed", "interrupted"}
            return manifest

    def list_jobs(self) -> list[dict]:
        jobs = []
        for run_dir in sorted(self.runs_dir.glob("*"), reverse=True):
            if not run_dir.is_dir():
                continue
            try:
                manifest = self.get(run_dir.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            jobs.append(manifest)
        return jobs

    def wait(self, job_id: str, timeout: float | None = None) -> dict:
        with self._lock:
            active = self._active.get(job_id)
            thread = active["thread"] if active else None
        if thread:
            thread.join(timeout=timeout)
        return self.get(job_id)

    def prepare_publish(self, job_id: str, **kwargs) -> dict:
        """Prepare publishing explicitly without changing the render stage."""
        from publishing.pipeline import prepare_publish_jobs

        with self._lock:
            manifest = self._load(job_id)
            if self._pipeline_version(manifest) != "v2":
                raise ValueError("publishing preparation is supported only for v2 jobs")
            if manifest.get("status") != "completed":
                raise ValueError("publishing requires a completed render job")
            run_dir = manifest["run_dir"]
        return prepare_publish_jobs(run_dir, **kwargs)

    def publish(self, job_id: str, *, manifest_path=None, **kwargs) -> dict:
        """Execute an explicit publish action; never runs automatically after render."""
        from publishing.pipeline import publish_prepared_jobs

        with self._lock:
            manifest = self._load(job_id)
            if self._pipeline_version(manifest) != "v2":
                raise ValueError("publishing is supported only for v2 jobs")
            if manifest_path is None:
                manifest_path = Path(manifest["run_dir"]) / "publishing" / "publish_manifest.v1.json"
        return publish_prepared_jobs(manifest_path, **kwargs)


JOB_MANAGER = JobManager()
