"""Deterministic, audio-signal-authoritative silence analysis.

This module deliberately does not classify or remove fillers. Whisper words are
used only as conservative speech-protection intervals when a signal silence
would otherwise overlap a known word.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable


PROFILE_SETTINGS = {
    "natural": {"min_silence_us": 600_000, "retain_pause_us": 180_000},
    "strong": {"min_silence_us": 400_000, "retain_pause_us": 100_000},
    # Strong, but still conservative enough to keep a short edit-friendly gap.
    # This is a signal-silence profile; it does not classify or remove fillers.
    "youtube": {"min_silence_us": 150_000, "retain_pause_us": 0},
}
DEFAULT_PROFILE = "youtube"
DEFAULT_NOISE_DB = -35.0
DEFAULT_SPEECH_PROTECTION_US = 80_000

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


class SilenceDetectionError(RuntimeError):
    def __init__(self, message: str, *, code: str = "SILENCE_ANALYSIS_FAILED"):
        super().__init__(message)
        self.code = code


class SilenceDetectionCancelled(SilenceDetectionError):
    def __init__(self, message: str = "無音解析をキャンセルしました。"):
        super().__init__(message, code="CANCELLED")


def _seconds_to_us(value: Any) -> int:
    result = int(round(float(value) * 1_000_000))
    if result < 0:
        raise ValueError("time must be non-negative")
    return result


def _null_device() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        finally:
            process.wait(timeout=2)


def _read_pipe(stream: Any, chunks: list[bytes]) -> None:
    try:
        data = stream.read()
        if data:
            chunks.append(data)
    finally:
        stream.close()


def _run(
    command: list[str],
    *,
    timeout: int = 120,
    cancel_event: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[bytes] | None = None
    readers: list[threading.Thread] = []
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    try:
        if cancel_event is None:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                shell=False,
                timeout=timeout,
            )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        readers = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout_chunks), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr_chunks), daemon=True),
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel_event.is_set():
                _stop_process(process)
                raise SilenceDetectionCancelled()
            if time.monotonic() >= deadline:
                _stop_process(process)
                raise SilenceDetectionError("FFmpegの無音解析がタイムアウトしました。", code="FFMPEG_TIMEOUT")
            time.sleep(0.05)
        returncode = process.wait()
        for reader in readers:
            reader.join(timeout=2)
        return subprocess.CompletedProcess(
            command,
            returncode or 0,
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SilenceDetectionError("FFmpeg silence analysis could not start.") from exc
    finally:
        if process is not None and process.poll() is None:
            _stop_process(process)
        for reader in readers:
            reader.join(timeout=2)


def _ffmpeg_failure_code(stderr: str) -> str:
    lowered = str(stderr or "").lower()
    if (
        "matches no streams" in lowered
        or "does not contain any stream" in lowered
        or "stream specifier" in lowered and "matches no streams" in lowered
    ):
        return "NO_AUDIO_STREAM"
    if "invalid data found" in lowered or "error opening input" in lowered:
        return "INVALID_MEDIA"
    if "no such file" in lowered or "cannot open" in lowered:
        return "INPUT_NOT_FOUND"
    return "FFMPEG_NONZERO_EXIT"


def _ffmpeg_failure_message(code: str) -> str:
    return {
        "NO_AUDIO_STREAM": "メディアに音声トラックがありません。",
        "INVALID_MEDIA": "入力メディア形式を解析できません。",
        "INPUT_NOT_FOUND": "入力メディアを開けません。",
        "FFMPEG_NONZERO_EXIT": "FFmpegの無音解析に失敗しました。",
    }.get(code, "FFmpegの無音解析に失敗しました。")


def _has_malformed_silencedetect_event(text: str) -> bool:
    for line in str(text or "").splitlines():
        if "silence_start:" in line and _SILENCE_START.search(line) is None:
            return True
        if "silence_end:" in line and _SILENCE_END.search(line) is None:
            return True
    return False


def probe_duration_us(
    media_path: str | os.PathLike,
    *,
    ffprobe_path: str = "ffprobe",
    cancel_event: Any | None = None,
) -> int:
    command = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(Path(media_path).resolve()),
    ]
    result = _run(command, timeout=30, cancel_event=cancel_event)
    if result.returncode != 0:
        raise SilenceDetectionError("メディア長を取得できません。")
    try:
        duration_us = _seconds_to_us(result.stdout.strip())
    except (TypeError, ValueError):
        raise SilenceDetectionError("メディア長の形式が不正です。") from None
    if duration_us <= 0:
        raise SilenceDetectionError("メディア長が0です。")
    return duration_us


def parse_silencedetect_output(
    text: str,
    *,
    duration_us: int,
    min_silence_us: int,
) -> list[dict[str, int]]:
    """Parse deterministic FFmpeg silencedetect events into integer microseconds."""

    starts: list[int] = []
    silences: list[dict[str, int]] = []
    for line in str(text or "").splitlines():
        start_match = _SILENCE_START.search(line)
        if start_match:
            starts.append(min(duration_us, _seconds_to_us(start_match.group(1))))
        end_match = _SILENCE_END.search(line)
        if end_match and starts:
            start_us = starts.pop(0)
            end_us = min(duration_us, _seconds_to_us(end_match.group(1)))
            if end_us > start_us and end_us - start_us >= min_silence_us:
                silences.append({
                    "startUs": start_us,
                    "endUs": end_us,
                    "durationUs": end_us - start_us,
                })
    for start_us in starts:
        if duration_us - start_us >= min_silence_us:
            silences.append({
                "startUs": start_us,
                "endUs": duration_us,
                "durationUs": duration_us - start_us,
            })
    return _merge_intervals(silences)


def _merge_intervals(intervals: list[dict[str, int]]) -> list[dict[str, int]]:
    ordered = sorted(
        (item for item in intervals if item["endUs"] > item["startUs"]),
        key=lambda item: (item["startUs"], item["endUs"]),
    )
    merged: list[dict[str, int]] = []
    for item in ordered:
        if merged and item["startUs"] <= merged[-1]["endUs"]:
            merged[-1]["endUs"] = max(merged[-1]["endUs"], item["endUs"])
            merged[-1]["durationUs"] = merged[-1]["endUs"] - merged[-1]["startUs"]
        else:
            merged.append(dict(item))
    return merged


def _load_words(transcript: str | os.PathLike | list[dict] | None) -> list[dict[str, int]]:
    if transcript is None:
        return []
    if isinstance(transcript, (str, os.PathLike)):
        data = json.loads(Path(transcript).read_text(encoding="utf-8"))
    else:
        data = transcript
    raw_words = data if isinstance(data, list) else data.get("words", []) if isinstance(data, dict) else []
    if isinstance(data, list) and data and isinstance(data[0], dict) and "words" in data[0]:
        raw_words = [word for segment in data for word in segment.get("words", []) if isinstance(word, dict)]
    words = []
    for word in raw_words:
        if not isinstance(word, dict):
            continue
        start = word.get("startUs")
        end = word.get("endUs")
        if start is None:
            start = _seconds_to_us(word.get("start", 0))
        if end is None:
            end = _seconds_to_us(word.get("end", 0))
        start_us, end_us = int(start), int(end)
        if end_us > start_us:
            words.append({"startUs": start_us, "endUs": end_us})
    return words


def _subtract_protected(
    start_us: int,
    end_us: int,
    protected: list[dict[str, int]],
) -> list[dict[str, int]]:
    pieces = [{"startUs": start_us, "endUs": end_us}]
    for guard in protected:
        next_pieces = []
        for piece in pieces:
            if guard["endUs"] <= piece["startUs"] or guard["startUs"] >= piece["endUs"]:
                next_pieces.append(piece)
                continue
            if piece["startUs"] < guard["startUs"]:
                next_pieces.append({"startUs": piece["startUs"], "endUs": guard["startUs"]})
            if guard["endUs"] < piece["endUs"]:
                next_pieces.append({"startUs": guard["endUs"], "endUs": piece["endUs"]})
        pieces = next_pieces
    return [piece for piece in pieces if piece["endUs"] > piece["startUs"]]


def _keep_ranges(
    duration_us: int,
    removed: list[dict[str, int]],
    *,
    start_us: int = 0,
) -> list[dict[str, int]]:
    ranges = []
    cursor = start_us
    for item in _merge_intervals([
        {**item, "durationUs": item["endUs"] - item["startUs"]} for item in removed
    ]):
        if item["startUs"] > cursor:
            ranges.append({"startUs": cursor, "endUs": item["startUs"], "durationUs": item["startUs"] - cursor})
        cursor = max(cursor, item["endUs"])
    if cursor < duration_us:
        ranges.append({"startUs": cursor, "endUs": duration_us, "durationUs": duration_us - cursor})
    return ranges


def analyze_silences(
    media_path: str | os.PathLike,
    transcript: str | os.PathLike | list[dict] | None = None,
    *,
    profile: str = DEFAULT_PROFILE,
    min_silence_us: int | None = None,
    noise_db: float = DEFAULT_NOISE_DB,
    speech_protection_us: int = DEFAULT_SPEECH_PROTECTION_US,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    cancel_event: Any | None = None,
) -> dict[str, Any]:
    if profile not in PROFILE_SETTINGS:
        raise ValueError("profile must be natural, strong, or youtube")
    if int(speech_protection_us) < 0:
        raise ValueError("speech_protection_us must be non-negative")
    settings = PROFILE_SETTINGS[profile]
    if min_silence_us is None:
        min_silence_us = int(settings["min_silence_us"])
    else:
        min_silence_us = int(min_silence_us)
        if min_silence_us < 0 or min_silence_us > 2_000_000:
            raise ValueError("min_silence_us must be between 0 and 2000000")
    retain_pause_us = int(settings["retain_pause_us"])
    media = Path(media_path).resolve()
    if not media.is_file():
        raise FileNotFoundError("入力メディアが見つかりません。")
    duration_us = probe_duration_us(media, ffprobe_path=ffprobe_path, cancel_event=cancel_event)
    command = [
        ffmpeg_path,
        "-hide_banner", "-loglevel", "info",
        "-i", str(media),
        "-map", "0:a:0",
        "-vn", "-af", f"silencedetect=noise={float(noise_db):.2f}dB:d={max(1_000, min_silence_us) / 1_000_000:.6f}",
        "-f", "null", _null_device(),
    ]
    if runner is None:
        result = _run(command, cancel_event=cancel_event)
    else:
        result = runner(command, capture_output=True, text=True, check=False, shell=False, timeout=120)
    if result.returncode != 0:
        code = _ffmpeg_failure_code(result.stderr)
        raise SilenceDetectionError(_ffmpeg_failure_message(code), code=code)
    combined_output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if _has_malformed_silencedetect_event(combined_output):
        raise SilenceDetectionError(
            "FFmpegの無音解析結果の形式が不正です。",
            code="FFMPEG_OUTPUT_MALFORMED",
        )
    detected = parse_silencedetect_output(
        combined_output,
        duration_us=duration_us,
        min_silence_us=min_silence_us,
    )
    result = {
        "silences": detected,
        "keepRanges": _keep_ranges(duration_us, detected),
        "removedDurationUs": sum(item["endUs"] - item["startUs"] for item in detected),
        "settings": {
            "profile": profile,
            "minSilenceUs": min_silence_us,
            "retainPauseUs": retain_pause_us,
            "noiseDb": float(noise_db),
            "speechProtectionUs": int(speech_protection_us),
            "fillerRemoval": False,
        },
        "diagnostics": [
            {"level": "info", "code": "AUDIO_SIGNAL_AUTHORITY", "message": "Silences came from FFmpeg silencedetect."},
            {"level": "info", "code": "WHISPER_WORD_PROTECTION", "message": "No raw word intervals were supplied."},
            {"level": "info", "code": "FILLER_REMOVAL_DISABLED", "message": "Filler classification and deletion were not run."},
        ],
    }
    return apply_speech_protection(result, transcript)


def apply_speech_protection(
    result: dict[str, Any],
    transcript: str | os.PathLike | list[dict] | None,
) -> dict[str, Any]:
    """Apply raw-word protection after signal analysis has completed.

    This second pass allows signal analysis and Whisper inference to run in
    parallel without treating a Whisper word gap as the silence authority.
    """

    silences = result.get("silences") or []
    if not silences:
        return result
    words = _load_words(transcript)
    duration_us = max(
        [item["endUs"] for item in result.get("keepRanges", [])]
        + [item["endUs"] for item in silences]
    )
    range_start_us = min(
        [item["startUs"] for item in result.get("keepRanges", [])]
        + [item["startUs"] for item in silences]
    )
    protected = [
        {
            "startUs": max(range_start_us, word["startUs"] - int(result["settings"]["speechProtectionUs"])),
            "endUs": min(duration_us, word["endUs"] + int(result["settings"]["speechProtectionUs"])),
        }
        for word in words
    ]
    removed: list[dict[str, int]] = []
    updated_silences: list[dict[str, Any]] = []
    retain_pause_us = int(result["settings"]["retainPauseUs"])
    retain_left = retain_pause_us // 2
    retain_right = retain_pause_us - retain_left
    for silence in silences:
        candidate_start = silence["startUs"] + retain_left
        candidate_end = silence["endUs"] - retain_right
        removable = _subtract_protected(candidate_start, candidate_end, protected) if candidate_end > candidate_start else []
        removed.extend(removable)
        updated_silences.append({
            **silence,
            "removedRanges": [
                {**item, "durationUs": item["endUs"] - item["startUs"]} for item in removable
            ],
            "speechProtected": len(removable) != (1 if candidate_end > candidate_start else 0),
        })
    removed = _merge_intervals([
        {**item, "durationUs": item["endUs"] - item["startUs"]} for item in removed
    ])
    result = dict(result)
    result["silences"] = updated_silences
    result["keepRanges"] = _keep_ranges(duration_us, removed, start_us=range_start_us)
    result["removedDurationUs"] = sum(item["endUs"] - item["startUs"] for item in removed)
    result["diagnostics"] = [
        item for item in result.get("diagnostics", [])
        if item.get("code") != "WHISPER_WORD_PROTECTION"
    ]
    result["diagnostics"].insert(1, {
        "level": "info",
        "code": "WHISPER_WORD_PROTECTION",
        "message": f"Protected {len(words)} raw word intervals.",
    })
    return result


def shift_silence_analysis(result: dict[str, Any], offset_us: int) -> dict[str, Any]:
    """Move signal-analysis intervals from a prepared range to source time."""

    offset = int(offset_us)
    if offset < 0:
        raise ValueError("offset_us must be non-negative")
    shifted = dict(result)
    shifted["silences"] = [
        {
            **item,
            "startUs": item["startUs"] + offset,
            "endUs": item["endUs"] + offset,
            "removedRanges": [
                {
                    **removed,
                    "startUs": removed["startUs"] + offset,
                    "endUs": removed["endUs"] + offset,
                }
                for removed in item.get("removedRanges", [])
            ],
        }
        for item in result.get("silences", [])
    ]
    shifted["keepRanges"] = [
        {
            **item,
            "startUs": item["startUs"] + offset,
            "endUs": item["endUs"] + offset,
        }
        for item in result.get("keepRanges", [])
    ]
    return shifted


def write_silence_analysis(result: dict[str, Any], output_path: str | os.PathLike) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _map_source_us(source_us: int, keep_ranges: list[dict[str, int]]) -> int:
    timeline_us = 0
    for item in keep_ranges:
        start_us, end_us = item["startUs"], item["endUs"]
        if source_us < start_us:
            return timeline_us
        if source_us <= end_us:
            return timeline_us + source_us - start_us
        timeline_us += end_us - start_us
    return timeline_us


def build_clean_transcript(
    transcript: str | os.PathLike | list[dict],
    keep_ranges: list[dict[str, int]],
) -> list[dict[str, Any]]:
    """Map every raw word onto the compacted timeline without filler filtering."""

    data = json.loads(Path(transcript).read_text(encoding="utf-8")) if isinstance(transcript, (str, os.PathLike)) else transcript
    segments = data if isinstance(data, list) else []
    raw_words = []
    for segment in segments:
        if isinstance(segment, dict):
            raw_words.extend(word for word in segment.get("words", []) if isinstance(word, dict))
    output = []
    for item in keep_ranges:
        words = []
        for raw in raw_words:
            start_us = _seconds_to_us(raw.get("start", 0)) if raw.get("startUs") is None else int(raw["startUs"])
            end_us = _seconds_to_us(raw.get("end", 0)) if raw.get("endUs") is None else int(raw["endUs"])
            if end_us <= item["startUs"] or start_us >= item["endUs"]:
                continue
            mapped_start = _map_source_us(max(start_us, item["startUs"]), keep_ranges)
            mapped_end = _map_source_us(min(end_us, item["endUs"]), keep_ranges)
            if mapped_end <= mapped_start:
                continue
            words.append({
                "word": str(raw.get("word", "")),
                "start": round(mapped_start / 1_000_000, 6),
                "end": round(mapped_end / 1_000_000, 6),
            })
        if words:
            output.append({
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": "".join(word["word"] for word in words),
                "words": words,
            })
    return output


def generate_silence_cut_data(
    media_path: str | os.PathLike,
    transcript_path: str | os.PathLike,
    output_edl: str | os.PathLike,
    output_clean_transcript: str | os.PathLike,
    *,
    output_analysis: str | os.PathLike | None = None,
    profile: str = DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Analyze audio silence and write compatibility EDL plus all-word transcript."""

    result = analyze_silences(media_path, transcript_path, profile=profile)
    clean = build_clean_transcript(transcript_path, result["keepRanges"])
    if not clean:
        raise SilenceDetectionError("発話単語を含むkeep rangeがありません。")
    edl = [
        {"start": round(item["startUs"] / 1_000_000, 6), "end": round(item["endUs"] / 1_000_000, 6)}
        for item in result["keepRanges"]
        if item["endUs"] > item["startUs"]
    ]
    for target, payload in (
        (Path(output_edl), edl),
        (Path(output_clean_transcript), clean),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    if output_analysis is not None:
        write_silence_analysis(result, output_analysis)
    return result
