"""Dedicated Spectra auto-caption worker.

The worker intentionally owns only bounded transcription and caption
segmentation.  It does not import or execute jumpcut, planning, highlight,
render, publish, or the legacy editor pipeline.
"""

from __future__ import annotations

import argparse
import contextlib
import caption_llm
import gc
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, TextIO

from auto_caption_contract import (
    RESULT_VERSION,
    AutoCaptionContractError,
    parse_request,
    parse_result,
)
from japanese_units import merge_bunsetsu_units
from caption import HARD_MAX_DURATION_SECONDS, deterministic_failsafe_captions, generate_subtitles, get_ollama_runtime_diagnostic
from job_control import PipelineCancelled
from transcribe import AccelerationMode, WhisperAccelerationError, WhisperModelCache, transcribe_video


PROGRESS_VERSION = "spectra.auto-caption.progress.v1"
DEFAULT_WHISPER_MODEL = "large-v3"
# 2026-10-21 comparison on real Japanese speech: the Japanese fine-tune
# recognised proper nouns and dropped fewer words than large-v3.
JAPANESE_WHISPER_MODEL = "aikobin/whisper-ja-1.5B-ct2"
DEFAULT_CAPTION_MODEL = caption_llm.default_model("qwen2.5:14b")


def whisper_model_for_language(language: str, requested: str | None = None) -> str:
    if requested:
        return requested
    return JAPANESE_WHISPER_MODEL if language == "ja" else DEFAULT_WHISPER_MODEL


def _model_label(model: object) -> str:
    """Diagnostics carry a path-free model name, also for Hugging Face IDs."""
    text = str(model)
    if "/" in text and not text.startswith(("/", ".")) and ":" not in text and "\\" not in text:
        text = text.rsplit("/", 1)[-1]
    return text


# The local Qwen3.6 server is slower per request than a small Ollama model,
# especially when RAM is short; waiting longer keeps the AI segmentation.
DEFAULT_CAPTION_TIMEOUT_SECONDS = 300 if caption_llm.configured() else 120


class WorkerCancelled(RuntimeError):
    """Raised when the worker receives cancellation or parent termination."""


