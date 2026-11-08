import argparse
import datetime
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from caption import generate_subtitles
from clip import create_clip
from download import download_video
from highlight import (
    _validate_highlight,
    analyze_highlights,
    build_edit_regions,
    fallback_highlights,
    validate_highlights,
)
from job_control import raise_if_cancelled
from silence_detection import generate_silence_cut_data
from subtitle_project import (
    create_caption_project,
    load_caption_project,
    save_caption_project,
    write_ass_subtitles,
)
from transcribe import transcribe_video
from timeline_preparation import create_native_timeline_project
from processing_device import resolve_processing_policy, validate_processing_device


PROJECT_ROOT = Path(__file__).resolve().parent
STAGE_DEFINITIONS = [
    ("acquire", "動画取得", 0.10),
    ("transcribe", "文字起こし", 0.25),
    ("jumpcut", "無音解析", 0.05),
    ("captions", "字幕生成", 0.10),
    ("render", "動画レンダリング", 0.45),
    ("highlight", "ハイライト抽出", 0.05),
]

LEGACY_STAGE_DEFINITIONS = tuple(STAGE_DEFINITIONS)
V2_STAGE_DEFINITIONS = [
    ("acquire", "動画取得", 0.10),
    ("transcribe", "文字起こし", 0.25),
    ("jumpcut", "無音解析", 0.05),
    ("captions", "字幕生成", 0.10),
    ("planning", "候補選定・編集計画", 0.15),
    ("render", "動画レンダリング", 0.35),
]


def stage_definitions_for(pipeline_version: str):
    if pipeline_version == "legacy":
        return LEGACY_STAGE_DEFINITIONS
    if pipeline_version == "v2":
        return V2_STAGE_DEFINITIONS
    raise ValueError(f"unsupported pipeline_version: {pipeline_version}")


def _create_run_dir(base_dir: str | os.PathLike | None = None) -> Path:
    data_dir = Path(base_dir) if base_dir else PROJECT_ROOT / "data"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    runs_dir = data_dir / "runs"
    run_dir = runs_dir / timestamp
    suffix = 1
    while run_dir.exists():
        run_dir = runs_dir / f"{timestamp}_{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=True)
    return run_dir


def _json_is_valid(path: Path, *, nonempty_list=False) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data) if nonempty_list else isinstance(data, (dict, list))


def _transcript_duration(path: Path) -> float:
    with path.open("r", encoding="utf-8") as file:
        segments = json.load(file)
    if not isinstance(segments, list) or not segments:
        raise ValueError("文字起こしデータが空です。")
    return max(float(segment["end"]) for segment in segments)


def load_valid_highlight(path: Path, clean_transcript_path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return _validate_highlight(data, _transcript_duration(clean_transcript_path))


def load_valid_highlights(path: Path, clean_transcript_path: Path) -> list[dict]:
    duration = _transcript_duration(clean_transcript_path)
    if path.is_file():
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        candidates = validate_highlights(data, duration)
        if candidates:
            return candidates
    return [load_valid_highlight(path.with_name("highlight.json"), clean_transcript_path)]


def video_is_valid(path: str | os.PathLike) -> bool:
    video_path = Path(path)
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return False
    try:
        return probe_video(video_path)["duration"] > 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return False


def expected_edl_duration(edl_path: str | os.PathLike) -> float:
    with Path(edl_path).open("r", encoding="utf-8") as file:
        entries = json.load(file)
    return sum(float(item["end"]) - float(item["start"]) for item in entries)


def probe_video(path: str | os.PathLike) -> dict:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "ffprobeに失敗しました。")
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    rate = stream.get("avg_frame_rate", "0/1")
    return {
        "codec": stream.get("codec_name"),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": float(Fraction(rate)) if rate != "0/0" else 0.0,
        "duration": float(data["format"]["duration"]),
    }


def validate_rendered_video(
    path: str | os.PathLike,
    edl_path: str | os.PathLike,
    *,
    width: int,
    height: int,
    fps: float,
    duration_tolerance: float = 0.75,
) -> dict:
    info = probe_video(path)
    expected = expected_edl_duration(edl_path)
    if info["codec"] not in {"h264", "hevc"}:
        raise ValueError(f"未対応の動画codecです: {info['codec']}")
    if (info["width"], info["height"]) != (int(width), int(height)):
        raise ValueError("完成動画の解像度が設定と一致しません。")
    if abs(info["fps"] - float(fps)) > 0.1:
        raise ValueError("完成動画のFPSが設定と一致しません。")
    if abs(info["duration"] - expected) > duration_tolerance:
        raise ValueError(
            f"完成動画の尺がEDL期待尺と一致しません: actual={info['duration']:.3f}, expected={expected:.3f}"
        )
    info["expected_duration"] = expected
    return info


