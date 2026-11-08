"""Phase 6 production pipeline: acquire -> planning -> external render.

This module owns the new production orchestration only.  The pre-Phase-6
pipeline remains in :mod:`pipeline` as an explicit legacy implementation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from caption import generate_subtitles
from caption_settings import DEFAULT_MAX_LINES
from download import download_video
from job_control import raise_if_cancelled
from silence_detection import generate_silence_cut_data
from planning_manifest import load_manifest as load_planning_manifest
from planning_pipeline import run_planning
from render_manifest import load_manifest as load_render_manifest
from render_pipeline import render_planning_outputs
from subtitle_project import (
    create_caption_project,
    load_caption_project,
    save_caption_project,
    write_ass_subtitles,
)
from timeline_preparation import create_native_timeline_project
from transcribe import transcribe_video
from processing_device import resolve_processing_policy, validate_processing_device

# Compatibility symbol for older test/integration patch points. Production
# execution below never calls the legacy filler-removal implementation.
generate_jumpcut_data = None


def _emit(callback, stage_id: str, progress: float, message: str) -> None:
    if callback:
        callback(stage_id, max(0.0, min(1.0, float(progress))), message)


def _json_valid(path: Path, *, nonempty_list: bool = False) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(value) if nonempty_list else isinstance(value, (dict, list))


def sha256_stream(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_source_media(path: str | Path) -> dict[str, Any]:
    """Probe video metadata and hash the source without loading it in memory."""

    source = Path(path).resolve()
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate:format=duration",
        "-of", "json", str(source),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "source ffprobe failed")
    data = json.loads(completed.stdout)
    stream = data["streams"][0]
    rate = stream.get("avg_frame_rate", "0/1")
    return {
        "duration": float(data["format"]["duration"]),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": float(Fraction(rate)) if rate not in {"0/0", "0/1"} else 0.0,
        "codec": stream.get("codec_name"),
        "sha256": sha256_stream(source),
    }


def _caption_inputs_ready(run_dir: Path) -> bool:
    return all(
        (
            run_dir / name
        ).is_file()
        for name in (
            "captions.srt",
            "caption_project.json",
            "caption_project.v2.json",
            "transcript_words.json",
            "migration_report.json",
        )
    ) and _json_valid(run_dir / "caption_project.v2.json")


def _planning_ready(planning_dir: Path) -> bool:
    manifest_path = planning_dir / "job_manifest.json"
    try:
        manifest = load_planning_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    stage = manifest.get("stages", {}).get("contract_generation", {})
    artifacts = stage.get("artifacts", [])
    return (
        stage.get("state") == "completed"
        and all(
            entry.get("state") == "completed"
            and Path(entry["path"]).is_file()
            for entry in artifacts
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        )
    )


def _render_ready(planning_dir: Path) -> bool:
    try:
        manifest = load_render_manifest(planning_dir / "render_manifest.v1.json")
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    summary = manifest.get("summary", {})
    return summary.get("failed", 0) == 0 and summary.get("skipped", 0) == 0 and all(
        item.get("state") == "completed"
        and (planning_dir / item["output"]["path"]).is_file()
        for item in manifest.get("clips", [])
    )


def _clip_results(planning_dir: Path, render_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for entry in render_manifest.get("clips", []):
        output = entry.get("output", {})
        edit_project = entry.get("edit_project", {})
        results.append(
            {
                "clip_id": entry.get("clip_id"),
                "state": entry.get("state"),
                "edit_project_path": str((planning_dir / edit_project["path"]).resolve()),
                "rendered_video_path": str((planning_dir / output["path"]).resolve()),
                "output_sha256": output.get("sha256"),
                "error": entry.get("error"),
            }
        )
    return results


def run_v2_pipeline(
    video_source: str,
    is_local_file: bool = False,
    base_dir: str | Path | None = None,
    *,
    existing_run_dir: str | Path | None = None,
    resume: bool = False,
    output_width: int = 1920,
    output_height: int = 1080,
    output_fps: int = 60,
    max_chars: int = 32,
    max_lines: int = DEFAULT_MAX_LINES,
    use_llm_captions: bool = True,
    caption_model: str = "qwen2.5:14b",
    default_template_id: str = "standard",
    video_encoder: str = "auto",
    processing_device: str = "auto",
    force_render: bool = False,
    prepare_only: bool = False,
    progress_callback=None,
    cancel_event=None,
    candidate_provider=None,
    evaluator_provider=None,
    planning_options: dict[str, Any] | None = None,
    video_editor_root: str | Path | None = None,
    explicit_command=None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run a new production job without invoking the legacy renderer."""

    processing_device = validate_processing_device(processing_device)
    processing_policy = resolve_processing_policy(processing_device)

    if prepare_only:
        raise ValueError("prepare_only is supported only by the legacy/local compatibility path")

    from pipeline import _create_run_dir, inspect_run

    run_dir = Path(existing_run_dir).resolve() if existing_run_dir else _create_run_dir(base_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = inspect_run(run_dir) if resume else None

    raise_if_cancelled(cancel_event)
    source_path = None
    if resume and snapshot and snapshot.get("source_path") and Path(snapshot["source_path"]).is_file():
        source_path = Path(snapshot["source_path"]).resolve()
        _emit(progress_callback, "acquire", 1.0, "既存の取得済み動画を使用")
    elif is_local_file:
        source_path = Path(video_source).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"ローカルファイルが見つかりません: {source_path}")
        _emit(progress_callback, "acquire", 1.0, "ローカル動画を確認")
    else:
        _emit(progress_callback, "acquire", 0.0, "YouTube動画を取得中")
        source_path = Path(
            download_video(
                video_source,
                str(run_dir),
                progress_callback=lambda value, message: _emit(progress_callback, "acquire", value, message),
                cancel_event=cancel_event,
            )
        ).resolve()
        _emit(progress_callback, "acquire", 1.0, "動画取得完了")

    raise_if_cancelled(cancel_event)
    transcript_path = run_dir / "transcript.json"
    if resume and _json_valid(transcript_path, nonempty_list=True):
        _emit(progress_callback, "transcribe", 1.0, "既存の文字起こしを使用")
    else:
        _emit(progress_callback, "transcribe", 0.0, "Whisper文字起こしを開始")
        transcribe_video(
            str(source_path),
            output_json=str(transcript_path),
            progress_callback=lambda value, message: _emit(progress_callback, "transcribe", value, message),
            cancel_event=cancel_event,
            acceleration_mode=processing_policy["whisper_mode"],
        )

    raise_if_cancelled(cancel_event)
    edl_path = run_dir / "edl.json"
    clean_path = run_dir / "clean_transcript.json"
    silence_analysis_path = run_dir / "silence_analysis.json"
    if resume and _json_valid(edl_path, nonempty_list=True) and _json_valid(clean_path, nonempty_list=True):
        _emit(progress_callback, "jumpcut", 1.0, "既存のEDLを使用")
    else:
        _emit(progress_callback, "jumpcut", 0.0, "音声信号から無音区間を解析中")
        if generate_jumpcut_data is not None:
            # Keep an explicit compatibility seam for older integration tests
            # and downstream callers that inject their own deterministic EDL
            # producer. Normal production execution leaves this hook as None.
            generate_jumpcut_data(str(transcript_path), str(edl_path), str(clean_path))
            _emit(progress_callback, "jumpcut", 1.0, "互換解析hookでEDLを生成しました")
        else:
            generate_silence_cut_data(
                str(source_path),
                str(transcript_path),
                str(edl_path),
                str(clean_path),
                output_analysis=str(silence_analysis_path),
            )
            _emit(progress_callback, "jumpcut", 1.0, "音声信号ベースの無音区間を解析しました（フィラー削除なし）")

    raise_if_cancelled(cancel_event)
    srt_path = run_dir / "captions.srt"
    ass_path = run_dir / "captions.ass"
    caption_project_path = run_dir / "caption_project.json"
    if resume and _caption_inputs_ready(run_dir):
        caption_project = load_caption_project(caption_project_path)
        caption_result = {"method": "checkpoint", "captions": caption_project["captions"], "blocks": len(caption_project["captions"])}
        _emit(progress_callback, "captions", 1.0, "既存の字幕成果物を使用")
    else:
        _emit(progress_callback, "captions", 0.0, "字幕を生成中")
        caption_result = generate_subtitles(
            str(clean_path), str(srt_path), use_llm=use_llm_captions, model=caption_model,
            max_chars=max_chars, max_lines=max_lines,
            processing_device=processing_device,
            collect_runtime=True,
        )
        caption_project = create_caption_project(
            captions=caption_result["captions"], source_path=str(source_path), edl_path=str(edl_path),
            run_dir=str(run_dir), default_template_id=default_template_id,
            width=output_width, height=output_height, fps=output_fps,
            max_chars=max_chars, max_lines=max_lines,
        )
        save_caption_project(caption_project, caption_project_path)
        create_native_timeline_project(
            run_dir, captions=caption_project["captions"], source_path=str(source_path), edl_path=str(edl_path),
            default_template_id=default_template_id, width=output_width, height=output_height,
            fps=output_fps, max_chars=max_chars, max_lines=max_lines,
        )
        write_ass_subtitles(caption_project, ass_path)
        _emit(progress_callback, "captions", 1.0, "字幕成果物生成完了")

    # v2の出力設定authorityはJobManagerから渡されたcurrent settingsである。
    # caption_project.jsonは字幕stageのcheckpointであり、過去のoutput値を
    # resume/replan時の設定へ復元してはならない。legacy pipelineの挙動は
    # pipeline.py側に隔離して維持する。
    caption_project = load_caption_project(caption_project_path)
    source_probe = probe_source_media(source_path)
    planning_dir = run_dir / "planning"
    planning_options = dict(planning_options or {})

    raise_if_cancelled(cancel_event)
    if resume and _planning_ready(planning_dir) and not planning_options.get("force_planning"):
        _emit(progress_callback, "planning", 1.0, "既存のplanning成果物を使用")
    else:
        _emit(progress_callback, "planning", 0.0, "候補選定・編集計画を生成中")
        run_planning(
            run_dir,
            planning_dir=planning_dir,
            source_media_probe=source_probe,
            candidate_provider=candidate_provider,
            evaluator_provider=evaluator_provider,
            target_output={"width": output_width, "height": output_height, "fps": output_fps},
            progress_callback=lambda value, message: _emit(progress_callback, "planning", value, message),
            cancel_event=cancel_event,
            **{key: value for key, value in planning_options.items() if key != "force_planning"},
        )
        _emit(progress_callback, "planning", 1.0, "編集計画生成完了")

    raise_if_cancelled(cancel_event)
    if resume and not force_render and _render_ready(planning_dir):
        render_manifest_path = planning_dir / "render_manifest.v1.json"
        render_manifest = load_render_manifest(render_manifest_path)
        _emit(progress_callback, "render", 1.0, "既存のrender成果物を使用")
    else:
        _emit(progress_callback, "render", 0.0, "外部video_editorでクリップをレンダリング中")
        rendered = render_planning_outputs(
            planning_dir,
            video_editor_root=video_editor_root,
            explicit_command=explicit_command,
            repo_root=repo_root or Path(__file__).resolve().parent,
            progress_callback=lambda value, message: _emit(progress_callback, "render", value, message),
            cancel_event=cancel_event,
        )
        render_manifest_path = rendered.manifest_path
        render_manifest = rendered.manifest
    clips = _clip_results(planning_dir, render_manifest)
    completed_outputs = [item["rendered_video_path"] for item in clips if item["state"] == "completed"]
    failed_outputs = [item for item in clips if item["state"] != "completed"]
    status = "completed" if not failed_outputs else "failed"
    _emit(progress_callback, "render", len(completed_outputs) / max(1, len(clips)),
          "全クリップのレンダリング完了" if status == "completed" else "一部クリップのレンダリングに失敗")
    return {
        "pipeline_version": "v2",
        "status": status,
        "run_dir": str(run_dir),
        "planning_dir": str(planning_dir),
        "planning_manifest_path": str(planning_dir / "job_manifest.json"),
        "render_manifest_path": str(render_manifest_path),
        "clips": clips,
        "completed_outputs": completed_outputs,
        "failed_outputs": failed_outputs,
        "source_path": str(source_path),
        "source_media_probe": source_probe,
        "srt_path": str(srt_path),
        "ass_path": str(ass_path),
        "caption_project_path": str(caption_project_path),
        "silence_analysis_path": str(silence_analysis_path),
        "caption_result": caption_result,
        "render_summary": render_manifest["summary"],
        "render_info": {"renderer": render_manifest.get("renderer")},
        "processing_device": processing_device,
        "processing_policy": processing_policy,
        "caption_ai_runtime": (caption_result or {}).get("caption_ai_runtime"),
    }