class WorkerFailure(RuntimeError):
    """Raised for a worker failure that must not publish a result."""

    def __init__(self, message: str, *, code: str = "WORKER_FAILED", diagnostics: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or []
        self.artifact_path: str | None = None


def _caption_failure_code(error: ValueError) -> str:
    message = str(error)
    if "発話" in message or "word" in message.lower() and "ありません" in message:
        return "NO_SPEECH"
    return "CAPTION_SEGMENTATION_FAILED"


def _whisper_failure_code(error: ValueError) -> str:
    return "NO_SPEECH" if "発話" in str(error) else "WHISPER_TRANSCRIPTION_FAILED"


def _seconds_decimal(microseconds: int) -> str:
    return f"{microseconds // 1_000_000}.{microseconds % 1_000_000:06d}"


def build_range_extract_command(
    media_path: str,
    source_start_us: int,
    source_end_us: int,
    output_path: str,
    *,
    ffmpeg_path: str | None = None,
) -> list[str]:
    """Build an argv-only accurate audio extraction command."""

    if source_end_us <= source_start_us:
        raise ValueError("source range must have positive duration")
    executable = ffmpeg_path or os.environ.get("SPECTRA_FFMPEG") or shutil.which("ffmpeg")
    if not executable:
        raise WorkerFailure("FFmpegが見つかりません。")
    return [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        media_path,
        "-ss",
        _seconds_decimal(source_start_us),
        "-t",
        _seconds_decimal(source_end_us - source_start_us),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        output_path,
    ]


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def extract_source_range(
    media_path: str,
    source_start_us: int,
    source_end_us: int,
    temp_dir: Path,
    cancel_event: threading.Event,
    progress: Callable[[str, float, str], None],
    *,
    popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> Path:
    """Decode only the requested source range into a temporary mono WAV."""

    output_path = temp_dir / "source-range.wav"
    command = build_range_extract_command(media_path, source_start_us, source_end_us, str(output_path))
    progress("preparing", 0.10, "selected source rangeを準備中")
    process = popen_factory(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    while process.poll() is None:
        if cancel_event.is_set():
            _terminate_process(process)
            raise WorkerCancelled("処理をキャンセルしました。")
        time.sleep(0.05)
    stdout, stderr = process.communicate()
    if cancel_event.is_set():
        raise WorkerCancelled("処理をキャンセルしました。")
    if process.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        # FFmpeg stderr can contain the absolute input/output path. Keep the
        # process diagnostic private to avoid leaking local paths through the
        # worker contract or its machine-readable error stream.
        stderr_text = stderr.decode("utf-8", errors="replace").lower()
        if "matches no streams" in stderr_text or "does not contain any stream" in stderr_text:
            raise WorkerFailure("メディアに音声トラックがありません。", code="NO_AUDIO_STREAM")
        raise WorkerFailure("音声区間の抽出に失敗しました。")
    return output_path


class ProgressEmitter:
    def __init__(
        self,
        job_id: str,
        stream: TextIO | None = None,
        *,
        close_stream: bool = False,
        callback: Callable[[dict], None] | None = None,
    ):
        self.job_id = job_id
        self.stream = stream
        self.close_stream = close_stream
        self.callback = callback

    def emit(
        self,
        state: str,
        ratio: float,
        message: str,
        *,
        progress_mode: str = "indeterminate",
        stage_ratio: float | None = None,
        error_code: str | None = None,
        diagnostics: list[dict] | None = None,
    ) -> None:
        payload = {
            "version": PROGRESS_VERSION,
            "jobId": self.job_id,
            "state": state,
            "ratio": max(0.0, min(1.0, float(ratio))),
            "progressMode": progress_mode if progress_mode in {"determinate", "indeterminate"} else "indeterminate",
            "message": str(message),
        }
        if stage_ratio is not None:
            payload["stageRatio"] = max(0.0, min(1.0, float(stage_ratio)))
        if error_code:
            payload["errorCode"] = error_code
        if diagnostics is not None:
            payload["diagnostics"] = diagnostics
        if self.callback is not None:
            self.callback(payload)
        if self.stream is None:
            return
        self.stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()

    def close(self) -> None:
        if self.close_stream and self.stream is not None:
            self.stream.close()


def _microseconds(seconds: object) -> int:
    value = float(seconds)
    if not math.isfinite(value) or value < 0:
        raise WorkerFailure("Whisper timingが不正です。", code="WHISPER_TIMING_INVALID")
    return int(round(value * 1_000_000))


def _path_free_value(value: object, *, fallback: str) -> str:
    text = str(value)
    if not text or "/" in text or "\\" in text or ":" in text:
        return fallback
    return text[:256]


def _safe_model_value(value: object, *, fallback: str = "configured-model") -> str:
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_.:@+\-]{1,128}", text):
        return text
    return fallback


def _sanitize_failure_text(value: object) -> str:
    text = str(value)
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"']+|\\\\[^\s\"']+|/(?:Users|home|private|tmp|var)/[^\s\"']+", "[redacted]", text)
    text = re.sub(r"(?:password|secret|token|api[_ -]?key|credential)\s*[=:]\s*[^\s,;]+", "[redacted]", text, flags=re.IGNORECASE)
    return text[:20_000]


def _failure_artifacts_enabled() -> bool:
    configured = os.environ.get("SPECTRA_AUTO_CAPTION_FAILURE_ARTIFACTS")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes", "on"}
    return os.environ.get("SPECTRA_PRODUCTION", "").lower() not in {"1", "true", "yes", "on"}


def _safe_job_directory(job_id: object) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]", "_", str(job_id))
    return value[:128] or "unknown"


def _copy_bounded(source: Path, target: Path, maximum: int = 8 * 1024 * 1024) -> None:
    if not source.is_file() or source.stat().st_size > maximum:
        return
    shutil.copyfile(source, target)


def _write_failure_artifact(
    *,
    request: dict,
    temp_dir: Path,
    exception: BaseException,
    progress_events: list[dict],
    transcript_path: Path | None,
    captions_path: Path | None,
    caption_result: dict | None,
    stderr_tail: str,
) -> Path | None:
    """Persist bounded local evidence before the worker temp dir is removed."""

    if not _failure_artifacts_enabled():
        return None
    try:
        root = Path(tempfile.gettempdir()) / "spectra-auto-caption-failures"
        target = root / _safe_job_directory(request.get("jobId", "unknown"))
        target.mkdir(parents=True, exist_ok=True)
        safe_request = {key: value for key, value in request.items() if key != "mediaPath"}
        safe_request["mediaPath"] = "[redacted]"
        failure_payload = {
            "version": "spectra.auto-caption.failure-artifact.v1",
            "jobId": safe_request.get("jobId", "unknown"),
            "request": safe_request,
            "exceptionType": type(exception).__name__,
            "exceptionMessage": _sanitize_failure_text(exception),
            "errorCode": getattr(exception, "code", "WORKER_FAILED"),
            "traceback": _sanitize_failure_text("".join(traceback.format_exception(exception))),
            "lastProgress": progress_events[-1] if progress_events else None,
            "rawWordCount": None,
        }
        if transcript_path and transcript_path.is_file():
            try:
                transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
                failure_payload["rawWordCount"] = sum(len(segment.get("words") or []) for segment in transcript if isinstance(segment, dict))
            except Exception:
                pass
        _write_json_atomically(target / "failure.json", failure_payload)
        if transcript_path:
            _copy_bounded(transcript_path, target / "transcript.json")
        if captions_path:
            _copy_bounded(captions_path, target / "captions.srt")
        if caption_result is not None:
            _write_json_atomically(target / "caption-result.json", caption_result)
        (target / "worker-stderr-tail.txt").write_text(_sanitize_failure_text(stderr_tail), encoding="utf-8")
        for old in sorted(root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)[3:]:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        return target
    except Exception:
        # Diagnostics must never replace the original worker exception.
        return None


