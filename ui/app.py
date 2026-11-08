import html
import json
import os
import sys
from pathlib import Path

import gradio as gr


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feedback import record_feedback
from diagnostics import get_acceleration_status, run_preflight
from job_manager import JOB_MANAGER
from ollama_service import check_ollama, start_ollama
from pipeline import PROJECT_ROOT, STAGE_DEFINITIONS, stage_definitions_for, video_is_valid
from preview import (
    PREVIEW_CSS_TEMPLATE,
    PREVIEW_HTML_TEMPLATE,
    PREVIEW_JS_ON_LOAD,
    get_preview_payload,
    save_preview_caption,
)
from project_settings import (
    delete_project_settings_preset,
    load_project_settings_presets,
    save_project_settings_preset,
)
from processing_device import load_processing_device, save_processing_device
from subtitle_project import (
    delete_user_template,
    load_caption_project,
    load_templates,
    project_to_rows,
    realign_caption_project_texts,
    save_caption_project,
    save_user_template,
    update_project_from_rows,
    write_ass_subtitles,
)
from render_manifest import load_manifest as load_render_manifest
from publishing.pipeline import publish_prepared_jobs, refresh_processing_status
from ui_helpers import (
    RESOLUTION_CHOICES,
    apply_resolution_preset,
    build_template_preview,
    detect_resolution_preset,
    installed_font_names,
)
from ui_presenter import format_health, format_user_error


RUNS_DIR = (PROJECT_ROOT / "data" / "runs").resolve()
CAPTION_HEADERS = ["ID", "開始秒", "終了秒", "字幕本文", "テンプレートID"]
STAGE_HEADERS = ["段階", "状態", "進捗", "メッセージ"]
ALIGNMENT_CHOICES = [
    ("左下", 1),
    ("中央下", 2),
    ("右下", 3),
    ("左中央", 4),
    ("中央", 5),
    ("右中央", 6),
    ("左上", 7),
    ("中央上", 8),
    ("右上", 9),
]
STATUS_LABELS = {
    "pending": "待機",
    "running": "実行中",
    "cancel_requested": "停止処理中",
    "canceled": "停止済み",
    "failed": "失敗",
    "interrupted": "中断・再開可能",
    "completed": "完了",
}


def _template_choices(include_default=False):
    choices = [("デフォルトを使用", "")] if include_default else []
    for template_id, template in load_templates().items():
        choices.append((f"{template['name']} [{template_id}]", template_id))
    return choices


def _project_settings_choices():
    choices = []
    for preset_id, preset in load_project_settings_presets().items():
        choices.append((f"{preset['name']} [{preset_id}]", preset_id))
    return choices


def _format_seconds(value):
    if value is None:
        return "--"
    value = max(0, int(float(value)))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_eta(manifest):
    value = manifest.get("eta_seconds")
    if value is not None:
        return _format_seconds(value)
    if manifest.get("status") in {"running", "cancel_requested"}:
        return "計測中"
    return "--"


def _progress_card(manifest: dict | None) -> str:
    if not manifest:
        return "<div class='job-card'><b>ジョブ未選択</b><p>新規処理または既存runを選択してください。</p></div>"
    overall = float(manifest.get("overall_progress", 0))
    stage = manifest.get("current_stage")
    stage_label = manifest.get("stages", {}).get(stage, {}).get("label", "待機")
    status = STATUS_LABELS.get(manifest.get("status"), manifest.get("status", "不明"))
    message = html.escape(str(manifest.get("message", "")))
    error_html = ""
    if manifest.get("error"):
        error_text = html.escape(format_user_error(manifest["error"])).replace("\n", "<br>")
        error_html = f"<div class='job-error'>{error_text}</div>"
    return f"""
    <div class="job-card">
      <div class="job-card-head"><b>{html.escape(manifest['job_id'])}</b><span>{status}</span></div>
      <div class="job-stage">{manifest.get('stage_number', 0)}/{manifest.get('stage_count', 6)} {html.escape(stage_label)} — 段階内 {float(manifest.get('stage_progress', 0)):.1f}%</div>
      <div class="job-progress"><div style="width:{overall:.2f}%"></div></div>
      <div class="job-overall">全体 {overall:.1f}%</div>
      <div class="job-times">経過 {_format_seconds(manifest.get('elapsed_seconds'))} ／ 残り目安 {_format_eta(manifest)}</div>
      <p>{message}</p>{error_html}
    </div>
    """


def _stage_rows(manifest: dict | None) -> list[list]:
    if not manifest:
        return []
    rows = []
    definitions = stage_definitions_for(manifest.get("pipeline_version", "legacy"))
    for index, (stage_id, label, _) in enumerate(definitions, start=1):
        stage = manifest["stages"][stage_id]
        rows.append(
            [
                f"{index}/{len(definitions)} {label}",
                STATUS_LABELS.get(stage["status"], stage["status"]),
                f"{float(stage['progress']) * 100:.1f}%",
                stage.get("message", ""),
            ]
        )
    return rows