def _discover_source(run_dir: Path) -> Path | None:
    project_path = run_dir / "caption_project.json"
    if _json_is_valid(project_path):
        try:
            source = Path(load_caption_project(project_path)["source_path"])
            if source.is_file():
                return source
        except (KeyError, OSError, ValueError):
            pass
    candidates = [
        path
        for path in run_dir.glob("*.mp4")
        if not path.name.startswith(("final_", "preview_"))
        and ".partial" not in path.name
    ]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def inspect_run(run_dir: str | os.PathLike) -> dict:
    run_path = Path(run_dir).resolve()
    source = _discover_source(run_path)
    paths = {
        "transcript": run_path / "transcript.json",
        "edl": run_path / "edl.json",
        "clean_transcript": run_path / "clean_transcript.json",
        "silence_analysis": run_path / "silence_analysis.json",
        "srt": run_path / "captions.srt",
        "ass": run_path / "captions.ass",
        "caption_project": run_path / "caption_project.json",
        "preview_proxy": run_path / "preview_proxy.mp4",
        "final_video": run_path / "final_jumpcut_video.mp4",
        "highlight": run_path / "highlight.json",
        "highlights": run_path / "highlights.json",
        "edit_regions": run_path / "edit_regions.json",
        "planning_manifest": run_path / "planning" / "job_manifest.json",
        "render_manifest": run_path / "planning" / "render_manifest.v1.json",
    }
    completed = {
        "acquire": source is not None,
        "transcribe": _json_is_valid(paths["transcript"], nonempty_list=True),
        "jumpcut": _json_is_valid(paths["edl"], nonempty_list=True)
        and _json_is_valid(paths["clean_transcript"], nonempty_list=True),
        "captions": _json_is_valid(paths["caption_project"])
        and paths["srt"].is_file()
        and paths["ass"].is_file(),
        "render": video_is_valid(paths["final_video"]),
        "highlight": (
            _json_is_valid(paths["highlight"])
            and _json_is_valid(paths["clean_transcript"], nonempty_list=True)
            and _highlight_is_valid(paths["highlight"], paths["clean_transcript"])
        ),
        "highlights": _highlights_is_valid(paths["highlights"], paths["clean_transcript"]),
        "edit_regions": _json_is_valid(paths["edit_regions"], nonempty_list=True),
    }
    v2_detected = paths["planning_manifest"].is_file() or paths["render_manifest"].is_file()
    v2_planning_state = None
    v2_render_summary = None
    v2_clips = []
    if v2_detected:
        try:
            from planning_manifest import load_manifest as load_planning_manifest
            from render_manifest import load_manifest as load_render_manifest

            planning_manifest = load_planning_manifest(paths["planning_manifest"])
            v2_planning_state = planning_manifest.get("stages", {}).get("contract_generation", {}).get("state")
            if paths["render_manifest"].is_file():
                render_manifest = load_render_manifest(paths["render_manifest"])
                v2_render_summary = render_manifest.get("summary")
                v2_clips = render_manifest.get("clips", [])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            v2_planning_state = "invalid"

    if v2_detected:
        completed["planning"] = v2_planning_state == "completed"
        completed["render"] = bool(v2_render_summary) and v2_render_summary.get("failed", 0) == 0 and v2_render_summary.get("skipped", 0) == 0

    return {
        "run_dir": str(run_path),
        "pipeline_version": "v2" if v2_detected else "legacy",
        "source_path": str(source) if source else None,
        "paths": {key: str(value) for key, value in paths.items()},
        "completed": completed,
        "planning_manifest_path": str(paths["planning_manifest"]) if v2_detected else None,
        "render_manifest_path": str(paths["render_manifest"]) if v2_detected else None,
        "planning_state": v2_planning_state,
        "render_summary": v2_render_summary,
        "clip_outputs": v2_clips,
    }