class _FailureCaptureDirectory:
    def __init__(self, context: object, on_failure: Callable[[Path, BaseException], None]):
        self.context = context
        self.on_failure = on_failure
        self.path: Path | None = None

    def __enter__(self) -> str:
        value = self.context.__enter__()
        self.path = Path(value)
        return str(value)

    def __exit__(self, exception_type: object, exception: BaseException | None, traceback_value: object) -> bool:
        if exception is not None and self.path is not None:
            try:
                self.on_failure(self.path, exception)
            except Exception:
                pass
        return bool(self.context.__exit__(exception_type, exception, traceback_value))


class _TailWriter:
    def __init__(self, sink: TextIO, maximum: int = 32_000):
        self.sink = sink
        self.maximum = maximum
        self.parts: list[str] = []

    def write(self, value: str) -> int:
        self.sink.write(value)
        self.sink.flush()
        self.parts.append(value)
        combined = "".join(self.parts)
        if len(combined) > self.maximum:
            combined = combined[-self.maximum:]
        self.parts = [combined]
        return len(value)

    def flush(self) -> None:
        self.sink.flush()

    def value(self) -> str:
        return "".join(self.parts)


def _runtime_diagnostic_entries(runtime: dict | None) -> list[dict]:
    if not runtime:
        return []
    values = {
        "WHISPER_DEVICE": f"device={_path_free_value(runtime.get('device'), fallback='unknown')}",
        "WHISPER_COMPUTE_TYPE": f"computeType={_path_free_value(runtime.get('computeType'), fallback='unknown')}",
        "WHISPER_MODEL": f"model={_path_free_value(_model_label(runtime.get('model')), fallback='unknown')}",
        "WHISPER_CUDA_AVAILABILITY": f"cudaAvailable={'true' if runtime.get('cudaAvailable') is True else 'false'}",
        "WHISPER_FALLBACK": f"fallbackReason={_path_free_value(runtime.get('fallbackReason'), fallback='unknown')}",
        "WHISPER_CACHE": f"cache={'warm' if runtime.get('cacheHit') is True else 'cold'}",
    }
    return [{"level": "info", "code": code, "message": message} for code, message in values.items()]


def _caption_runtime_diagnostic_entries(runtime: dict | None) -> list[dict]:
    if not runtime:
        return []
    status = runtime.get("status")
    if status not in {"gpu", "cpu", "mixed", "available", "unavailable"}:
        status = "unavailable"
    model = _safe_model_value(runtime.get("model"))
    entries = [
        {"level": "info" if status != "unavailable" else "warning", "code": "CAPTION_AI_RUNTIME", "message": f"status={status}"},
        {"level": "info", "code": "CAPTION_AI_MODEL", "message": f"model={model}"},
    ]
    if runtime.get("backend") == "openai-compatible":
        entries.extend([
            {"level": "info", "code": "CAPTION_AI_BACKEND", "message": "backend=openai-compatible"},
            {"level": "info", "code": "CAPTION_AI_ACCELERATION", "message": "acceleration=unmeasured"},
        ])
    for key, code_suffix, label in (("sizeBytes", "MODEL_SIZE_BYTES", "modelBytes"), ("vramBytes", "VRAM_BYTES", "vramBytes")):
        value = runtime.get(key)
        if isinstance(value, int) and value >= 0:
            entries.append({"level": "info", "code": f"CAPTION_AI_{code_suffix}", "message": f"{label}={value}"})
    reason = _path_free_value(runtime.get("reason") or "none", fallback="none")
    if status == "unavailable" or reason != "none":
        entries.append({"level": "warning", "code": "CAPTION_AI_RUNTIME_REASON", "message": f"reason={reason}"})
    return entries