def _job_choices():
    choices = []
    for run_dir in sorted(JOB_MANAGER.runs_dir.glob("*"), reverse=True):
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / "job_manifest.json"
        try:
            item = json.loads(manifest_path.read_text(encoding="utf-8"))
            label = (
                f"{run_dir.name} | {STATUS_LABELS.get(item.get('status'), item.get('status'))} | "
                f"{float(item.get('overall_progress', 0.0)):.1f}%"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            label = f"{run_dir.name} | 未登録（選択時に検査）"
        choices.append((label, run_dir.name))
    return choices


def _settings(width, height, fps, max_chars, max_lines, segmentation_mode, caption_model, default_template_id, encoder):
    processing_device = load_processing_device()
    return {
        "output_width": int(width),
        "output_height": int(height),
        "output_fps": int(fps),
        "max_chars": int(max_chars),
        "max_lines": int(max_lines),
        "use_llm_captions": segmentation_mode == "llm",
        "caption_model": str(caption_model),
        "default_template_id": str(default_template_id),
        # The unified processing policy is user-scoped. Keep the legacy field
        # for old manifests, but do not let it become the new UI authority.
        "video_encoder": "auto",
        "processing_device": processing_device,
    }


def _extract_file_path(source):
    if not source:
        return ""
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        if "video" in source and isinstance(source["video"], dict):
            return source["video"].get("path") or source["video"].get("name") or ""
        return source.get("path") or source.get("name") or ""
    if hasattr(source, "name"):
        return getattr(source, "name")
    if hasattr(source, "path"):
        return getattr(source, "path")
    return str(source)


def _start_job(source, is_local, width, height, fps, max_chars, max_lines, segmentation_mode, caption_model, default_template_id, encoder):
    clean_source = _extract_file_path(source)
    if not clean_source:
        gr.Warning("動画またはURLを指定してください。")
        payload = get_preview_payload("")
        return None, gr.update(), _progress_card(None), "動画またはURLを指定してください。", payload, payload
    try:
        report = run_preflight(
            clean_source,
            is_local=is_local,
            require_ollama=segmentation_mode == "llm",
            required_model=str(caption_model),
        )
        if not report["ok"]:
            payload = get_preview_payload("")
            return None, gr.update(), _progress_card(None), format_user_error(report["errors"][0]), payload, payload
        clean_source = report["source"]
        manifest = JOB_MANAGER.create_job(
            clean_source,
            is_local=is_local,
            settings=_settings(
                width,
                height,
                fps,
                max_chars,
                max_lines,
                segmentation_mode,
                caption_model,
                default_template_id,
                encoder,
            ),
        )

        payload = get_preview_payload(manifest["job_id"])
        
        if manifest.get("request_reused"):
            gr.Info("同じ入力・設定のジョブが処理中のため、既存ジョブへ統合しました。")
            start_message = "同じ入力・設定のジョブがすでに処理中のため、新しいrunを増やさず既存ジョブを表示しました。"
        else:
            gr.Info("バックグラウンド処理を開始しました。")
            start_message = "バックグラウンド処理を開始しました。画面を閉じてもrun状態は保存されます。"
            
        return (
            manifest["job_id"],
            gr.update(choices=_job_choices(), value=manifest["job_id"]),
            _progress_card(manifest),
            start_message,
            payload,
            payload,
        )
    except Exception as exc:
        payload = get_preview_payload("")
        return None, gr.update(), _progress_card(None), f"開始失敗: {exc}", payload, payload


def refresh_system_health(required_model):
    state = check_ollama()
    choices = state.get("models") or [required_model or "gemma3:latest"]
    value = required_model if required_model in choices else choices[0]
    return format_health(state, required_model), gr.update(choices=choices, value=value)


def _format_acceleration_status(status: dict) -> str:
    caption = str(status.get("caption_ai", "unavailable")).upper()
    caption_label = {
        "GPU": "GPU",
        "MIXED": "mixed",
        "CPU": "CPU",
        "UNAVAILABLE": "利用不可",
    }.get(caption, caption)
    lines = [
        f"**処理デバイス:** `{status.get('processing_device', 'auto')}`",
        f"- GPU: **{'利用可能' if status.get('gpu_available') else '利用不可'}**",
        f"- Whisper: **{status.get('whisper', 'CPU')}**",
        f"- 字幕AI: **{caption_label}**",
        f"- Render: **{status.get('render', 'CPU')}**",
    ]
    if status.get("errors"):
        lines.append("\n⚠️ " + " / ".join(str(item) for item in status["errors"]))
    return "\n".join(lines)


def refresh_acceleration_status(processing_device=None, caption_model=None):
    try:
        status = get_acceleration_status(processing_device or load_processing_device(), caption_model or "qwen2.5:14b")
        return _format_acceleration_status(status)
    except Exception:
        return "処理デバイス診断を取得できませんでした。再確認してください。"


def save_processing_device_from_ui(processing_device, caption_model):
    try:
        saved = save_processing_device(str(processing_device))
        return refresh_acceleration_status(saved, caption_model)
    except Exception as exc:
        return f"処理デバイス設定の保存に失敗しました: {exc}"


def launch_ollama_from_ui(required_model):
    return format_health(start_ollama(), required_model)


def process_youtube(url, *settings):
    return _start_job(url, False, *settings)


def process_local_file(path_input, file_upload, *settings):
    source = path_input or file_upload
    return _start_job(source, True, *settings)


def prepare_publish_ui(job_id, title, description, tags, privacy_status):
    if not job_id:
        return "", {"error": "投稿準備するrunを選択してください。"}, "投稿準備するrunを選択してください。"
    try:
        metadata = {
            "title": title or None,
            "description": description or "",
            "tags": [item.strip() for item in (tags or "").split(",") if item.strip()],
            "privacy_status": privacy_status or "private",
            "category_id": "22",
        }
        # None title means deterministic per-clip clip_id title is used.
        metadata_by_clip = {} if not metadata["title"] else {"default": metadata}
        result = JOB_MANAGER.prepare_publish(job_id, metadata_by_clip=metadata_by_clip)
        path = result.get("manifest_path", "")
        return path, result, f"投稿準備完了: {path}"
    except Exception as exc:
        return "", {"error": str(exc)}, f"投稿準備失敗: {exc}"


def _publish_manifest_path(value):
    if isinstance(value, dict):
        return value.get("manifest_path")
    return value or ""


def dry_run_publish_ui(manifest_value):
    path = _publish_manifest_path(manifest_value)
    if not path:
        return {"error": "先に投稿準備を実行してください。"}, "dry-run対象のmanifestがありません。"
    try:
        result = publish_prepared_jobs(path, dry_run=True)
        return result, "dry-run完了。remote mutationは0件です。"
    except Exception as exc:
        return {"error": str(exc)}, f"dry-run失敗: {exc}"


def publish_youtube_ui(job_id, manifest_value):
    path = _publish_manifest_path(manifest_value)
    if not job_id or not path:
        return {"error": "runとpublish manifestを指定してください。"}, "YouTube投稿の対象がありません。"
    try:
        result = JOB_MANAGER.publish(job_id, manifest_path=path)
        return result, "YouTube投稿処理が完了しました。"
    except Exception as exc:
        return {"error": str(exc)}, f"YouTube投稿失敗: {exc}"


def refresh_publish_status_ui(manifest_value):
    path = _publish_manifest_path(manifest_value)
    if not path:
        return {"error": "publish manifestを指定してください。"}, "status対象のmanifestがありません。"
    try:
        result = refresh_processing_status(path)
        return result, "processing statusを更新しました。"
    except Exception as exc:
        return {"error": str(exc)}, f"status更新失敗: {exc}"



def refresh_job_list(current_job):
    choices = _job_choices()
    values = [value for _, value in choices]
    value = current_job if current_job in values else (values[0] if values else None)
    return gr.update(choices=choices, value=value)


def select_existing_job(job_id):
    if not job_id:
        payload = get_preview_payload("")
        return None, _progress_card(None), "runを選択してください。", payload, payload
    try:
        manifest = JOB_MANAGER.get(job_id)
        payload = get_preview_payload(job_id)
        return job_id, _progress_card(manifest), manifest["message"], payload, payload
    except Exception as exc:
        payload = get_preview_payload("")
        return None, _progress_card(None), f"読込失敗: {exc}", payload, payload


def stop_job(job_id):
    if not job_id:
        return _progress_card(None), "停止するrunを選択してください。"
    try:
        manifest = JOB_MANAGER.cancel(job_id)
        return _progress_card(manifest), manifest["message"]
    except Exception as exc:
        return _progress_card(None), f"停止失敗: {exc}"


def resume_job(job_id):
    if not job_id:
        return _progress_card(None), "再開するrunを選択してください。"
    try:
        manifest = JOB_MANAGER.start(job_id, resume=True)
        return _progress_card(manifest), "完成済み段階を検証し、未完了段階から再開しました。"
    except Exception as exc:
        return _progress_card(None), f"再開失敗: {exc}"


def rewind_and_start_job(job_id, stage_id, width, height, fps, max_chars, max_lines, segmentation_mode, caption_model, default_template_id, encoder):
    if not job_id:
        return _progress_card(None), "runを選択してください。"
    if not stage_id:
        return _progress_card(None), "やり直す段階を選択してください。"
    try:
        JOB_MANAGER.update_settings(
            job_id,
            _settings(width, height, fps, max_chars, max_lines, segmentation_mode, caption_model, default_template_id, encoder)
        )
        manifest = JOB_MANAGER.rewind_and_start(job_id, stage_id)
        return _progress_card(manifest), manifest["message"]
    except Exception as exc:
        return _progress_card(None), f"やり直し失敗: {exc}"


def _manifest_results(manifest):
    run_dir = Path(manifest["run_dir"])
    if manifest.get("pipeline_version") == "v2":
        planning_dir = run_dir / "planning"
        render_manifest_path = planning_dir / "render_manifest.v1.json"
        clips = []
        if render_manifest_path.is_file():
            try:
                render_manifest = load_render_manifest(render_manifest_path)
                for entry in render_manifest.get("clips", []):
                    output = entry.get("output", {})
                    edit_project = entry.get("edit_project", {})
                    clips.append(
                        {
                            "clip_id": entry.get("clip_id"),
                            "state": entry.get("state"),
                            "edit_project_path": str((planning_dir / edit_project["path"]).resolve()),
                            "rendered_video_path": str((planning_dir / output["path"]).resolve()),
                            "output_sha256": output.get("sha256"),
                            "error": entry.get("error"),
                        }
                    )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                clips = []
        return {
            "pipeline_version": "v2",
            "run_dir": str(run_dir),
            "planning_dir": str(planning_dir),
            "planning_manifest_path": str(planning_dir / "job_manifest.json"),
            "render_manifest_path": str(render_manifest_path),
            "clips": clips,
            "completed_outputs": [item["rendered_video_path"] for item in clips if item["state"] == "completed"],
            "failed_outputs": [item for item in clips if item["state"] != "completed"],
            "srt_path": str(run_dir / "captions.srt"),
            "caption_project_path": str(run_dir / "caption_project.json"),
            "source_path": manifest.get("source"),
        }
    project_path = run_dir / "caption_project.json"
    if not project_path.is_file():
        return None
    return {
        "run_dir": str(run_dir),
        "final_video_path": str(run_dir / "final_jumpcut_video.mp4"),
        "preview_proxy_path": str(run_dir / "preview_proxy.mp4"),
        "srt_path": str(run_dir / "captions.srt"),
        "ass_path": str(run_dir / "captions.ass"),
        "caption_project_path": str(project_path),
        "highlight_json_path": str(run_dir / "highlight.json"),
        "source_path": manifest.get("source"),
    }


def _highlight_text(path):
    if not path or not Path(path).is_file():
        return "ハイライト情報はまだありません。"
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            highlight = json.load(file)
        return (
            f"【推論結果】\n時間: {highlight.get('start')}s 〜 {highlight.get('end')}s\n"
            f"理由: {highlight.get('reason')}"
        )
    except (OSError, json.JSONDecodeError):
        return "ハイライト情報を読み込めません。"


def poll_job(job_id, previous_preview_payload):
    if not job_id:
        payload = get_preview_payload("")
        return (
            _progress_card(None),
            [],
            "runを選択してください。",
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            None,
            None,
            "ハイライト情報はまだありません。",
            None,
            [],
            payload,
            payload if payload != previous_preview_payload else gr.skip(),
        )
    try:
        manifest = JOB_MANAGER.get(job_id)
        results = _manifest_results(manifest)
        video_path = None
        srt_path = None
        highlight = "ハイライト情報はまだありません。"
        clip_results = []
        if results:
            if results.get("pipeline_version") == "v2":
                clip_results = results.get("clips", [])
                highlight = "Phase 6: 独立クリップの生成結果を表示しています。"
            elif video_is_valid(results["final_video_path"]):
                video_path = results["final_video_path"]
            if results.get("srt_path") and Path(results["srt_path"]).is_file():
                srt_path = results["srt_path"]
            if results.get("pipeline_version") != "v2":
                highlight = _highlight_text(results["highlight_json_path"])
        payload = get_preview_payload(job_id)
        return (
            _progress_card(manifest),
            _stage_rows(manifest),
            manifest["message"],
            gr.update(interactive=manifest.get("can_stop", False)),
            gr.update(interactive=manifest.get("can_resume", False)),
            gr.update(interactive=not manifest.get("can_stop", False)),
            video_path,
            srt_path,
            highlight,
            results,
            clip_results,
            payload,
            payload if payload != previous_preview_payload else gr.skip(),
        )
    except Exception as exc:
        payload = get_preview_payload("")
        return (
            _progress_card(None),
            [],
            f"状態取得失敗: {exc}",
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            None,
            None,
            "ハイライト情報はまだありません。",
            None,
            [],
            payload,
            payload if payload != previous_preview_payload else gr.skip(),
        )


def load_caption_editor(job_id):
    if not job_id:
        payload = get_preview_payload("")
        preview_html = build_template_preview("字幕プレビュー", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)
        return [], "", gr.update(choices=[], value=None), payload, preview_html, "先にrunを選択してください。"
    try:
        manifest = JOB_MANAGER.get(job_id)
        results = _manifest_results(manifest)
        if not results:
            payload = get_preview_payload(job_id)
            preview_html = build_template_preview("字幕プレビュー", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)
            return (
                [],
                "",
                gr.update(choices=[], value=None),
                payload,
                preview_html,
                "字幕プロジェクトの生成を待っています。",
            )
        project = load_caption_project(results["caption_project_path"])
        rows = project_to_rows(project)
        bulk_text = "\n".join(caption["text"] for caption in project["captions"])
        caption_ids = [row[0] for row in rows]
        payload = get_preview_payload(job_id)
        first_id = caption_ids[0] if caption_ids else None
        preview_html = render_caption_editor_preview(job_id, first_id)
        return (
            rows,
            bulk_text,
            gr.update(choices=caption_ids, value=first_id),
            payload,
            preview_html,
            f"字幕{len(rows)}件を読み込み、プレビュー画面へ反映しました。",
        )
    except Exception as exc:
        payload = get_preview_payload("")
        preview_html = build_template_preview("字幕読込失敗", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)
        return [], "", gr.update(choices=[], value=None), payload, preview_html, f"字幕読込失敗: {exc}"



def render_caption_editor_preview(job_id, caption_id):
    """選択された字幕IDのテキスト・装飾・画面解像度に基づいてピクセルパーフェクトなHTMLプレビューを生成する"""
    if not job_id:
        return build_template_preview("字幕プレビュー", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)
    try:
        manifest = JOB_MANAGER.get(job_id)
        results = _manifest_results(manifest)
        if not results or not os.path.exists(results["caption_project_path"]):
            return build_template_preview("字幕プレビュー", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)

        project = load_caption_project(results["caption_project_path"])
        captions = project.get("captions", [])
        if not captions:
            return build_template_preview("字幕なし", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)

        selected_caption = captions[0]
        if caption_id is not None:
            for c in captions:
                if int(c["id"]) == int(caption_id):
                    selected_caption = c
                    break

        default_tmpl = project.get("templates", {}).get(project.get("default_template_id", "standard"), {})
        tmpl_id = selected_caption.get("template_id") or project.get("default_template_id", "standard")
        tmpl = project.get("templates", {}).get(tmpl_id, default_tmpl)

        out_w = int(project.get("output", {}).get("width", 1920))
        out_h = int(project.get("output", {}).get("height", 1080))

        return build_template_preview(
            text=selected_caption.get("text", ""),
            font_name=tmpl.get("font_name", "Meiryo"),
            font_size=tmpl.get("font_size", 54),
            primary_color=tmpl.get("primary_color", "#FFFFFF"),
            outline_color=tmpl.get("outline_color", "#000000"),
            bold=tmpl.get("bold", True),
            italic=tmpl.get("italic", False),
            outline=tmpl.get("outline", 3),
            shadow=tmpl.get("shadow", 1),
            alignment=tmpl.get("alignment", 2),
            margin_v=tmpl.get("margin_v", 80),
            use_custom_position=tmpl.get("position_x") is not None,
            position_x=tmpl.get("position_x"),
            position_y=tmpl.get("position_y"),
            canvas_width=out_w,
            canvas_height=out_h,
        )
    except Exception:
        return build_template_preview("字幕プレビュー", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)


def realign_editor_subtitles(job_id, bulk_text):
    if not job_id:
        payload = get_preview_payload("")
        preview_html = build_template_preview("字幕プレビュー", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)
        return [], "", gr.update(choices=[], value=None), payload, preview_html, "先にrunを選択してください。"
    try:
        manifest = JOB_MANAGER.get(job_id)
        results = _manifest_results(manifest)
        run_dir = Path(manifest["run_dir"])
        clean_path = run_dir / "clean_transcript.json"

        with open(clean_path, "r", encoding="utf-8") as file:
            raw_segments = json.load(file)

        project_path = results["caption_project_path"]
        project = load_caption_project(project_path)

        lines = [line.strip() for line in (bulk_text or "").split("\n") if line.strip()]
        realigned_project = realign_caption_project_texts(project, lines, raw_segments)
        save_caption_project(realigned_project, project_path)
        write_ass_subtitles(realigned_project, run_dir / "captions.ass")


        rows = project_to_rows(realigned_project)
        caption_ids = [row[0] for row in rows]
        payload = get_preview_payload(job_id)
        first_id = caption_ids[0] if caption_ids else None
        preview_html = render_caption_editor_preview(job_id, first_id)
        return (
            rows,
            "\n".join(lines),
            gr.update(choices=caption_ids, value=first_id),
            payload,
            preview_html,
            f"全{len(lines)}件の字幕切断とタイムスタンプを全自動で再同期し、プレビューへ即時反映しました！",
        )
    except Exception as exc:
        payload = get_preview_payload(job_id if job_id else "")
        preview_html = build_template_preview("再同期失敗", "Meiryo", 54, "#FFFFFF", "#000000", True, False, 3, 1, 2, 80, False, None, None, 1920, 1080)
        rows = []
        try:
            manifest = JOB_MANAGER.get(job_id)
            project = load_caption_project(Path(manifest["run_dir"]) / "caption_project.json")
            rows = project_to_rows(project)
        except Exception:
            pass
        caption_ids = [row[0] for row in rows]
        return rows, bulk_text or "", gr.update(choices=caption_ids, value=caption_ids[0] if caption_ids else None), payload, preview_html, f"再同期失敗: {exc}"




def save_highlight_feedback(results, verdict, corrected_start, corrected_end, comment):
    if not results:
        return "先に処理済みrunを選択してください。"
    if verdict not in {"good", "bad", "corrected"}:
        return "評価を選択してください。"
    highlight_path = results.get("highlight_json_path")
    if not highlight_path or not os.path.exists(highlight_path):
        return "保存対象のハイライト結果がありません。"
    with open(highlight_path, "r", encoding="utf-8") as file:
        original_highlight = json.load(file)
    try:
        entry = record_feedback(
            run_dir=results["run_dir"],
            original_highlight=original_highlight,
            verdict=verdict,
            corrected_start=corrected_start,
            corrected_end=corrected_end,
            comment=comment,
        )
    except ValueError as exc:
        return f"保存できません: {exc}"
    labels = {"good": "良かった", "bad": "悪かった", "corrected": "区間を修正"}
    return f"保存しました: {labels[verdict]} / ID {entry['id'][:8]}"


def apply_template_to_caption(rows, caption_id, template_id):
    if not rows or caption_id is None:
        return rows, "字幕行を選択してください。"
    updated = [list(row) for row in rows]
    for row in updated:
        if int(row[0]) == int(caption_id):
            row[4] = template_id or ""
            return updated, f"字幕{caption_id}へ {template_id or 'デフォルト'} を設定しました。"
    return updated, "指定した字幕IDが見つかりません。"


def queue_edited_render(job_id, rows, default_template_id, width, height, fps, max_chars, max_lines, encoder):
    if not job_id:
        return "先にrunを選択してください。"
    try:
        manifest = JOB_MANAGER.get(job_id)
        if manifest.get("can_stop"):
            return "処理中です。字幕を保存する前に停止してください。"
        run_dir = Path(manifest["run_dir"])
        project_path = run_dir / "caption_project.json"
        project = update_project_from_rows(
            load_caption_project(project_path),
            rows,
            default_template_id=default_template_id,
            width=width,
            height=height,
            fps=fps,
            max_chars=max_chars,
            max_lines=max_lines,
        )
        save_caption_project(project, project_path)
        write_ass_subtitles(project, run_dir / "captions.ass")
        JOB_MANAGER.update_settings(
            job_id,
            {
                "output_width": int(width),
                "output_height": int(height),
                "output_fps": int(fps),
                "max_chars": int(max_chars),
                "max_lines": int(max_lines),
                "default_template_id": default_template_id,
                "video_encoder": "auto",
                "processing_device": load_processing_device(),
            },
            invalidate=False,
        )
        JOB_MANAGER.request_rerender(job_id)
        return "字幕編集を保存し、最終動画の書き出し（再レンダリング）を開始しました！画面上部で進捗を確認できます。"
    except Exception as exc:
        return f"再レンダリング開始失敗: {exc}"



def load_template_for_edit(template_id):
    template = load_templates().get(template_id)
    if not template:
        return (None,) * 15 + ("テンプレートが見つかりません。",)
    use_custom_position = template["position_x"] is not None and template["position_y"] is not None
    return (
        template["id"], template["name"], template["font_name"], template["font_size"],
        template["primary_color"], template["outline_color"], template["bold"],
        template["italic"], template["outline"], template["shadow"], template["alignment"],
        template["margin_v"], use_custom_position, template["position_x"], template["position_y"],
        "読み込みました。変更内容は上のプレビューへ即時反映されます。組み込みテンプレートを保存すると複製されます。",
    )


def save_template_from_ui(template_id, name, font_name, font_size, primary_color, outline_color, bold, italic, outline, shadow, alignment, margin_v, use_custom_position, position_x, position_y):
    try:
        saved = save_user_template(
            {
                "id": template_id, "name": name, "font_name": font_name, "font_size": font_size,
                "primary_color": primary_color, "outline_color": outline_color, "bold": bold,
                "italic": italic, "outline": outline, "shadow": shadow, "alignment": alignment,
                "margin_v": margin_v,
                "position_x": position_x if use_custom_position else None,
                "position_y": position_y if use_custom_position else None,
            }
        )
        standard_choices = _template_choices()
        individual_choices = _template_choices(include_default=True)
        return (
            gr.update(choices=standard_choices, value=saved["id"]),
            gr.update(choices=standard_choices, value=saved["id"]),
            gr.update(choices=individual_choices, value=saved["id"]),
            saved["id"],
            f"テンプレート「{saved['name']}」を保存しました。",
        )
    except Exception as exc:
        return gr.update(), gr.update(), gr.update(), template_id, f"保存失敗: {exc}"


def delete_template_from_ui(template_id):
    try:
        deleted = delete_user_template(template_id)
        if not deleted:
            return gr.update(), gr.update(), gr.update(), "対象が見つかりません。"
        return (
            gr.update(choices=_template_choices(), value="standard"),
            gr.update(choices=_template_choices(), value="standard"),
            gr.update(choices=_template_choices(include_default=True), value=""),
            "削除しました。",
        )
    except Exception as exc:
        return gr.update(), gr.update(), gr.update(), f"削除失敗: {exc}"


def load_project_settings(preset_id):
    preset = load_project_settings_presets().get(preset_id)
    if not preset:
        return [gr.update()] * 9 + ["プリセットが見つかりません。"]
    return (
        preset["output_width"],
        preset["output_height"],
        preset["output_fps"],
        preset["max_chars"],
        preset.get("max_lines", 2),
        preset["segmentation_mode"],
        preset["caption_model"],
        preset["default_template_id"],
        preset["video_encoder"],
        f"「{preset['name']}」を読み込みました。",
    )


def save_project_settings_from_ui(preset_id, name, width, height, fps, max_chars, max_lines, segmentation_mode, caption_model, default_template_id, encoder):
    try:
        saved = save_project_settings_preset({
            "id": preset_id,
            "name": name,
            "output_width": width,
            "output_height": height,
            "output_fps": fps,
            "max_chars": max_chars,
            "max_lines": max_lines,
            "segmentation_mode": segmentation_mode,
            "caption_model": caption_model,
            "default_template_id": default_template_id,
            "video_encoder": encoder,
        })
        choices = _project_settings_choices()
        return (
            gr.update(choices=choices, value=saved["id"]),
            saved["id"],
            f"プリセット「{saved['name']}」を保存しました。"
        )
    except Exception as exc:
        return gr.update(), preset_id, f"保存失敗: {exc}"


def delete_project_settings_from_ui(preset_id):
    try:
        deleted = delete_project_settings_preset(preset_id)
        if not deleted:
            return gr.update(), "対象が見つかりません。"
        return gr.update(choices=_project_settings_choices(), value="fhd_landscape"), "削除しました。"
    except Exception as exc:
        return gr.update(), f"削除失敗: {exc}"


def toggle_custom_position(enabled):
    return gr.update(interactive=bool(enabled)), gr.update(interactive=bool(enabled))


def get_installed_ollama_models():
    default_models = ["gemma3:latest", "qwen2.5:14b"]
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = [m["name"] for m in data.get("models", [])]
            if models:
                return models
    except Exception:
        pass
    return default_models


initial_template_choices = _template_choices()
initial_project_settings_choices = _project_settings_choices()
initial_job_choices = _job_choices()
initial_font_choices = installed_font_names()
initial_ollama_models = get_installed_ollama_models()


CSS = """
.gradio-container { max-width: 1500px !important; }
.job-card { border: 1px solid #444; border-radius: 14px; padding: 16px; background: #171717; margin-bottom: 12px; }
.job-card-head { display:flex; justify-content:space-between; gap:12px; align-items:center; }
.job-stage,.job-overall,.job-times { margin-top:8px; font-size: 0.95em; color: #ddd; }
.job-error { margin-top:10px; padding:10px; border-left:4px solid #e35b5b; background:#351f1f; color:#ffdede; }
.job-progress { height:14px; background:#333; border-radius:8px; overflow:hidden; margin-top:10px; }
.job-progress > div { height:100%; background:linear-gradient(90deg,#f05a24,#ff9b4a); transition:width .3s; }
.section-note { color:#aaa; margin-top:-4px; margin-bottom:12px; font-size:0.9em; }
.template-preview-shell { width:100%; padding:4px 0 10px; }
.template-preview-stage {
  position:relative; width:100%; margin:0 auto; overflow:hidden; container-type:size;
  border:1px solid #555; border-radius:14px; background:#111; box-shadow:0 12px 34px rgba(0,0,0,.35);
}
.template-preview-scene { position:absolute; inset:0; background:linear-gradient(135deg,#1c3150 0%,#845163 46%,#d19b72 100%); }
.template-preview-scene::before { content:""; position:absolute; inset:0; background:linear-gradient(180deg,transparent 50%,rgba(0,0,0,.5)); }
.template-preview-light { position:absolute; border-radius:50%; filter:blur(2cqh); opacity:.52; }
.template-preview-light.light-one { width:42%; height:65%; left:-8%; top:-15%; background:#66c7ff; }
.template-preview-light.light-two { width:38%; height:55%; right:-5%; top:2%; background:#ffb066; }
.template-preview-subject { position:absolute; width:24%; height:72%; left:38%; bottom:-10%; border-radius:48% 48% 20% 20%; background:linear-gradient(160deg,#222,#090909); box-shadow:0 0 4cqh rgba(255,255,255,.18); }
.template-preview-safe { position:absolute; inset:5%; border:1px dashed rgba(255,255,255,.28); pointer-events:none; }
.template-preview-text { position:absolute; z-index:3; width:max-content; max-width:90%; line-height:1.28; text-align:center; white-space:pre-wrap; overflow-wrap:anywhere; letter-spacing:.02em; }
.template-preview-meta { position:absolute; z-index:4; top:8px; left:10px; padding:4px 7px; border-radius:6px; color:#eee; background:rgba(0,0,0,.58); font:11px/1.2 Segoe UI,sans-serif; }
.template-preview-note { margin:8px 0 0; color:#aaa; text-align:center; font-size:.86rem; }
@media (max-width: 760px) {
  .job-card-head { flex-direction:column; align-items:flex-start; }
  .template-preview-meta { font-size:9px; }
}
"""

with gr.Blocks(title="YouTube Clip Automation") as app:
    active_job_id = gr.State(value=None)
    run_state = gr.State(value=None)
    template_edit_id = gr.State(value=None)
    preview_payload_state = gr.State(value=get_preview_payload(""))
    poll_timer = gr.Timer(1.0, active=True)

    gr.Markdown("# 🎬 YouTube 切り抜き自動化・字幕エディタ")
    gr.Markdown("処理はrun単位で保存されます。新規処理 → 進捗確認 → 字幕編集 → 最終書き出しの順で操作します。")
    with gr.Row():
        health_bar = gr.Markdown(
            format_health(check_ollama(), initial_ollama_models[0] if initial_ollama_models else "gemma3:latest")
        )
        refresh_health_btn = gr.Button("再確認")
        start_ollama_btn = gr.Button("Ollamaを起動")

    with gr.Accordion("① 入力と事前確認（出力・字幕設定）", open=True):
        with gr.Row():
            project_settings_selector = gr.Dropdown(
                choices=initial_project_settings_choices,
                value="fhd_landscape",
                label="プロジェクト設定テンプレート",
                scale=3,
            )
            load_project_settings_btn = gr.Button("設定を読み込む", scale=1)
        with gr.Row():
            project_settings_name = gr.Textbox(label="設定テンプレート名", value="新規設定")
            save_project_settings_btn = gr.Button("現在の設定を保存", variant="primary")
            delete_project_settings_btn = gr.Button("ユーザー設定を削除")
        project_settings_status = gr.Textbox(label="設定保存状態", interactive=False)
        gr.Markdown("---")
        gr.Markdown("解像度プリセットを選んだ後も、横・縦の数値は自由に上書きできます。", elem_classes="section-note")
        with gr.Row():
            resolution_preset = gr.Dropdown(
                choices=RESOLUTION_CHOICES,
                value="fhd_landscape",
                label="解像度プリセット",
                info="720p / FHD / 4K、横・縦・正方形",
            )
            output_width = gr.Number(
                value=1920, label="横解像度（px）", minimum=320, maximum=7680
            )
            output_height = gr.Number(
                value=1080, label="縦解像度（px）", minimum=240, maximum=7680
            )
            output_fps = gr.Number(value=60, label="FPS", minimum=1, maximum=240)
            max_chars = gr.Number(value=32, label="字幕ブロックの文字数上限", minimum=8, maximum=80)
            max_lines = gr.Number(value=2, label="字幕の最大行数", minimum=1, maximum=16, precision=0)
        with gr.Row():
            segmentation_mode = gr.Dropdown(
                choices=[("高品質LLM自動分割（低速）", "llm"), ("ルールベース（高速）", "heuristic")],
                value="llm", label="字幕区切り方式",
            )
            caption_model = gr.Dropdown(
                choices=initial_ollama_models, value=initial_ollama_models[0] if initial_ollama_models else "gemma3:latest",
                allow_custom_value=True, label="字幕分割LLM",
            )
            processing_device = gr.Dropdown(
                choices=[
                    ("自動（推奨）", "auto"),
                    ("GPU優先", "gpu"),
                    ("CPU", "cpu"),
                ],
                value=load_processing_device(),
                label="処理デバイス",
                info="この設定はuserDataへ保存され、ProjectFileには固定されません。",
            )


            default_template = gr.Dropdown(
                choices=initial_template_choices, value="standard", label="デフォルト字幕テンプレート",
            )
            video_encoder = gr.Dropdown(
                choices=[("自動（NVENC優先）", "auto"), ("GPU NVENC", "h264_nvenc"), ("CPU libx264", "libx264")],
                value="auto", label="動画エンコーダー（旧legacy経路のみ）",
                info="Phase 6 v2 productionではvideo_editor側がencoderを決定します。",
            )
        with gr.Row():
            acceleration_status = gr.Markdown(
                value=refresh_acceleration_status(
                    load_processing_device(),
                    initial_ollama_models[0] if initial_ollama_models else "qwen2.5:14b",
                )
            )
            refresh_acceleration_btn = gr.Button("処理デバイスを再確認")

    gr.Markdown("## ② 処理と進捗")
    gr.Markdown("同じ入力・同じ設定で開始ボタンを連打しても、処理中の1ジョブへ統合します。", elem_classes="section-note")
    with gr.Row():
        job_selector = gr.Dropdown(choices=initial_job_choices, label="既存run")
        load_job_btn = gr.Button("runを読み込む")
        refresh_jobs_btn = gr.Button("一覧更新")
    progress_card = gr.HTML(value=_progress_card(None))
    with gr.Row():
        stop_btn = gr.Button("安全に停止", variant="stop", interactive=False)
        resume_btn = gr.Button("未完了段階から再開", variant="primary", interactive=False)
        rewind_stage_selector = gr.Dropdown(
            choices=[
                ("文字起こし(Whisper)から", "transcribe"),
                ("無音解析から", "jumpcut"),
                ("字幕生成(Ollama)から", "captions"),
                ("候補選定・編集計画から", "planning"),
                ("動画レンダリングから", "render"),
            ],
            value="captions",
            label="やり直す段階",
        )
        rewind_btn = gr.Button("設定を適用してやり直す", variant="primary", interactive=False)
    stage_table = gr.Dataframe(
        headers=STAGE_HEADERS, datatype=["str"] * 4, type="array", interactive=False, value=[]
    )
    out_log = gr.Textbox(label="現在の状態", interactive=False)

    with gr.Tabs():
        with gr.Tab("① 入力と開始"):
            with gr.Tabs():
                with gr.Tab("YouTubeから取得"):
                    yt_url = gr.Textbox(label="YouTube URL")
                    yt_btn = gr.Button("YouTube処理を開始", variant="primary")
                with gr.Tab("ローカル動画"):
                    gr.Markdown("ファイルの選択・ドロップ、またはファイルパスの直接コピー＆ペーストが可能です。巨大動画はパス指定が最も高速です。", elem_classes="section-note")
                    with gr.Row():
                        local_path_input = gr.Textbox(
                            label="動画ファイルパス（直接入力 / コピペ）",
                            placeholder=r"C:\Users\user\Videos\sample.mp4",
                            scale=3,
                        )
                        local_file_upload = gr.File(
                            label="ファイルを選択 / ドロップ",
                            file_types=["video"],
                            type="filepath",
                            scale=2,
                        )
                    local_btn = gr.Button("ローカル動画処理を開始", variant="primary")

            with gr.Row():
                out_video = gr.Video(label="完成動画", interactive=False)
                with gr.Column():
                    out_highlight = gr.Textbox(label="Ollama ハイライト提案", lines=6, interactive=False)
                    out_clip_results = gr.JSON(label="生成クリップ一覧", value=[])
                    out_srt_file = gr.File(label="字幕SRT")

            with gr.Accordion("投稿（明示操作 / YouTube）", open=False):
                gr.Markdown("render完了後も自動投稿は行いません。対象clip、タイトル、privacyを確認してから投稿準備・dry-run・投稿を個別に実行してください。既定privacyはprivateです。")
                with gr.Row():
                    publish_title = gr.Textbox(label="タイトル（空欄時はclip_id）", value="", scale=2)
                    publish_privacy = gr.Dropdown(
                        choices=[("private", "private"), ("unlisted", "unlisted"), ("public", "public")],
                        value="private", label="privacy", scale=1,
                    )
                    publish_tags = gr.Textbox(label="タグ（カンマ区切り）", value="", scale=2)
                publish_description = gr.TextArea(label="説明", value="", lines=3)
                with gr.Row():
                    prepare_publish_btn = gr.Button("投稿準備", variant="secondary")
                    publish_dry_run_btn = gr.Button("dry-run（remote変更なし）")
                    publish_youtube_btn = gr.Button("YouTubeへ投稿", variant="primary")
                    refresh_publish_status_btn = gr.Button("processing status更新")
                publish_manifest_path = gr.Textbox(label="publish manifest path", interactive=False)
                publish_result = gr.JSON(label="投稿状態", value={})
                publish_status = gr.Textbox(label="投稿メッセージ", interactive=False)

            gr.Markdown("### ハイライト提案へのフィードバック")
            feedback_verdict = gr.Radio(
                choices=[("良かった", "good"), ("悪かった", "bad"), ("区間を修正", "corrected")], label="評価"
            )
            with gr.Row():
                corrected_start = gr.Number(label="修正後の開始秒", minimum=0)
                corrected_end = gr.Number(label="修正後の終了秒", minimum=0)
            feedback_comment = gr.Textbox(label="編集メモ")
            feedback_btn = gr.Button("フィードバックを保存")
            feedback_status = gr.Textbox(label="保存結果")

        with gr.Tab("③ 字幕確認・修正"):
            load_captions_btn = gr.Button("直近の字幕データを読み込む")
            
            gr.Markdown("### 1. 文章・区切りの自由直感編集 ＆ タイムスタンプ全自動再同期")
            gr.Markdown("テキストを自由編集したり、改行(`\\n`)を入れて区切り位置を変更できます。ボタンを押すと秒数が全自動で完璧に再同期計算・保存されます。")
            bulk_text_editor = gr.TextArea(
                label="字幕文章 ＆ 区切り編集（1行＝1字幕）",
                lines=8,
                placeholder="「直近の字幕データを読み込む」を押すとここにテキストが表示されます。"
            )
            realign_btn = gr.Button("⚡ 改行・テキスト編集を適用し、タイミングを全自動再同期・保存", variant="primary")
            
            gr.Markdown("### 2. 字幕プレビュー ＆ 詳細タイミング・テンプレート表編集")
            realtime_preview = gr.HTML(
                value=get_preview_payload(""),
                html_template=PREVIEW_HTML_TEMPLATE,
                css_template=PREVIEW_CSS_TEMPLATE,
                js_on_load=PREVIEW_JS_ON_LOAD,
                server_functions=[get_preview_payload, save_preview_caption],
            )
            caption_table = gr.Dataframe(
                headers=CAPTION_HEADERS,
                datatype=["number", "number", "number", "str", "str"],
                type="array", interactive=True, wrap=True, max_height=420,
            )
            with gr.Row():
                caption_selector = gr.Dropdown(label="字幕ID")
                individual_template = gr.Dropdown(
                    choices=_template_choices(include_default=True), value="", label="個別テンプレート"
                )
                apply_template_btn = gr.Button("選択字幕へ適用")
            editor_status = gr.Textbox(label="編集状態")
            render_btn = gr.Button("④ 字幕編集を保存して最終書き出し", variant="primary")



        with gr.Tab("テンプレート管理"):
            with gr.Row():
                template_selector = gr.Dropdown(
                    choices=initial_template_choices,
                    value="standard",
                    label="読み込むテンプレート",
                    scale=3,
                )
                load_template_btn = gr.Button("読み込む", scale=1)

            gr.Markdown("### 作成中のライブプレビュー")
            with gr.Row():
                template_preview_res = gr.Dropdown(
                    choices=RESOLUTION_CHOICES,
                    value="fhd_landscape",
                    label="プレビュー画面サイズ / アスペクト比",
                    scale=2,
                    info="選択した画面サイズ・向き（横16:9 / 縦Shorts 9:16 / 1:1等）で字幕の配置と見え方をリアルタイム確認できます。",
                )
                template_sample_text = gr.Textbox(
                    label="プレビュー文字",
                    value="字幕の見た目をここで確認できます。",
                    lines=1,
                    scale=3,
                    info="文字を入力すると、下の画面へ即時反映されます。",
                )

            template_preview = gr.HTML(
                value=build_template_preview(
                    "字幕の見た目をここで確認できます。",
                    "Meiryo",
                    54,
                    "#FFFFFF",
                    "#000000",
                    True,
                    False,
                    3,
                    1,
                    2,
                    80,
                    False,
                    None,
                    None,
                    1920,
                    1080,
                )
            )

            with gr.Accordion("文字・フォント", open=True):
                with gr.Row():
                    template_name = gr.Textbox(
                        label="テンプレート名", value="新規テンプレート"
                    )
                    font_name = gr.Dropdown(
                        choices=initial_font_choices,
                        value="Meiryo",
                        allow_custom_value=True,
                        filterable=True,
                        label="フォント",
                        info="インストール済み一覧から選択。名前の直接入力も可能",
                    )
                    font_size = gr.Number(
                        label="文字サイズ", value=54, minimum=8, maximum=300
                    )
                with gr.Row():
                    bold = gr.Checkbox(label="太字", value=True)
                    italic = gr.Checkbox(label="斜体", value=False)

            with gr.Accordion("色・縁取り・影", open=True):
                with gr.Row():
                    primary_color = gr.ColorPicker(label="文字色", value="#FFFFFF")
                    outline_color = gr.ColorPicker(label="縁取り色", value="#000000")
                    outline_width = gr.Number(
                        label="縁取り幅", value=3, minimum=0, maximum=20
                    )
                    shadow = gr.Number(label="影", value=1, minimum=0, maximum=20)

            with gr.Accordion("位置", open=True):
                with gr.Row():
                    alignment = gr.Dropdown(
                        choices=ALIGNMENT_CHOICES, value=2, label="基準位置"
                    )
                    margin_v = gr.Number(label="上下余白", value=80, minimum=0)
                    use_custom_position = gr.Checkbox(
                        label="X/Y座標を直接指定", value=False
                    )
                with gr.Row():
                    position_x = gr.Number(
                        label="X座標", value=None, interactive=False
                    )
                    position_y = gr.Number(
                        label="Y座標", value=None, interactive=False
                    )
            with gr.Row():
                save_template_btn = gr.Button("新規保存／更新", variant="primary")
                delete_template_btn = gr.Button("ユーザーテンプレートを削除")
            template_status = gr.Textbox(label="テンプレート状態")

    common_settings = [
        output_width, output_height, output_fps, max_chars, max_lines, segmentation_mode,
        caption_model, default_template, video_encoder,
    ]
    refresh_health_btn.click(
        refresh_system_health, inputs=caption_model,
        outputs=[health_bar, caption_model], show_progress="hidden",
    )
    processing_device.change(
        save_processing_device_from_ui,
        inputs=[processing_device, caption_model],
        outputs=acceleration_status,
        show_progress="hidden",
    )
    refresh_acceleration_btn.click(
        refresh_acceleration_status,
        inputs=[processing_device, caption_model],
        outputs=acceleration_status,
        show_progress="hidden",
    )
    caption_model.change(
        refresh_acceleration_status,
        inputs=[processing_device, caption_model],
        outputs=acceleration_status,
        show_progress="hidden",
    )
    start_ollama_btn.click(
        launch_ollama_from_ui, inputs=caption_model, outputs=health_bar,
    )
    resolution_preset.change(
        apply_resolution_preset,
        inputs=[resolution_preset, output_width, output_height],
        outputs=[output_width, output_height],
        show_progress="hidden",
    )
    output_width.input(
        detect_resolution_preset,
        inputs=[output_width, output_height],
        outputs=resolution_preset,
        show_progress="hidden",
    )
    output_height.input(
        detect_resolution_preset,
        inputs=[output_width, output_height],
        outputs=resolution_preset,
        show_progress="hidden",
    )
    start_outputs = [
        active_job_id,
        job_selector,
        progress_card,
        out_log,
        preview_payload_state,
        realtime_preview,
    ]
    yt_btn.click(process_youtube, inputs=[yt_url] + common_settings, outputs=start_outputs)
    local_btn.click(process_local_file, inputs=[local_path_input, local_file_upload] + common_settings, outputs=start_outputs)

    prepare_publish_btn.click(
        prepare_publish_ui,
        inputs=[active_job_id, publish_title, publish_description, publish_tags, publish_privacy],
        outputs=[publish_manifest_path, publish_result, publish_status],
    )
    publish_dry_run_btn.click(
        dry_run_publish_ui,
        inputs=publish_manifest_path,
        outputs=[publish_result, publish_status],
    )
    publish_youtube_btn.click(
        publish_youtube_ui,
        inputs=[active_job_id, publish_manifest_path],
        outputs=[publish_result, publish_status],
    )
    refresh_publish_status_btn.click(
        refresh_publish_status_ui,
        inputs=publish_manifest_path,
        outputs=[publish_result, publish_status],
    )

    refresh_jobs_btn.click(refresh_job_list, inputs=active_job_id, outputs=job_selector)
    load_job_btn.click(
        select_existing_job,
        inputs=job_selector,
        outputs=[active_job_id, progress_card, out_log, preview_payload_state, realtime_preview],
    )
    stop_btn.click(stop_job, inputs=active_job_id, outputs=[progress_card, out_log])
    resume_btn.click(resume_job, inputs=active_job_id, outputs=[progress_card, out_log])
    rewind_btn.click(
        rewind_and_start_job,
        inputs=[active_job_id, rewind_stage_selector] + common_settings,
        outputs=[progress_card, out_log]
    )

    load_project_settings_btn.click(
        load_project_settings,
        inputs=project_settings_selector,
        outputs=common_settings + [project_settings_status]
    )
    save_project_settings_btn.click(
        save_project_settings_from_ui,
        inputs=[project_settings_selector, project_settings_name] + common_settings,
        outputs=[project_settings_selector, project_settings_selector, project_settings_status]
    )
    delete_project_settings_btn.click(
        delete_project_settings_from_ui,
        inputs=project_settings_selector,
        outputs=[project_settings_selector, project_settings_status]
    )

    poll_timer.tick(
        poll_job,
        inputs=[active_job_id, preview_payload_state],
        outputs=[
            progress_card, stage_table, out_log, stop_btn, resume_btn, rewind_btn, out_video,
            out_srt_file, out_highlight, run_state, out_clip_results, preview_payload_state, realtime_preview,
        ],
        show_progress="hidden",
    )
    load_captions_btn.click(
        load_caption_editor,
        inputs=active_job_id,
        outputs=[
            caption_table,
            bulk_text_editor,
            caption_selector,
            preview_payload_state,
            realtime_preview,
            editor_status,
        ],
    )
    realign_btn.click(
        realign_editor_subtitles,
        inputs=[active_job_id, bulk_text_editor],
        outputs=[
            caption_table,
            bulk_text_editor,
            caption_selector,
            preview_payload_state,
            realtime_preview,
            editor_status,
        ],
    )

    caption_selector.change(
        render_caption_editor_preview,
        inputs=[active_job_id, caption_selector],
        outputs=realtime_preview,
    )
    feedback_btn.click(
        save_highlight_feedback,
        inputs=[run_state, feedback_verdict, corrected_start, corrected_end, feedback_comment],
        outputs=feedback_status,
    )

    apply_template_btn.click(
        apply_template_to_caption,
        inputs=[caption_table, caption_selector, individual_template],
        outputs=[caption_table, editor_status],
    )
    render_btn.click(
        queue_edited_render,
        inputs=[
            active_job_id, caption_table, default_template, output_width,
            output_height, output_fps, max_chars, max_lines, video_encoder,
        ],
        outputs=editor_status,
    )

    template_field_outputs = [
        template_edit_id, template_name, font_name, font_size, primary_color,
        outline_color, bold, italic, outline_width, shadow, alignment, margin_v,
        use_custom_position, position_x, position_y, template_status,
    ]
    load_template_event = load_template_btn.click(
        load_template_for_edit, inputs=template_selector, outputs=template_field_outputs
    )
    save_template_btn.click(
        save_template_from_ui,
        inputs=[
            template_edit_id, template_name, font_name, font_size, primary_color,
            outline_color, bold, italic, outline_width, shadow, alignment, margin_v,
            use_custom_position, position_x, position_y,
        ],
        outputs=[template_selector, default_template, individual_template, template_edit_id, template_status],
    )
    delete_template_btn.click(
        delete_template_from_ui,
        inputs=template_edit_id,
        outputs=[template_selector, default_template, individual_template, template_status],
    )

    def _render_template_preview(
        sample_text, font_name, font_size, primary_color, outline_color,
        bold, italic, outline_width, shadow, alignment, margin_v,
        use_custom_position, position_x, position_y, preset_id, w, h
    ):
        width, height = apply_resolution_preset(preset_id, w, h)
        return build_template_preview(
            sample_text, font_name, font_size, primary_color, outline_color,
            bold, italic, outline_width, shadow, alignment, margin_v,
            use_custom_position, position_x, position_y, width, height
        )

    template_preview_inputs = [
        template_sample_text,
        font_name,
        font_size,
        primary_color,
        outline_color,
        bold,
        italic,
        outline_width,
        shadow,
        alignment,
        margin_v,
        use_custom_position,
        position_x,
        position_y,
        template_preview_res,
        output_width,
        output_height,
    ]
    for preview_input in [
        template_sample_text,
        font_name,
        font_size,
        primary_color,
        outline_color,
        bold,
        italic,
        outline_width,
        shadow,
        alignment,
        margin_v,
        use_custom_position,
        position_x,
        position_y,
        template_preview_res,
    ]:
        preview_input.change(
            _render_template_preview,
            inputs=template_preview_inputs,
            outputs=template_preview,
            show_progress="hidden",
        )
    output_width.change(
        _render_template_preview,
        inputs=template_preview_inputs,
        outputs=template_preview,
        show_progress="hidden",
    )
    output_height.change(
        _render_template_preview,
        inputs=template_preview_inputs,
        outputs=template_preview,
        show_progress="hidden",
    )

    use_custom_position.change(
        toggle_custom_position,
        inputs=use_custom_position,
        outputs=[position_x, position_y],
        show_progress="hidden",
    )
    load_template_event.then(
        _render_template_preview,
        inputs=template_preview_inputs,
        outputs=template_preview,
        show_progress="hidden",
    )



if __name__ == "__main__":
    app.queue(default_concurrency_limit=8).launch(
        server_name="127.0.0.1",
        server_port=7860,
        allowed_paths=[str(RUNS_DIR)],
        css=CSS,
    )
