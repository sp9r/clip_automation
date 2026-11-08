"""Timeline Editor用ローカルHTTP API。既存pipelineを変更せずv2 projectだけを扱う。"""

import json
import os
import re
from urllib.parse import quote
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi import BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from caption_commands import CaptionCommandService
from timeline_project import load_timeline_project, validate_timeline_project, validate_transcript_words
import subtitle_project
from subtitle_project import delete_user_template, load_templates, save_user_template, validate_template, write_ass_subtitles
from timeline_preparation import TimelinePreparationService
from project_creation import (
    create_project_run,
    load_creation_status,
    run_project_pipeline,
    save_creation_status,
)
from project_settings import load_project_settings_presets


class CommandRequest(BaseModel):
    command_id: str
    type: str
    base_revision: int
    caption_id: str | None = None
    display_text: str | None = None
    delta: float | None = None
    left_display_text: str | None = None
    right_display_text: str | None = None
    left_id: str | None = None
    right_id: str | None = None
    boundary_word_id: int | None = None
    after_word_id: int | None = None
    caption_ids: list[str] | None = None
    template_id: str | None = None


class RenderRequest(BaseModel):
    start: float
    end: float


class EditRegionsRequest(BaseModel):
    regions: list[dict]


MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
SOURCE_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# Keep the editor/API contract extensible.  subtitle_project.validate_template
# intentionally normalizes the fields understood by the ASS renderer, so the
# API preserves additional Inspector fields alongside that canonical payload.
_TEMPLATE_CANONICAL_KEYS = {
    "id", "name", "font_name", "font_size", "primary_color", "outline_color",
    "bold", "italic", "outline", "shadow", "shadow_color", "background_color",
    "background_opacity", "border_style", "underline", "strikeout", "scale_x",
    "scale_y", "letter_spacing", "angle", "alignment", "margin_v", "position_x",
    "position_y",
}
_TEMPLATE_ALIASES = {
    "font_family": "font_name",
    "fontColor": "primary_color",
    "color": "primary_color",
    "outline_width": "outline",
    "shadow_blur": "shadow",
}


def _normalize_template_payload(raw: dict) -> tuple[dict, dict]:
    if not isinstance(raw, dict):
        raise TypeError("Subtitle template payload must be an object.")
    payload = dict(raw)
    for alias, canonical in _TEMPLATE_ALIASES.items():
        if canonical not in payload and alias in payload:
            value = payload[alias]
            if alias == "position" and isinstance(value, str):
                value = {"left": 1, "center": 2, "right": 3, "top-left": 7, "top-center": 8, "top-right": 9}.get(value, value)
            payload[canonical] = value
    if "position" in payload:
        position = payload["position"]
        if isinstance(position, dict):
            if "position_x" not in payload and position.get("x") is not None:
                payload["position_x"] = position["x"]
            if "position_y" not in payload and position.get("y") is not None:
                payload["position_y"] = position["y"]
        elif isinstance(position, str):
            payload.setdefault(
                "alignment",
                {"left": 1, "center": 2, "right": 3, "top-left": 7, "top-center": 8, "top-right": 9}.get(position, position),
            )
    normalized = validate_template(payload)
    extras = {key: value for key, value in raw.items() if key not in _TEMPLATE_CANONICAL_KEYS}
    return normalized, extras


def _template_store_path() -> Path:
    return Path(subtitle_project.DEFAULT_USER_TEMPLATE_PATH)


def _raw_template_store() -> dict:
    path = _template_store_path()
    if not path.is_file() or path.stat().st_size == 0:
        return {"schema_version": 1, "templates": []}
    data = _json(path)
    if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
        raise ValueError("Subtitle template store must contain a templates array.")
    return data


def _merge_stored_template_extras(templates: list[dict]) -> list[dict]:
    raw_templates = _raw_template_store()["templates"]
    extras_by_id = {
        str(item.get("id")): {key: value for key, value in item.items() if key not in _TEMPLATE_CANONICAL_KEYS}
        for item in raw_templates
        if isinstance(item, dict) and item.get("id")
    }
    return [dict(template, **extras_by_id.get(str(template.get("id")), {})) for template in templates]