def _raw_words(transcript: list[dict], source_start_us: int, source_end_us: int | None = None) -> list[dict]:
    words = []
    for segment in transcript:
        for word in segment.get("words") or []:
            text = str(word.get("word", ""))
            if not text:
                continue
            start_us = source_start_us + _microseconds(word.get("start", 0.0))
            end_us = source_start_us + _microseconds(word.get("end", word.get("start", 0.0)))
            if end_us < start_us:
                raise WorkerFailure("Whisper word timingが逆行しています。", code="WHISPER_TIMING_INVALID")
            if start_us < source_start_us or source_end_us is not None and end_us > source_end_us:
                raise WorkerFailure("Whisper word timingが選択source range外です。", code="WHISPER_TIMING_INVALID")
            if words and start_us < words[-1]["startUs"]:
                raise WorkerFailure("Whisper word timingが時系列順ではありません。", code="WHISPER_TIMING_INVALID")
            item = {
                "id": f"source-word:{len(words)}",
                "text": text,
                "startUs": start_us,
                "endUs": end_us,
            }
            probability = word.get("probability")
            if probability is not None:
                item["confidence"] = float(probability)
            words.append(item)
    if not words:
        raise WorkerFailure("発話を検出できませんでした。")
    return words


def _deterministic_caption_result(
    transcript: list[dict],
    *,
    source_start_us: int,
    source_end_us: int,
    max_chars: int,
) -> dict:
    raw_words = _raw_words(transcript, source_start_us, source_end_us)
    words = [{
        "start": (word["startUs"] - source_start_us) / 1_000_000,
        "end": (word["endUs"] - source_start_us) / 1_000_000,
        "word": word["text"],
    } for word in raw_words]
    return {
        "method": "failsafe",
        "captions": deterministic_failsafe_captions(
            words,
            max_chars=max_chars,
            max_duration=HARD_MAX_DURATION_SECONDS,
        ),
        "diagnostics": [{
            "level": "warning",
            "code": "CAPTION_RESULT_ASSEMBLY_FALLBACK",
            "message": "Caption result assembly failed; deterministic lossless captions were rebuilt from raw Whisper words.",
        }],
    }


def _method_metadata(method: str, segmentation_mode: str) -> tuple[str, bool, str | None, list[dict]]:
    if method == "llm":
        return "llm", False, None, []
    if method == "llm+fallback":
        return "rule_fallback", True, "LLM semantic segmentation was unavailable; rule segmentation was used.", [{
            "level": "warning",
            "code": "LLM_SEGMENTATION_FALLBACK",
            "message": "LLM semantic segmentation was unavailable; rule segmentation was used.",
        }]
    if method in {"failsafe", "llm+failsafe"}:
        return "rule_fallback", True, "Deterministic lossless caption segmentation was used.", []
    if method == "heuristic" and segmentation_mode == "rule":
        return "caption_algorithm", False, None, [{
            "level": "info",
            "code": "RULE_SEGMENTATION",
            "message": "Rule-based caption segmentation was explicitly requested.",
        }]
    raise WorkerFailure("caption segmentation method is unsupported")


def _repair_point_caption_intervals(
    captions: list[dict],
    *,
    source_start_us: int,
    source_end_us: int,
) -> bool:
    """Give point-word-only captions a positive display interval.

    Whisper can emit a run of words with identical start/end timestamps. The
    raw word timings remain untouched; this only partitions the available
    interval before the next timed caption so the result contract remains
    renderable. If no positive interval exists, fail closed instead of
    inventing an acoustic boundary.
    """

    repaired = False
    index = 0
    while index < len(captions):
        current = captions[index]
        if current["endUs"] > current["startUs"]:
            index += 1
            continue

        block_start = index
        while index < len(captions) and captions[index]["endUs"] <= captions[index]["startUs"]:
            index += 1
        block_end = index
        left_us = captions[block_start - 1]["endUs"] if block_start > 0 else source_start_us
        right_us = source_end_us
        if block_end < len(captions):
            next_caption = captions[block_end]
            right_us = next_caption["startUs"] if next_caption["startUs"] > left_us else next_caption["endUs"]
        span_us = right_us - left_us
        count = block_end - block_start
        if span_us < count or span_us <= 0:
            raise WorkerFailure("caption point-word run has no positive display interval", code="CAPTION_POINT_TIMING_FAILED")
        for offset, caption_index in enumerate(range(block_start, block_end)):
            captions[caption_index]["startUs"] = left_us + (span_us * offset) // count
            captions[caption_index]["endUs"] = left_us + (span_us * (offset + 1)) // count
        repaired = True
    return repaired


