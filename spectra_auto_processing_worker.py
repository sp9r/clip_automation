"""Stage 8 bounded auto-processing orchestrator.

The existing caption worker remains a transcription/segmentation-only CLI.
This orchestrator prepares the selected audio range once, then runs Whisper
and signal-authoritative silence analysis concurrently. The caption result
keeps the v1 contract; silence analysis is written as a separate path-free
artifact for the editor bridge.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

from auto_caption_contract import parse_request
from silence_detection import (
    PROFILE_SETTINGS,
    SilenceDetectionCancelled,
    SilenceDetectionError,
    analyze_silences,
    apply_speech_protection,
    shift_silence_analysis,
    write_silence_analysis,
)
from spectra_caption_worker import (
    DEFAULT_CAPTION_MODEL,
    DEFAULT_CAPTION_TIMEOUT_SECONDS,
    DEFAULT_WHISPER_MODEL,
    ProgressEmitter,
    WorkerCancelled,
    WorkerFailure,
    _write_json_atomically,
    extract_source_range,
    run_worker,
)


def _load_request(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure("request JSONを読み込めません。") from exc


def _relative_words(words: list[dict], source_start_us: int) -> list[dict]:
    return [
        {
            **word,
            "startUs": int(word["startUs"]) - source_start_us,
            "endUs": int(word["endUs"]) - source_start_us,
        }
        for word in words
    ]


def _publish_results(
    caption_result: dict,
    signal_result: dict,
    *,
    output_path: str | Path,
    silence_output_path: str | Path,
) -> None:
    """Publish the two worker artifacts only after both payloads are ready."""

    output = Path(output_path)
    silence_output = Path(silence_output_path)
    output_existed = output.exists()
    silence_existed = silence_output.exists()
    try:
        write_silence_analysis(signal_result, silence_output)
        _write_json_atomically(output, caption_result)
    except Exception:
        if not output_existed:
            output.unlink(missing_ok=True)
        if not silence_existed:
            silence_output.unlink(missing_ok=True)
        raise


def run_auto_processing(
    request_input: dict,
    *,
    output_path: str | Path,
    silence_output_path: str | Path | None = None,
    progress_stream: TextIO | None = None,
    cancel_event: threading.Event | None = None,
    acceleration_mode: str = "auto",
    whisper_model: str | None = None,
    caption_model: str = DEFAULT_CAPTION_MODEL,
    caption_timeout: int = DEFAULT_CAPTION_TIMEOUT_SECONDS,
) -> dict:
    request = parse_request(request_input)
    cancel_event = cancel_event or threading.Event()
    progress = ProgressEmitter(request["jobId"], progress_stream)
    media_path = Path(request["mediaPath"]).expanduser().resolve()
    if not media_path.is_file():
        failure = WorkerFailure("入力メディアが見つかりません。", code="MEDIA_NOT_FOUND")
        progress.emit("failed", 0.0, "入力メディアが見つかりません。", error_code=failure.code)
        failure.progress_emitted = True
        raise failure
    if cancel_event.is_set():
        failure = WorkerCancelled("処理をキャンセルしました。")
        progress.emit("failed", 0.0, "処理をキャンセルしました。", error_code="CANCELLED")
        failure.progress_emitted = True
        raise failure

    progress.emit("preparing", 0.0, "選択範囲の音声を一度だけ準備中")
    with tempfile.TemporaryDirectory(prefix="spectra-auto-processing-") as temporary:
        temp_dir = Path(temporary)
        audio_path = extract_source_range(
            str(media_path),
            request["sourceStartUs"],
            request["sourceEndUs"],
            temp_dir,
            cancel_event,
            progress.emit,
        )
        effective_acceleration_mode = request.get("accelerationMode", acceleration_mode)
        if request.get("silenceCut", "youtube") == "off":
            caption_result = run_worker(
                request,
                output_path=None,
                progress_stream=None,
                cancel_event=cancel_event,
                prepared_audio_path=audio_path,
                progress_callback=lambda payload: progress.emit(
                    str(payload.get("state", "transcribing")),
                    float(payload.get("ratio", 0.15)),
                    str(payload.get("message", "Whisper文字起こし中")),
                    progress_mode=str(payload.get("progressMode", "indeterminate")),
                    stage_ratio=payload.get("stageRatio"),
                    diagnostics=payload.get("diagnostics"),
                ),
                emit_completed=False,
                whisper_model=whisper_model,
                caption_model=caption_model,
                caption_timeout=caption_timeout,
                acceleration_mode=effective_acceleration_mode,
            )
            if cancel_event.is_set():
                raise WorkerCancelled("処理をキャンセルしました。")
            _write_json_atomically(Path(output_path), caption_result)
            progress.emit("segmenting", 0.92, "字幕区切りを生成しました")
            progress.emit("completed", 1.0, "自動字幕を完了しました（無音カットなし）")
            return caption_result
        if silence_output_path is None:
            raise WorkerFailure("無音解析の出力先が指定されていません。")
        progress.emit("transcribing", 0.15, "Whisper文字起こしを開始")
        progress.emit("silence_analyzing", 0.15, "音声信号から無音区間を解析中")
        emit_lock = threading.Lock()

        def emit_caption(payload: dict) -> None:
            with emit_lock:
                progress.emit(
                    str(payload.get("state", "transcribing")),
                    float(payload.get("ratio", 0.15)),
                    str(payload.get("message", "Whisper文字起こし中")),
                    progress_mode=str(payload.get("progressMode", "indeterminate")),
                    stage_ratio=payload.get("stageRatio"),
                    diagnostics=payload.get("diagnostics"),
                )

        def analyze_signal() -> dict:
            profile = request.get("silenceCut", "youtube")
            result = analyze_silences(
                audio_path,
                profile=profile,
                min_silence_us=request.get("silenceMinUs", PROFILE_SETTINGS[profile]["min_silence_us"]),
                cancel_event=cancel_event,
            )
            with emit_lock:
                progress.emit("silence_analyzing", 0.70, "無音解析が完了しました")
            return result

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="spectra-auto") as executor:
            silence_future = executor.submit(analyze_signal)
            try:
                caption_result = run_worker(
                    request,
                    output_path=None,
                    progress_stream=None,
                    cancel_event=cancel_event,
                    prepared_audio_path=audio_path,
                    progress_callback=emit_caption,
                    emit_completed=False,
                    whisper_model=whisper_model,
                    caption_model=caption_model,
                    caption_timeout=caption_timeout,
                    acceleration_mode=effective_acceleration_mode,
                )
                if cancel_event.is_set():
                    raise WorkerCancelled("処理をキャンセルしました。")
                signal_result = silence_future.result()
            except Exception:
                cancel_event.set()
                silence_future.cancel()
                raise

        relative_words = _relative_words(caption_result["rawWords"], request["sourceStartUs"])
        signal_result = apply_speech_protection(signal_result, relative_words)
        signal_result = shift_silence_analysis(signal_result, request["sourceStartUs"])
        signal_result["diagnostics"].append({
            "level": "info",
            "code": "PARALLEL_SILENCE_ANALYSIS",
            "message": "Signal silence analysis overlapped Whisper transcription after one shared range extraction.",
        })
        _publish_results(
            caption_result,
            signal_result,
            output_path=output_path,
            silence_output_path=silence_output_path,
        )
        progress.emit("segmenting", 0.92, "字幕区切りを生成しました")
        progress.emit("completed", 1.0, "文字起こしと無音解析を完了しました")
        return caption_result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spectra parallel auto-processing worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--silence-output")
    parser.add_argument("--whisper-model", default=None)
    parser.add_argument("--caption-model", default=DEFAULT_CAPTION_MODEL)
    parser.add_argument("--caption-timeout", type=int, default=DEFAULT_CAPTION_TIMEOUT_SECONDS)
    parser.add_argument("--acceleration-mode", choices=("auto", "gpu", "cpu"), default="auto")
    args = parser.parse_args(argv)
    cancel_event = threading.Event()

    def request_cancel(_signum: int, _frame: object) -> None:
        cancel_event.set()

    signal.signal(signal.SIGINT, request_cancel)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_cancel)
    progress_file: TextIO | None = None
    job_id = "unknown"
    try:
        request_payload = _load_request(Path(args.request))
        if isinstance(request_payload.get("jobId"), str):
            job_id = request_payload["jobId"]
        progress_file = sys.stdout
        run_auto_processing(
            request_payload,
            output_path=args.output,
            silence_output_path=args.silence_output,
            progress_stream=progress_file,
            cancel_event=cancel_event,
            whisper_model=args.whisper_model,
            caption_model=args.caption_model,
            caption_timeout=args.caption_timeout,
            acceleration_mode=args.acceleration_mode,
        )
        return 0
    except (WorkerCancelled, SilenceDetectionCancelled) as exc:
        if not getattr(exc, "progress_emitted", False):
            ProgressEmitter(job_id, progress_file).emit("failed", 0.0, "処理をキャンセルしました。", error_code="CANCELLED")
        print(str(exc), file=sys.stderr)
        return 2
    except SilenceDetectionError as exc:
        ProgressEmitter(job_id, progress_file).emit("failed", 0.0, "無音解析に失敗しました。", error_code=exc.code)
        print(str(exc), file=sys.stderr)
        return 1
    except WorkerFailure as exc:
        if not getattr(exc, "progress_emitted", False):
            ProgressEmitter(job_id, progress_file).emit("failed", 0.0, "自動処理workerに失敗しました。", error_code=exc.code)
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        ProgressEmitter(job_id, progress_file).emit("failed", 0.0, "自動処理workerに失敗しました。", error_code="WORKER_FAILED")
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