def _highlight_is_valid(path: Path, clean_transcript_path: Path) -> bool:
    try:
        load_valid_highlight(path, clean_transcript_path)
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _highlights_is_valid(path: Path, clean_transcript_path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return bool(load_valid_highlights(path, clean_transcript_path))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _emit(callback, stage_id: str, progress: float, message: str):
    if callback:
        callback(stage_id, max(0.0, min(1.0, float(progress))), message)


def _run_legacy_pipeline(
    video_source: str,
    is_local_file: bool = False,
    base_dir: str | os.PathLike | None = None,
    *,
    existing_run_dir: str | os.PathLike | None = None,
    resume: bool = False,
    output_width: int = 1920,
    output_height: int = 1080,
    output_fps: int = 60,
    max_chars: int = 32,
    max_lines: int = 2,
    use_llm_captions: bool = True,
    caption_model: str = "qwen2.5:14b",
    default_template_id: str = "standard",
    video_encoder: str = "auto",
    processing_device: str = "auto",
    force_render: bool = False,
    prepare_only: bool = False,
    progress_callback=None,
    cancel_event=None,
):
    processing_device = validate_processing_device(processing_device)
    processing_policy = resolve_processing_policy(processing_device)
    # Direct legacy callers that explicitly select an encoder remain compatible;
    # the unified UI passes processing_device as the authoritative policy.
    render_encoder = (
        video_encoder
        if processing_device == "auto" and video_encoder != "auto"
        else processing_policy["render_encoder"]
    )
    decode_mode = (
        "auto"
        if processing_device == "auto" and video_encoder != "h264_nvenc"
        else processing_policy["decode_mode"]
    )
    run_dir = (
        Path(existing_run_dir).resolve()
        if existing_run_dir
        else _create_run_dir(base_dir)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot = inspect_run(run_dir) if resume else None

    transcript_path = run_dir / "transcript.json"
    edl_path = run_dir / "edl.json"
    clean_transcript_path = run_dir / "clean_transcript.json"
    silence_analysis_path = run_dir / "silence_analysis.json"
    srt_path = run_dir / "captions.srt"
    ass_path = run_dir / "captions.ass"
    caption_project_path = run_dir / "caption_project.json"
    preview_proxy_path = run_dir / "preview_proxy.mp4"
    final_video_path = run_dir / "final_jumpcut_video.mp4"
    highlight_json_path = run_dir / "highlight.json"
    highlights_json_path = run_dir / "highlights.json"
    edit_regions_json_path = run_dir / "edit_regions.json"

    raise_if_cancelled(cancel_event)
    if resume and snapshot and snapshot["completed"]["acquire"]:
        video_path = Path(snapshot["source_path"])
        _emit(progress_callback, "acquire", 1.0, "既存の取得済み動画を使用")
    elif is_local_file:
        video_path = Path(video_source).expanduser().resolve()
        if not video_path.is_file():
            raise FileNotFoundError(f"ローカルファイルが見つかりません: {video_path}")
        _emit(progress_callback, "acquire", 1.0, "ローカル動画を確認")
    else:
        _emit(progress_callback, "acquire", 0.0, "YouTube動画を取得中")
        video_path = Path(
            download_video(
                video_source,
                str(run_dir),
                progress_callback=lambda value, message: _emit(
                    progress_callback, "acquire", value, message
                ),
                cancel_event=cancel_event,
            )
        )
        _emit(progress_callback, "acquire", 1.0, "動画取得完了")

    raise_if_cancelled(cancel_event)
    if resume and snapshot and snapshot["completed"]["transcribe"]:
        _emit(progress_callback, "transcribe", 1.0, "既存の文字起こしを使用")
    else:
        _emit(progress_callback, "transcribe", 0.0, "Whisper文字起こしを開始")
        transcribe_video(
            str(video_path),
            output_json=str(transcript_path),
            progress_callback=lambda value, message: _emit(
                progress_callback, "transcribe", value, message
            ),
            cancel_event=cancel_event,
            acceleration_mode=processing_policy["whisper_mode"],
        )

    raise_if_cancelled(cancel_event)
    if resume and snapshot and snapshot["completed"]["jumpcut"]:
        _emit(progress_callback, "jumpcut", 1.0, "既存のEDLを使用")
    else:
        _emit(progress_callback, "jumpcut", 0.0, "音声信号から無音区間を解析中")
        generate_silence_cut_data(
            str(video_path),
            str(transcript_path),
            str(edl_path),
            str(clean_transcript_path),
            output_analysis=str(silence_analysis_path),
        )
        _emit(progress_callback, "jumpcut", 1.0, "音声信号ベースの無音区間を解析しました（フィラー削除なし）")

    raise_if_cancelled(cancel_event)
    caption_result = None
    if resume and snapshot and snapshot["completed"]["captions"]:
        caption_project = load_caption_project(caption_project_path)
        caption_result = {
            "method": "checkpoint",
            "captions": caption_project["captions"],
            "blocks": len(caption_project["captions"]),
        }
        _emit(progress_callback, "captions", 1.0, "既存の字幕プロジェクトを使用")
    else:
        _emit(progress_callback, "captions", 0.0, "字幕を生成中")
        caption_input_path = transcript_path if prepare_only else clean_transcript_path
        caption_result = generate_subtitles(
            str(caption_input_path),
            str(srt_path),
            use_llm=use_llm_captions,
            model=caption_model,
            max_chars=max_chars,
            max_lines=max_lines,
            processing_device=processing_device,
            collect_runtime=True,
        )
        caption_project = create_caption_project(
            captions=caption_result["captions"],
            source_path=str(video_path),
            edl_path=str(edl_path),
            run_dir=str(run_dir),
            default_template_id=default_template_id,
            width=output_width,
            height=output_height,
            fps=output_fps,
            max_chars=max_chars,
            max_lines=max_lines,
        )
        save_caption_project(caption_project, caption_project_path)
        create_native_timeline_project(
            run_dir,
            captions=caption_project["captions"],
            source_path=str(video_path),
            edl_path=str(edl_path),
            timeline_transcript_path=str(transcript_path) if prepare_only else None,
            default_template_id=default_template_id,
            width=output_width,
            height=output_height,
            fps=output_fps,
            max_chars=max_chars,
            max_lines=max_lines,
        )
        write_ass_subtitles(caption_project, ass_path)
        _emit(progress_callback, "captions", 1.0, "字幕生成完了")

    # 再開時は字幕プロジェクトに保存済みの出力設定を優先する。
    caption_project = load_caption_project(caption_project_path)
    output_width = int(caption_project["output"]["width"])
    output_height = int(caption_project["output"]["height"])
    output_fps = int(caption_project["output"]["fps"])

    if prepare_only:
        source_duration = _transcript_duration(transcript_path)
        keep_regions = [{
            "id": "source_full",
            "start": 0.0,
            "end": round(source_duration, 3),
            "mode": "keep",
            "default_keep": True,
        }]
        with highlights_json_path.open("w", encoding="utf-8") as file:
            json.dump([], file, ensure_ascii=False, indent=2)
        with edit_regions_json_path.open("w", encoding="utf-8") as file:
            json.dump(keep_regions, file, ensure_ascii=False, indent=2)
        _emit(progress_callback, "render", 1.0, "エンコードを最終Exportまで延期しました")
        return {
            "run_dir": str(run_dir),
            "final_video_path": str(final_video_path),
            "preview_proxy_path": str(preview_proxy_path),
            "srt_path": str(srt_path),
            "ass_path": str(ass_path),
            "caption_project_path": str(caption_project_path),
            "highlight_json_path": str(highlight_json_path),
            "highlights_json_path": str(highlights_json_path),
            "edit_regions_json_path": str(edit_regions_json_path),
            "silence_analysis_path": str(silence_analysis_path),
            "highlight_data": [],
            "caption_result": caption_result,
            "source_path": str(video_path),
            "render_info": {"deferred": True, "processing_device": processing_device},
            "processing_device": processing_device,
            "processing_policy": processing_policy,
            "caption_ai_runtime": (caption_result or {}).get("caption_ai_runtime"),
        }

    raise_if_cancelled(cancel_event)
    if not video_is_valid(preview_proxy_path):
        _emit(progress_callback, "render", 0.0, "編集用プロキシを生成中")
        create_clip(
            str(video_path),
            str(edl_path),
            None,
            str(preview_proxy_path),
            output_width=854,
            output_height=480,
            output_fps=30,
            encoder=render_encoder,
            hardware_decode=decode_mode,
            progress_callback=lambda value, message: _emit(
                progress_callback, "render", value * 0.15, f"プロキシ: {message}"
            ),
            cancel_event=cancel_event,
        )
    else:
        _emit(progress_callback, "render", 0.15, "既存の編集用プロキシを使用")

    raise_if_cancelled(cancel_event)
    if resume and video_is_valid(final_video_path) and not force_render:
        render_info = {"encoder": "checkpoint"}
        _emit(progress_callback, "render", 1.0, "既存の完成動画を使用")
    else:
        render_info = create_clip(
            str(video_path),
            str(edl_path),
            str(ass_path),
            str(final_video_path),
            caption_project_path=str(caption_project_path),
            output_width=output_width,
            output_height=output_height,

            output_fps=output_fps,
            encoder=render_encoder,
            hardware_decode=decode_mode,
            progress_callback=lambda value, message: _emit(
                progress_callback,
                "render",
                0.15 + value * 0.85,
                f"最終出力: {message}",
            ),
            cancel_event=cancel_event,
        )
    render_info["validation"] = validate_rendered_video(
        final_video_path,
        edl_path,
        width=output_width,
        height=output_height,
        fps=output_fps,
    )

    raise_if_cancelled(cancel_event)
    if resume and _highlights_is_valid(highlights_json_path, clean_transcript_path):
        highlight_data = load_valid_highlights(highlights_json_path, clean_transcript_path)
        _emit(progress_callback, "highlight", 1.0, "既存のハイライト結果を使用")
    else:
        _emit(progress_callback, "highlight", 0.0, "ハイライトを抽出中")
        try:
            highlight_data = analyze_highlights(
                str(clean_transcript_path), str(highlight_json_path), cancel_event=cancel_event
            )
            highlight_data = load_valid_highlights(highlights_json_path, clean_transcript_path)
        except (ValueError, RuntimeError) as error:
            raise_if_cancelled(cancel_event)
            highlight_data = fallback_highlights(
                _transcript_duration(clean_transcript_path),
                f"Ollama candidate was corrected; keeping the full video: {error}",
            )
            with highlight_json_path.open("w", encoding="utf-8") as file:
                json.dump(highlight_data[0], file, ensure_ascii=False, indent=2)
            with highlights_json_path.open("w", encoding="utf-8") as file:
                json.dump(highlight_data, file, ensure_ascii=False, indent=2)
            _emit(progress_callback, "highlight", 1.0, "Highlight candidate corrected; continuing")
        with edit_regions_json_path.open("w", encoding="utf-8") as file:
            json.dump(
                build_edit_regions(highlight_data, _transcript_duration(clean_transcript_path)),
                file,
                ensure_ascii=False,
                indent=2,
            )
        _emit(progress_callback, "highlight", 1.0, "ハイライト抽出完了")

    return {
        "run_dir": str(run_dir),
        "final_video_path": str(final_video_path),
        "preview_proxy_path": str(preview_proxy_path),
        "srt_path": str(srt_path),
        "ass_path": str(ass_path),
        "caption_project_path": str(caption_project_path),
        "highlight_json_path": str(highlight_json_path),
        "highlights_json_path": str(highlights_json_path),
        "edit_regions_json_path": str(edit_regions_json_path),
        "silence_analysis_path": str(silence_analysis_path),
        "highlight_data": highlight_data,
        "caption_result": caption_result,
        "source_path": str(video_path),
        "render_info": render_info,
        "processing_device": processing_device,
        "processing_policy": processing_policy,
        "caption_ai_runtime": (caption_result or {}).get("caption_ai_runtime"),
    }


def run_legacy_pipeline(*args, **kwargs):
    """Explicit compatibility entry point for the pre-Phase-6 pipeline."""

    return _run_legacy_pipeline(*args, **kwargs)


def run_pipeline(*args, pipeline_version: str = "v2", **kwargs):
    """Dispatch new production jobs and preserve the legacy implementation."""

    if pipeline_version == "legacy":
        return _run_legacy_pipeline(*args, **kwargs)
    if pipeline_version == "v2":
        from production_pipeline import run_v2_pipeline

        return run_v2_pipeline(*args, **kwargs)
    raise ValueError(f"unsupported pipeline_version: {pipeline_version}")


def main():
    parser = argparse.ArgumentParser(description="YouTube Clip Automation Pipeline")
    parser.add_argument("source", help="YouTube video URL or Local file path")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--max-chars", type=int, default=32)
    parser.add_argument("--max-lines", type=int, default=2)
    parser.add_argument("--no-llm-captions", action="store_true")
    parser.add_argument("--caption-model", default="qwen2.5:14b")
    parser.add_argument(
        "--encoder", choices=["auto", "h264_nvenc", "libx264"], default="auto"
    )
    parser.add_argument("--resume-run")
    parser.add_argument("--pipeline-version", choices=["v2", "legacy"], default=None)
    args = parser.parse_args()
    pipeline_version = args.pipeline_version
    if pipeline_version is None and args.resume_run:
        manifest_path = Path(args.resume_run) / "job_manifest.json"
        try:
            stored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pipeline_version = stored_manifest.get("pipeline_version") or "legacy"
        except (OSError, ValueError, json.JSONDecodeError):
            pipeline_version = "legacy"
    pipeline_version = pipeline_version or "v2"
    try:
        run_pipeline(
            args.source,
            is_local_file=args.local,
            existing_run_dir=args.resume_run,
            resume=bool(args.resume_run),
            pipeline_version=pipeline_version,
            output_width=args.width,
            output_height=args.height,
            output_fps=args.fps,
            max_chars=args.max_chars,
            max_lines=args.max_lines,
            use_llm_captions=not args.no_llm_captions,
            caption_model=args.caption_model,
            video_encoder=args.encoder,
        )
        print("処理が完了しました。")
    except Exception as exc:
        print(f"\n[Error] パイプライン実行中にエラーが発生しました: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