def _build_result(
    request: dict,
    transcript: list[dict],
    caption_result: dict,
    *,
    whisper_model: str,
    whisper_runtime: dict | None = None,
    caption_runtime: dict | None = None,
    extra_diagnostics: list[dict] | None = None,
) -> dict:
    source_start_us = request["sourceStartUs"]
    source_end_us = request["sourceEndUs"]
    raw_words = _raw_words(transcript, source_start_us, source_end_us)
    provenance, fallback_used, fallback_reason, diagnostics = _method_metadata(
        str(caption_result.get("method", "")), request["segmentationMode"]
    )
    diagnostics = [{
        "level": "info",
        "code": "NO_FINE_ACOUSTIC_ALIGNMENT",
        "message": "Whisper word timing is preserved; character-level acoustic alignment is not available.",
    }, *(extra_diagnostics or []), *diagnostics]
    for item in caption_result.get("diagnostics") or []:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        message = item.get("message")
        level = item.get("level", "warning")
        if not isinstance(code, str) or not isinstance(message, str) or level not in {"info", "warning", "error"}:
            continue
        if any(existing["code"] == code for existing in diagnostics):
            continue
        diagnostics.append({"level": level, "code": code, "message": message})
    diagnostics.extend(_runtime_diagnostic_entries(whisper_runtime))
    diagnostics.extend(_caption_runtime_diagnostic_entries(caption_runtime))
    captions = []
    for index, caption in enumerate(caption_result.get("captions") or []):
        start_word_id = caption.get("start_word_id")
        end_word_id = caption.get("end_word_id")
        if not isinstance(start_word_id, int) or not isinstance(end_word_id, int):
            raise WorkerFailure("caption word ownershipがありません。", code="CAPTION_OWNERSHIP_FAILED")
        if start_word_id < 0 or end_word_id < start_word_id or end_word_id >= len(raw_words):
            raise WorkerFailure("caption word ownershipの範囲が不正です。", code="CAPTION_OWNERSHIP_FAILED")
        start_us = source_start_us + _microseconds(caption.get("start", 0.0))
        end_us = source_start_us + _microseconds(caption.get("end", 0.0))
        captions.append({
            "id": f"caption:{index}",
            "text": str(caption.get("text", "")),
            "sourceWordIds": [word["id"] for word in raw_words[start_word_id : end_word_id + 1]],
            "startUs": start_us,
            "endUs": end_us,
            "textStatus": str(caption.get("text_status", "as_transcribed")),
            "provenance": provenance,
        })
    if not captions:
        raise WorkerFailure("caption algorithm produced no usable captions", code="CAPTION_RESULT_ASSEMBLY_FAILED")
    owned_word_ids = [word_id for caption in captions for word_id in caption["sourceWordIds"]]
    expected_word_ids = [word["id"] for word in raw_words]
    if owned_word_ids != expected_word_ids:
        raise WorkerFailure("caption source word ownership is incomplete or duplicated", code="CAPTION_OWNERSHIP_FAILED")
    if _repair_point_caption_intervals(
        captions,
        source_start_us=source_start_us,
        source_end_us=source_end_us,
    ):
        diagnostics.append({
            "level": "warning",
            "code": "POINT_WORD_DISPLAY_TIMING",
            "message": "Whisper point-word captions received a derived positive display interval; raw word timing remains unchanged.",
        })
    result = {
        "version": RESULT_VERSION,
        "jobId": request["jobId"],
        "sourceStartUs": source_start_us,
        "sourceEndUs": source_end_us,
        "language": request["language"],
        "provider": "faster-whisper+caption.py",
        "model": _path_free_value(_model_label(whisper_model), fallback="custom-model"),
        "rawWords": raw_words,
        "captions": captions,
        "diagnostics": diagnostics,
        "fallback": {"used": fallback_used, **({"reason": fallback_reason} if fallback_reason else {})},
    }
    try:
        return parse_result(result)
    except AutoCaptionContractError as exc:
        raise WorkerFailure(f"worker result validation failed: {exc}", code="CAPTION_RESULT_CONTRACT_FAILED") from exc


