"""New project upload and background pipeline execution for the WebUI."""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from project_settings import load_project_settings_presets
from processing_device import load_processing_device


STATUS_FILENAME = "project_creation.json"
ALLOWED_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_STATUS_LOCK = threading.Lock()


def _safe_part(value: str, fallback: str, limit: int = 40) -> str:
    value = unquote(str(value or "")).strip()
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-_")
    return value[:limit] or fallback


def _safe_filename(value: str) -> str:
    name = Path(unquote(str(value or "source.mp4"))).name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("Supported video formats: mp4, mov, mkv, webm, avi, m4v.")
    stem = _safe_part(Path(name).stem, "source", 64)
    return f"{stem}{suffix}"


def _atomic_write(path: Path, data: dict):
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(serialized, encoding="utf-8")
    for attempt in range(12):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 11:
                break
            time.sleep(0.05 * (attempt + 1))
    # Windows may briefly deny replacing a file that the status endpoint just read.
    # Keep the job alive; the next update will restore atomic replacement.
    path.write_text(serialized, encoding="utf-8")
    temporary.unlink(missing_ok=True)


def save_creation_status(run_directory: Path, update: dict):
    with _STATUS_LOCK:
        path = run_directory / STATUS_FILENAME
        current = load_creation_status(run_directory) or {}
        current.update(update)
        current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _atomic_write(path, current)


def load_creation_status(run_directory: Path) -> dict | None:
    path = run_directory / STATUS_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "failed", "error": "Unable to read creation status."}
    return data if isinstance(data, dict) else {"state": "failed", "error": "Invalid creation status."}


def create_project_run(
    runs_root: Path,
    *,
    filename: str,
    project_name: str,
    preset_id: str,
    use_llm: bool,
) -> tuple[str, Path, Path, dict]:
    safe_filename = _safe_filename(filename)
    name = _safe_part(project_name, "project")
    presets = load_project_settings_presets()
    if preset_id not in presets:
        raise ValueError("The selected project preset was not found.")
    settings = dict(presets[preset_id])
    settings["project_name"] = name
    settings["use_llm_captions"] = bool(use_llm)
    settings["processing_device"] = load_processing_device()
    runs_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = _safe_part(name, "project", 28)
    run_id = f"{stamp}_{prefix}"
    run_directory = runs_root / run_id
    suffix = 1
    while run_directory.exists():
        run_id = f"{stamp}_{prefix}_{suffix:02d}"
        run_directory = runs_root / run_id
        suffix += 1
    run_directory.mkdir(parents=True)
    source_path = run_directory / safe_filename
    save_creation_status(run_directory, {
        "run_id": run_id,
        "state": "uploading",
        "stage": "upload",
        "project_name": name,
        "source_name": safe_filename,
        "preset_id": preset_id,
        "message": "Saving video…",
    })
    return run_id, run_directory, source_path, settings


def run_project_pipeline(run_directory: Path, source_path: Path, settings: dict):
    try:
        save_creation_status(run_directory, {"state": "preparing", "stage": "acquire", "message": "Checking video…"})

        def progress(stage: str, fraction: float, message: str):
            save_creation_status(run_directory, {
                "state": "preparing",
                "stage": stage,
                "progress": round(float(fraction), 4),
                "message": message,
            })

        from pipeline import run_pipeline

        result = run_pipeline(
            str(source_path),
            is_local_file=True,
            existing_run_dir=run_directory,
            pipeline_version="legacy",
            output_width=int(settings["output_width"]),
            output_height=int(settings["output_height"]),
            output_fps=int(settings["output_fps"]),
            max_chars=int(settings["max_chars"]),
            max_lines=int(settings.get("max_lines", 2)),
            use_llm_captions=bool(settings["use_llm_captions"]),
            caption_model=str(settings["caption_model"]),
            default_template_id=str(settings["default_template_id"]),
            video_encoder=str(settings["video_encoder"]),
            processing_device=str(settings.get("processing_device", "auto")),
            prepare_only=True,
            progress_callback=progress,
        )
        save_creation_status(run_directory, {
            "state": "ready",
            "stage": "complete",
            "progress": 1.0,
            "message": "New project is ready to edit.",
            "caption_project_path": result.get("caption_project_path"),
        })
    except Exception as error:
        save_creation_status(run_directory, {
            "state": "failed",
            "stage": "failed",
            "message": "Project creation failed.",
            "error": str(error)[-2000:],
        })