def _persist_template_extras(template_id: str, extras: dict) -> None:
    if not extras:
        return
    path = _template_store_path()
    data = _raw_template_store()
    for item in data["templates"]:
        if isinstance(item, dict) and str(item.get("id")) == str(template_id):
            item.update(extras)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp_path, path)
            return


from fastapi.staticfiles import StaticFiles

class ExportOutput(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 60

class ExportRequest(BaseModel):
    revision: int
    output: ExportOutput = ExportOutput()
    encoder: str = "auto"
    filename: str = "clip_automation_export.mp4"

def create_app(runs_dir: str | Path | None = None) -> FastAPI:
    root = Path(runs_dir or Path(__file__).parent / "data" / "runs").resolve()
    app = FastAPI(title="Clip Automation Timeline Editor", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8766", "http://localhost:8766"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-File-Name", "X-Project-Name", "X-Preset-Id", "X-Use-Llm"],
    )

    def run_dir(run_id: str) -> Path:
        target = (root / run_id).resolve()
        if target.parent != root or not target.is_dir():
            raise HTTPException(404, "runが見つかりません。")
        return target

    def context(run_id: str):
        directory = run_dir(run_id)
        project_path = directory / "caption_project.v2.json"
        words_path = directory / "transcript_words.json"
        if not project_path.is_file() or not words_path.is_file():
            raise HTTPException(409, "Timeline v2 projectまたはword正本がありません。")
        project = load_timeline_project(project_path)
        words = _json(words_path)
        issues = validate_transcript_words(words) + validate_timeline_project(project, words)
        if issues:
            raise HTTPException(422, {"message": "project検証に失敗しました。", "issues": issues})
        return directory, project_path, words, project

    def source_media_name(directory: Path, project: dict) -> str | None:
        raw_source = project.get("source_path")
        if raw_source:
            source = Path(str(raw_source))
            try:
                resolved = source.resolve()
                if (
                    resolved.parent == directory.resolve()
                    and resolved.is_file()
                    and resolved.suffix.lower() in SOURCE_VIDEO_SUFFIXES
                    and not resolved.name.startswith(("final_", "preview_", "editor_", "export_"))
                ):
                    return resolved.name
            except OSError:
                pass
        candidates = [
            item for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in SOURCE_VIDEO_SUFFIXES
            and not item.name.startswith(("final_", "preview_", "editor_", "export_"))
        ]
        return max(candidates, key=lambda item: item.stat().st_size).name if candidates else None

    def media_url(run_id: str, filename: str) -> str:
        return f"/api/runs/{run_id}/media/{quote(filename, safe='')}"

    @app.get("/api/health")
    def health():
        return {"ok": True, "runs_dir": str(root)}

    @app.get("/api/runs")
    def runs():
        if not root.is_dir():
            return []
        result = []
        for directory in sorted(root.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            report_path = directory / "migration_report.json"
            report = _json(report_path) if report_path.is_file() else None
            creation = load_creation_status(directory) or {}
            ready_files = (directory / "caption_project.v2.json").is_file() and (directory / "transcript_words.json").is_file()
            if creation.get("state") in {"uploading", "queued", "preparing", "failed"}:
                state = creation["state"]
            else:
                state = "ready" if ready_files and (not report or report.get("state") == "ready") else (report or {}).get("state", "not_ready")
            source_ready = False
            if ready_files:
                try:
                    source_ready = source_media_name(directory, _json(directory / "caption_project.v2.json")) is not None
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    source_ready = False
            result.append({
                "run_id": directory.name,
                "state": state,
                "timeline_ready": state == "ready",
                "source_ready": source_ready,
                "proxy_ready": (directory / "preview_proxy.mp4").is_file(),
            })
        return result

    @app.get("/api/project-presets")
    def project_presets():
        return list(load_project_settings_presets().values())

    @app.get("/api/subtitle-templates")
    def subtitle_templates():
        return _merge_stored_template_extras(list(load_templates().values()))

    @app.post("/api/subtitle-templates")
    async def save_subtitle_template(request: Request):
        try:
            raw = await request.json()
            template, extras = _normalize_template_payload(raw)
            # Updating a template with only canonical fields must not erase
            # previously saved Inspector-only fields.
            previous_extras = {}
            for item in _raw_template_store()["templates"]:
                if not isinstance(item, dict):
                    continue
                if (raw.get("id") and str(item.get("id")) == str(raw.get("id"))) or (
                    raw.get("name") and item.get("name") == raw.get("name")
                ):
                    previous_extras = {key: value for key, value in item.items() if key not in _TEMPLATE_CANONICAL_KEYS}
                    break
            extras = dict(previous_extras, **extras)
            saved = save_user_template(template)
            # save_user_template keeps the ASS-compatible canonical schema;
            # restore Inspector-only fields after it has assigned the final id.
            _persist_template_extras(saved["id"], extras)
            return dict(saved, **extras)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.delete("/api/subtitle-templates/{template_id}")
    def delete_subtitle_template(template_id: str):
        try:
            if not delete_user_template(template_id):
                raise HTTPException(404, "Subtitle template not found.")
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"deleted": template_id}

    @app.post("/api/projects")
    async def create_project(request: Request, background_tasks: BackgroundTasks):
        filename = request.headers.get("x-file-name", "source.mp4")
        project_name = request.headers.get("x-project-name", "New Project")
        preset_id = request.headers.get("x-preset-id", "fhd_landscape")
        use_llm = request.headers.get("x-use-llm", "true").lower() != "false"
        try:
            run_id, run_directory, source_path, settings = create_project_run(
                root,
                filename=filename,
                project_name=project_name,
                preset_id=preset_id,
                use_llm=use_llm,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

        size = 0
        try:
            with source_path.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("Video files must be 8GB or smaller.")
                    output.write(chunk)
        except ValueError as error:
            source_path.unlink(missing_ok=True)
            save_creation_status(run_directory, {"state": "failed", "error": str(error)})
            raise HTTPException(413, str(error)) from error
        except OSError as error:
            source_path.unlink(missing_ok=True)
            save_creation_status(run_directory, {"state": "failed", "error": "Failed to save the video."})
            raise HTTPException(500, "Failed to save the video.") from error

        if size == 0:
            source_path.unlink(missing_ok=True)
            save_creation_status(run_directory, {"state": "failed", "error": "The video file is empty."})
            raise HTTPException(422, "The video file is empty.")

        save_creation_status(run_directory, {"state": "queued", "bytes": size, "stage": "upload", "message": "Video received."})
        background_tasks.add_task(run_project_pipeline, run_directory, source_path, settings)
        return {"run_id": run_id, "state": "queued", "bytes": size}

    @app.get("/api/runs/{run_id}/creation-status")
    def creation_status(run_id: str):
        directory = run_dir(run_id)
        status = load_creation_status(directory)
        if status is None:
            raise HTTPException(404, "Creation status not found.")
        return status

    @app.post("/api/runs/{run_id}/prepare-timeline")
    def prepare_timeline(run_id: str):
        try:
            report = TimelinePreparationService(run_dir(run_id)).prepare()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(422, {"code": "TIMELINE_PREPARATION_FAILED", "message": str(error)}) from error
        return report

    @app.get("/api/runs/{run_id}/migration-report")
    def migration_report(run_id: str):
        path = run_dir(run_id) / "migration_report.json"
        if not path.is_file():
            raise HTTPException(404, "migration reportがありません。")
        return _json(path)

    @app.get("/api/projects/{run_id}")
    def project(run_id: str):
        directory, _, words, data = context(run_id)
        source_media = source_media_name(directory, data)
        rendered_media = next((name for name in ("final_jumpcut_video.mp4", "preview_proxy.mp4", "editor_exported.mp4") if (directory / name).is_file()), None)
        highlights_path = directory / "highlights.json"
        if highlights_path.is_file():
            highlights = _json(highlights_path)
        elif (directory / "highlight.json").is_file():
            highlights = [_json(directory / "highlight.json")]
        else:
            highlights = []
        regions_path = directory / "edit_regions.json"
        regions = _json(regions_path) if regions_path.is_file() else []
        return {
            "project": data,
            "words": words,
            "word_count": len(words),
            "media_url": media_url(run_id, source_media) if source_media else None,
            "source_media_url": media_url(run_id, source_media) if source_media else None,
            "rendered_media_url": media_url(run_id, rendered_media) if rendered_media else None,
            "highlights": highlights,
            "edit_regions": regions,
        }

    @app.post("/api/projects/{run_id}/edit-regions")
    def edit_regions(run_id: str, request: EditRegionsRequest):
        directory = run_dir(run_id)
        regions = request.regions
        previous_end = 0.0
        for region in regions:
            try:
                start = float(region["start"])
                end = float(region["end"])
            except (KeyError, TypeError, ValueError) as error:
                raise HTTPException(422, "Invalid edit region.") from error
            if region.get("mode") not in {"keep", "delete"} or start < previous_end - 0.001 or end <= start:
                raise HTTPException(422, "Edit regions must be ordered, non-overlapping keep/delete ranges.")
            previous_end = end
        path = directory / "edit_regions.json"
        path.write_text(json.dumps(regions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"regions": regions}

    @app.get("/api/projects/{run_id}/timeline")
    def timeline(run_id: str, start: float = Query(0, ge=0), end: float = Query(60, gt=0)):
        _, _, words, data = context(run_id)
        if end <= start:
            raise HTTPException(400, "endはstartより後にしてください。")
        captions = [c for c in data["captions"] if c["end"] >= start and c["start"] <= end]
        visible_words = [w for w in words if w["timeline_end"] >= start and w["timeline_start"] <= end]
        return {"revision": data["revision"], "start": start, "end": end, "captions": captions, "words": visible_words}

    @app.post("/api/projects/{run_id}/commands")
    def command(run_id: str, request: CommandRequest):
        _, path, words, _ = context(run_id)
        try:
            payload = request.model_dump(exclude_none=True) if hasattr(request, "model_dump") else request.dict(exclude_none=True)
            return CaptionCommandService(path, words).execute(payload)
        except ValueError as error:
            status = 409 if "revision競合" in str(error) else 422
            raise HTTPException(status, str(error)) from error

    @app.post("/api/projects/{run_id}/undo")
    def undo(run_id: str):
        _, path, words, _ = context(run_id)
        try:
            return CaptionCommandService(path, words).undo()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/projects/{run_id}/redo")
    def redo(run_id: str):
        _, path, words, _ = context(run_id)
        try:
            return CaptionCommandService(path, words).redo()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/projects/{run_id}/preview-renders")
    @app.post("/api/projects/{run_id}/render-preview")
    def render_preview(run_id: str, request: RenderRequest):
        directory, _, _, data = context(run_id)
        # Allow a tiny representation error when JSON floats such as
        # 34.06 - 4.06 are evaluated in Python as 30.000000000000004.
        if request.start < 0 or request.end <= request.start or request.end - request.start > 30.000001:
            raise HTTPException(422, "preview renderは0秒以上、最大30秒の範囲で指定してください。")
        media_name = source_media_name(directory, data)
        if not media_name:
            raise HTTPException(409, "未レンダーのソース動画がありません。")
        return {
            "revision": data["revision"],
            "media_url": media_url(run_id, media_name),
            "source_media_url": media_url(run_id, media_name),
            "start": request.start,
            "end": request.end,
            "rendered": False,
        }

    @app.post("/api/projects/{run_id}/exports")
    def export_video(run_id: str, request: ExportRequest):
        directory, _, _, data = context(run_id)
        if request.revision != data["revision"]:
            raise HTTPException(409, f"書き出し開始revision（{request.revision}）が現在（{data['revision']}）と一致しません。")
        # Preview is always the raw source.  Export must use the same input;
        # an old proxy/final render would make the result differ from what the
        # editor is showing and would reintroduce the pre-encode dependency.
        media_name = source_media_name(directory, data)
        if not media_name:
            raise HTTPException(409, "書き出し元の動画が見つかりません。")

        export_project = dict(data)
        export_project["captions"] = [
            {"id": caption["id"], "start": caption["start"], "end": caption["end"], "text": caption["display_text"], "template_id": caption.get("template_id", "")}
            for caption in data["captions"]
            if not caption.get("deleted")
        ]
        requested_name = Path(request.filename or "clip_automation_export.mp4").name
        if not requested_name.lower().endswith(".mp4"):
            requested_name += ".mp4"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(requested_name).stem).strip("._") or "clip_automation_export"
        output_name = f"export_{stem}.mp4"
        output = directory / output_name
        ass_path = directory / f"{output_name}.ass"
        partial = directory / f"{output_name}.partial.mp4"
        write_ass_subtitles(export_project, ass_path)
        escaped = str(ass_path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")

        command = [
            "ffmpeg", "-y", "-i", str(directory / media_name),
            "-vf", f"subtitles='{escaped}'",
            "-c:v", "libx264", "-preset", "medium", "-c:a", "aac", "-movflags", "+faststart", str(partial),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
        if completed.returncode != 0 or not partial.is_file():
            partial.unlink(missing_ok=True)
            raise HTTPException(500, {"message": "正式書き出しに失敗しました。", "stderr": completed.stderr[-2000:]})
        os.replace(partial, output)
        return {
            "status": "success",
            "revision": request.revision,
            "media_url": f"/api/runs/{run_id}/media/{output_name}",
            "download_url": f"/api/runs/{run_id}/download/{output_name}?name={quote(requested_name, safe='')}",
            "filename": requested_name,
        }

    @app.get("/api/runs/{run_id}/media/{filename}")
    def media(run_id: str, filename: str):
        safe_name = Path(filename).name
        if safe_name != filename or (filename not in {"preview_proxy.mp4", "final_jumpcut_video.mp4", "editor_preview.mp4", "editor_exported.mp4"} and not filename.startswith("export_") and Path(filename).suffix.lower() not in SOURCE_VIDEO_SUFFIXES):
            raise HTTPException(403, "配信を許可されていないファイルです。")
        target = run_dir(run_id) / safe_name
        if not target.is_file():
            raise HTTPException(404, "動画が見つかりません。")
        return FileResponse(target, media_type="video/mp4")

    @app.get("/api/runs/{run_id}/download/{filename}")
    def download(run_id: str, filename: str, name: str = ""):
        if not filename.startswith("export_") or not filename.endswith(".mp4"):
            raise HTTPException(403, "ダウンロードを許可されていないファイルです。")
        target = run_dir(run_id) / filename
        if not target.is_file():
            raise HTTPException(404, "書き出し動画が見つかりません。")
        download_name = Path(name).name if name else filename
        if not download_name.lower().endswith(".mp4"):
            download_name += ".mp4"
        return FileResponse(target, media_type="video/mp4", filename=download_name)

    dist_dir = Path(__file__).parent / "editor_frontend" / "dist"
    if dist_dir.is_dir():
        @app.get("/", include_in_schema=False)
        def read_index():
            return FileResponse(dist_dir / "index.html")

        if (dist_dir / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=str(dist_dir / "assets")), name="assets")

    return app




app = create_app()

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("CLIP_AUTOMATION_HOST", "127.0.0.1")
    port = int(os.environ.get("CLIP_AUTOMATION_PORT", "8766"))
    uvicorn.run("editor_api:app", host=host, port=port, reload=False)