def _write_json_atomically(target: Path, payload: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def run_worker(
    request_input: dict,
    *,
    output_path: str | Path | None = None,
    progress_stream: TextIO | None = None,
    cancel_event: threading.Event | None = None,
    transcriber: Callable[..., list[dict]] = transcribe_video,
    range_extractor: Callable[..., Path] = extract_source_range,
    whisper_model: str | None = None,
    caption_model: str = DEFAULT_CAPTION_MODEL,
    caption_timeout: int = DEFAULT_CAPTION_TIMEOUT_SECONDS,
    acceleration_mode: AccelerationMode = "auto",
    model_cache: WhisperModelCache | None = None,
    collect_caption_runtime: bool = False,
    prepared_audio_path: str | Path | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    emit_completed: bool = True,
) -> dict:
    request = parse_request(request_input)
    effective_acceleration_mode = request.get("accelerationMode", acceleration_mode)
    whisper_model = whisper_model_for_language(request["language"], whisper_model)
    use_llm = request["segmentationMode"] in {"auto", "llm"}
    # Whisper and the local caption LLM do not share the GPU. Free the GPU
    # for Whisper first; the LLM is loaded again after Whisper is released.
    local_llm = use_llm and caption_llm.configured()
    cancel_event = cancel_event or threading.Event()
    progress_events: list[dict] = []

    def capture_progress(payload: dict) -> None:
        progress_events.append(payload)
        del progress_events[:-80]
        if progress_callback is not None:
            progress_callback(payload)

    progress = ProgressEmitter(request["jobId"], progress_stream, callback=capture_progress)
    media_path = Path(request["mediaPath"]).expanduser().resolve()
    if not media_path.is_file():
        progress.emit("failed", 0.0, "入力メディアが見つかりません。", error_code="MEDIA_NOT_FOUND")
        raise WorkerFailure("入力メディアが見つかりません。")
    if cancel_event.is_set():
        progress.emit("failed", 0.0, "処理をキャンセルしました。", error_code="CANCELLED")
        raise WorkerCancelled("処理をキャンセルしました。")

    transcript_path: Path | None = None
    captions_path: Path | None = None
    caption_result: dict | None = None
    stderr_tail = _TailWriter(sys.stderr)

    def capture_failure(temp_dir: Path, exception: BaseException) -> None:
        artifact_path = _write_failure_artifact(
            request=request,
            temp_dir=temp_dir,
            exception=exception,
            progress_events=progress_events,
            transcript_path=transcript_path,
            captions_path=captions_path,
            caption_result=caption_result,
            stderr_tail=stderr_tail.value(),
        )
        if artifact_path is not None and isinstance(exception, WorkerFailure):
            exception.artifact_path = str(artifact_path)

    temporary_context = _FailureCaptureDirectory((
        tempfile.TemporaryDirectory(prefix="spectra-caption-worker-")
        if prepared_audio_path is None
        else contextlib.nullcontext(str(Path(prepared_audio_path).resolve().parent))
    ), capture_failure)
    with temporary_context as temporary:
        temp_dir = Path(temporary)
        progress.emit("preparing", 0.0, "自動字幕workerを準備中")
        if prepared_audio_path is None:
            try:
                audio_path = range_extractor(
                    str(media_path), request["sourceStartUs"], request["sourceEndUs"], temp_dir, cancel_event, progress.emit
                )
            except PipelineCancelled as exc:
                raise WorkerCancelled("処理をキャンセルしました。") from exc
        else:
            audio_path = Path(prepared_audio_path).resolve()
            if not audio_path.is_file() or audio_path.stat().st_size <= 0:
                raise WorkerFailure("準備済み音声が見つかりません。")
        if cancel_event.is_set():
            raise WorkerCancelled("処理をキャンセルしました。")
        transcript_path = temp_dir / "transcript.json"
        if local_llm and effective_acceleration_mode != "cpu":
            progress.emit("preparing", 0.12, "文字起こし用にGPUを空けています")
            released = caption_llm.release_gpu(caption_model)
            progress.emit("preparing", 0.14, "文字起こし用にGPUを空けています", diagnostics=[{"level": "info", "code": "CAPTION_AI_GPU_HANDOFF", "message": f"release={released}"}])
        progress.emit("transcribing", 0.15, "Whisperで単語時刻を取得中")

        def report_transcription(ratio: float, _message: str) -> None:
            measured_ratio = max(0.0, min(1.0, ratio))
            progress.emit(
                "transcribing",
                0.15 + measured_ratio * 0.55,
                "Whisperで単語時刻を取得中",
                progress_mode="determinate",
                stage_ratio=measured_ratio,
            )

        whisper_runtime: dict | None = None

        def report_runtime(runtime: dict) -> None:
            nonlocal whisper_runtime
            whisper_runtime = dict(runtime)
            progress.emit(
                "transcribing",
                0.15,
                "Whisperで単語時刻を取得中",
                diagnostics=_runtime_diagnostic_entries(whisper_runtime),
            )

        try:
            with contextlib.redirect_stdout(stderr_tail):
                transcript = transcriber(
                    str(audio_path),
                    model_size=whisper_model,
                    output_json=None,
                    # The UI contract uses "auto" as a portable sentinel.  The
                    # faster-whisper API uses None for language detection.
                    language=None if request["language"] == "auto" else request["language"],
                    progress_callback=report_transcription,
                    cancel_event=cancel_event,
                    acceleration_mode=effective_acceleration_mode,
                    diagnostics_callback=report_runtime,
                    model_cache=model_cache,
                )
        except PipelineCancelled as exc:
            raise WorkerCancelled("処理をキャンセルしました。") from exc
        except WhisperAccelerationError as exc:
            progress.emit(
                "failed",
                0.15,
                "Whisper acceleration policy failed.",
                error_code=exc.code,
                diagnostics=_runtime_diagnostic_entries(exc.diagnostics),
            )
            raise WorkerFailure(str(exc), code=exc.code) from exc
        except ValueError as exc:
            error_code = _whisper_failure_code(exc)
            progress.emit(
                "failed",
                0.70,
                "文字起こしに失敗しました。",
                error_code=error_code,
                diagnostics=[{
                    "level": "error",
                    "code": error_code,
                    "message": "Whisper returned no source words." if error_code == "NO_SPEECH" else "Whisper transcription failed.",
                }],
            )
            failure = WorkerFailure("文字起こしに失敗しました。", code=error_code)
            failure.progress_emitted = True
            raise failure from exc
        if cancel_event.is_set():
            raise WorkerCancelled("処理をキャンセルしました。")
        if request["language"] == "ja":
            # Whisperの日本語wordは1文字単位が多いので、字幕が語の途中で切れないよう文節へまとめる。
            # 単位は1行に収まる長さを上限にし、形態素の境界でだけ分ける。非表示の句読点は数えない。
            transcript = merge_bunsetsu_units(
                transcript,
                min(request["maxChars"], request.get("lineCapacity", request["maxChars"])),
                request.get("hidePunctuation", True),
            )
        _write_json_atomically(transcript_path, transcript)
        caption_runtime = None
        if local_llm:
            if model_cache is not None:
                model_cache.clear()
            gc.collect()
            progress.emit("segmenting", 0.72, "字幕AIを読み込み中")
            try:
                caption_llm.ensure_loaded(caption_model)
            except caption_llm.CaptionLlmError:
                # generate_subtitles falls back to the rule segmentation at once.
                progress.emit("segmenting", 0.72, "字幕AIを読み込めないためルール方式で区切ります", diagnostics=[{"level": "warning", "code": "CAPTION_AI_LOAD_FAILED", "message": "The local caption LLM could not be loaded."}])
            if cancel_event.is_set():
                raise WorkerCancelled("処理をキャンセルしました。")
        progress.emit("segmenting", 0.72, "字幕区切りを生成中")
        captions_path = temp_dir / "captions.srt"
        try:
            with contextlib.redirect_stdout(stderr_tail):
                caption_result = generate_subtitles(
                    str(transcript_path),
                    str(captions_path),
                    use_llm=use_llm,
                    model=caption_model,
                    timeout=caption_timeout,
                    max_chars=request["maxChars"],
                    max_lines=request.get("maxLines", 2),
                    hide_punctuation=request.get("hidePunctuation", True),
                    # 動画のファイル名は話題の手がかりになる（ローカルLLMにだけ渡す）。
                    topic=media_path.stem,
                    **({"line_capacity": request["lineCapacity"]} if "lineCapacity" in request else {}),
                    # 字幕ライター(思考あり)は長くかかるので、まとまりごとに進みを出す。
                    progress_callback=lambda done, total: progress.emit(
                        "segmenting",
                        0.72 + 0.17 * done / max(1, total),
                        f"字幕を作成中（{done}/{total}）",
                        progress_mode="determinate",
                        stage_ratio=done / max(1, total),
                    ),
                )
        except ValueError as exc:
            error_code = _caption_failure_code(exc)
            progress.emit(
                "failed",
                0.72,
                "字幕区切りに失敗しました。",
                error_code=error_code,
                diagnostics=[{
                    "level": "error",
                    "code": error_code,
                    "message": "Whisper source words were unavailable." if error_code == "NO_SPEECH" else "Caption segmentation could not preserve source word ownership.",
                }],
            )
            failure = WorkerFailure("字幕区切りに失敗しました。", code=error_code)
            failure.progress_emitted = True
            raise failure from exc
        _write_json_atomically(temp_dir / "caption-result.json", caption_result)
        if (collect_caption_runtime or caption_llm.configured()) and use_llm:
            caption_runtime = caption_result.get("caption_ai_runtime") or get_ollama_runtime_diagnostic(caption_model)
            progress.emit(
                "segmenting",
                0.9,
                "字幕AIのruntimeを確認中",
                diagnostics=_caption_runtime_diagnostic_entries(caption_runtime),
            )
        if cancel_event.is_set():
            raise WorkerCancelled("処理をキャンセルしました。")
        progress.emit("segmenting", 0.92, "字幕結果を検証中…")
        try:
            result = _build_result(
                request,
                transcript,
                caption_result,
                whisper_model=whisper_model,
                whisper_runtime=whisper_runtime,
                caption_runtime=caption_runtime,
            )
        except WorkerFailure as first_failure:
            if first_failure.code == "WHISPER_TIMING_INVALID":
                first_failure.progress_emitted = True
                progress.emit(
                    "failed",
                    0.92,
                    "Whisperの時刻情報が不正です。",
                    error_code=first_failure.code,
                    diagnostics=[{"level": "error", "code": first_failure.code, "message": "Raw Whisper timing cannot be safely mapped."}],
                )
                raise
            fallback_diagnostic = {
                "level": "warning",
                "code": "CAPTION_RESULT_ASSEMBLY_FALLBACK",
                "message": f"Result assembly failed at {first_failure.code}; deterministic source-word rebuild was attempted.",
            }
            progress.emit("segmenting", 0.94, "字幕結果を検証中…", diagnostics=[fallback_diagnostic])
            try:
                fallback_result = _deterministic_caption_result(
                    transcript,
                    source_start_us=request["sourceStartUs"],
                    source_end_us=request["sourceEndUs"],
                    max_chars=request["maxChars"],
                )
                result = _build_result(
                    request,
                    transcript,
                    fallback_result,
                    whisper_model=whisper_model,
                    whisper_runtime=whisper_runtime,
                    caption_runtime=caption_runtime,
                    extra_diagnostics=[fallback_diagnostic, {
                        "level": "warning",
                        "code": first_failure.code,
                        "message": "The higher-quality caption result was not used; raw source words were preserved.",
                    }],
                )
            except WorkerFailure as second_failure:
                second_failure.progress_emitted = True
                progress.emit(
                    "failed",
                    0.94,
                    "字幕結果の組み立てに失敗しました。",
                    error_code=second_failure.code,
                    diagnostics=[fallback_diagnostic, {
                        "level": "error",
                        "code": second_failure.code,
                        "message": "Deterministic source-word rebuild could not produce a safe contract result.",
                    }],
                )
                raise second_failure from first_failure
            except Exception as second_failure:
                failure = WorkerFailure("字幕結果の組み立てに失敗しました。", code="CAPTION_RESULT_ASSEMBLY_FAILED")
                failure.progress_emitted = True
                progress.emit(
                    "failed",
                    0.94,
                    "字幕結果の組み立てに失敗しました。",
                    error_code=failure.code,
                    diagnostics=[fallback_diagnostic, {"level": "error", "code": failure.code, "message": "Deterministic source-word rebuild failed safely."}],
                )
                raise failure from second_failure
        except Exception as first_failure:
            failure = WorkerFailure("字幕結果の組み立てに失敗しました。", code="CAPTION_RESULT_ASSEMBLY_FAILED")
            failure.progress_emitted = True
            progress.emit(
                "failed",
                0.92,
                "字幕結果の組み立てに失敗しました。",
                error_code=failure.code,
                diagnostics=[{"level": "error", "code": failure.code, "message": "Caption result assembly failed safely."}],
            )
            raise failure from first_failure
        if output_path is not None:
            _write_json_atomically(Path(output_path), result)
        if emit_completed:
            progress.emit("completed", 1.0, "自動字幕を完了しました。")
        return result


def _load_request(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerFailure("request JSONを読み込めません。") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spectra bounded auto-caption worker")
    parser.add_argument("--request", required=True, help="Stage 1 request JSON")
    parser.add_argument("--output", required=True, help="Stage 1 result JSON")
    parser.add_argument("--progress", help="JSONL progress output path; default is stdout")
    parser.add_argument("--whisper-model", default=None, help="default: Japanese fine-tune for ja, large-v3 otherwise")
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
        if args.progress:
            progress_path = Path(args.progress)
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_file = progress_path.open("w", encoding="utf-8")
        else:
            progress_file = sys.stdout
        run_worker(
            request_payload,
            output_path=args.output,
            progress_stream=progress_file,
            cancel_event=cancel_event,
            whisper_model=args.whisper_model,
            caption_model=args.caption_model,
            caption_timeout=args.caption_timeout,
            acceleration_mode=args.acceleration_mode,
            collect_caption_runtime=True,
        )
        return 0
    except WorkerCancelled as exc:
        if progress_file is not None:
            ProgressEmitter(job_id, progress_file).emit("failed", 0.0, "処理をキャンセルしました。", error_code="CANCELLED")
        print(str(exc), file=sys.stderr)
        return 2
    except (WorkerFailure, AutoCaptionContractError, ValueError) as exc:
        if progress_file is not None:
            if not getattr(exc, "progress_emitted", False):
                error_code = exc.code if isinstance(exc, WorkerFailure) else "WORKER_FAILED"
                ProgressEmitter(job_id, progress_file).emit("failed", 0.0, "自動字幕workerに失敗しました。", error_code=error_code)
        print(str(exc), file=sys.stderr)
        artifact_path = getattr(exc, "artifact_path", None)
        if artifact_path:
            print(f"failure artifact: {artifact_path}", file=sys.stderr)
        return 1
    finally:
        if args.progress and progress_file is not None:
            progress_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
