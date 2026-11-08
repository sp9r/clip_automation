"""Validator for the experimental local auto-caption worker boundary.

This module deliberately does not import the caption algorithm.  It only
validates the JSON exchanged with another process, so the worker can evolve
without making video_editor depend on clip_automation internals.
"""

from __future__ import annotations

from copy import deepcopy
from numbers import Integral, Real


REQUEST_VERSION = "spectra.auto-caption.request.v1"
RESULT_VERSION = "spectra.auto-caption.result.v1"
# Segmentation-only mode: re-split already transcribed words (no audio, no Whisper).
RESEGMENT_REQUEST_VERSION = "spectra.auto-caption.resegment.request.v1"
RESEGMENT_RESULT_VERSION = "spectra.auto-caption.resegment.result.v1"
BASELINE_VERSION = "experimental.auto-caption-baseline.v1"
MAPPING_FORMULA = "timelineUs = clip.startUs + (sourceUs - clip.sourceInUs)"
_MAX_ITEMS = 100_000
_MAX_TEXT_LENGTH = 100_000
_SEGMENTATION_MODES = {"auto", "rule", "llm"}
_SILENCE_CUT_MODES = {"off", "natural", "strong", "youtube"}
_ACCELERATION_MODES = {"auto", "gpu", "cpu"}
_TEXT_STATUSES = {"as_transcribed", "display_normalized", "ai_cleaned", "ai_corrected", "manually_corrected"}
_PROVENANCES = {"caption_algorithm", "rule_fallback", "llm", "unknown"}
_FORBIDDEN_KEYS = {
    "absolutePath", "accessToken", "clientSecret", "credential", "credentials",
    "mediaPath", "path", "paths", "projectPath", "refreshToken", "sourcePath", "token",
}


class AutoCaptionContractError(ValueError):
    """Raised when a worker contract payload is not safe to consume."""


def _record(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise AutoCaptionContractError(f"{label} must be an object")
    return value


def _keys(value: dict, allowed: set[str], label: str) -> None:
    unsupported = set(value) - allowed
    if unsupported:
        raise AutoCaptionContractError(f"{label} has unsupported fields: {sorted(unsupported)}")


def _required(value: dict, key: str, label: str) -> object:
    if key not in value:
        raise AutoCaptionContractError(f"{label}.{key} is required")
    return value[key]


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AutoCaptionContractError(f"{label} must be a boolean")
    return value


def _string(value: object, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AutoCaptionContractError(f"{label} must be a non-empty string")
    return value


def _identifier(value: object, label: str) -> str:
    result = _string(value, label, 128)
    if not result[0].isalnum() or any(character not in "._:-" and not character.isalnum() for character in result):
        raise AutoCaptionContractError(f"{label} has an invalid identifier")
    return result


def _integer_us(value: object, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise AutoCaptionContractError(f"{label} must be an integer microsecond value")
    result = int(value)
    if result < (1 if positive else 0):
        raise AutoCaptionContractError(f"{label} must be non-negative")
    return result


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    result = _integer_us(value, label)
    if not minimum <= result <= maximum:
        raise AutoCaptionContractError(f"{label} is out of range")
    return result


def _confidence(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not float(value) >= 0 or not float(value) <= 1:
        raise AutoCaptionContractError(f"{label} must be between 0 and 1")
    return float(value)


def _no_forbidden_fields(value: object, label: str = "contract") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _no_forbidden_fields(item, f"{label}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if key in _FORBIDDEN_KEYS:
            raise AutoCaptionContractError(f"{label}.{key} is not allowed")
        _no_forbidden_fields(child, f"{label}.{key}")


def parse_request(value: object) -> dict:
    request = _record(value, "auto-caption request")
    _keys(request, {"version", "jobId", "mediaPath", "sourceStartUs", "sourceEndUs", "language", "maxChars", "maxLines", "lineCapacity", "hidePunctuation", "segmentationMode", "silenceCut", "silenceMinUs", "accelerationMode", "subtitleTemplate"}, "auto-caption request")
    if _required(request, "version", "request") != REQUEST_VERSION:
        raise AutoCaptionContractError("unsupported auto-caption request version")
    source_start = _integer_us(_required(request, "sourceStartUs", "request"), "sourceStartUs")
    source_end = _integer_us(_required(request, "sourceEndUs", "request"), "sourceEndUs", positive=True)
    if source_end <= source_start:
        raise AutoCaptionContractError("sourceEndUs must be greater than sourceStartUs")
    mode = _string(_required(request, "segmentationMode", "request"), "segmentationMode", 16)
    if mode not in _SEGMENTATION_MODES:
        raise AutoCaptionContractError("unsupported segmentationMode")
    result = {
        "version": REQUEST_VERSION,
        "jobId": _identifier(_required(request, "jobId", "request"), "jobId"),
        "mediaPath": _string(_required(request, "mediaPath", "request"), "mediaPath", 32_000),
        "sourceStartUs": source_start,
        "sourceEndUs": source_end,
        "language": _string(_required(request, "language", "request"), "language", 35),
        "maxChars": _bounded_integer(_required(request, "maxChars", "request"), "maxChars", 1, 10_000),
        "segmentationMode": mode,
    }
    if "maxLines" in request:
        result["maxLines"] = _bounded_integer(request["maxLines"], "maxLines", 1, 16)
    if "lineCapacity" in request:
        # 1行に入る全角換算の文字数。省略時は文字数上限だけで区切る(旧request互換)。
        result["lineCapacity"] = _bounded_integer(request["lineCapacity"], "lineCapacity", 1, 200)
    if "hidePunctuation" in request:
        result["hidePunctuation"] = _boolean(request["hidePunctuation"], "hidePunctuation")
    if "silenceCut" in request:
        silence_cut = _string(request["silenceCut"], "silenceCut", 16)
        if silence_cut not in _SILENCE_CUT_MODES:
            raise AutoCaptionContractError("unsupported silenceCut")
        result["silenceCut"] = silence_cut
    if "silenceMinUs" in request:
        result["silenceMinUs"] = _bounded_integer(request["silenceMinUs"], "silenceMinUs", 0, 2_000_000)
    if "accelerationMode" in request:
        acceleration_mode = _string(request["accelerationMode"], "accelerationMode", 16)
        if acceleration_mode not in _ACCELERATION_MODES:
            raise AutoCaptionContractError("unsupported accelerationMode")
        result["accelerationMode"] = acceleration_mode
    if "subtitleTemplate" in request:
        subtitle_template = _identifier(request["subtitleTemplate"], "subtitleTemplate")
        result["subtitleTemplate"] = subtitle_template
    return result


def _parse_raw_word(value: object, label: str) -> dict:
    word = _record(value, label)
    _keys(word, {"id", "text", "startUs", "endUs", "confidence"}, label)
    start = _integer_us(_required(word, "startUs", label), f"{label}.startUs")
    end = _integer_us(_required(word, "endUs", label), f"{label}.endUs")
    if end < start:
        raise AutoCaptionContractError(f"{label} has a reversed time range")
    parsed = {
        "id": _identifier(_required(word, "id", label), f"{label}.id"),
        "text": _string(_required(word, "text", label), f"{label}.text", _MAX_TEXT_LENGTH),
        "startUs": start,
        "endUs": end,
    }
    if "confidence" in word:
        parsed["confidence"] = _confidence(word["confidence"], f"{label}.confidence")
    return parsed


def _parse_caption(value: object, label: str, word_ids: set[str]) -> dict:
    caption = _record(value, label)
    _keys(caption, {"id", "text", "sourceWordIds", "startUs", "endUs", "textStatus", "provenance"}, label)
    start = _integer_us(_required(caption, "startUs", label), f"{label}.startUs")
    end = _integer_us(_required(caption, "endUs", label), f"{label}.endUs", positive=True)
    if end <= start:
        raise AutoCaptionContractError(f"{label} must have a positive duration")
    source_word_ids = _required(caption, "sourceWordIds", label)
    if not isinstance(source_word_ids, list) or not source_word_ids or len(source_word_ids) > _MAX_ITEMS:
        raise AutoCaptionContractError(f"{label}.sourceWordIds must be a non-empty array")
    ids = [_identifier(item, f"{label}.sourceWordIds[{index}]") for index, item in enumerate(source_word_ids)]
    if len(set(ids)) != len(ids) or any(item not in word_ids for item in ids):
        raise AutoCaptionContractError(f"{label}.sourceWordIds reference unknown or duplicate words")
    status = _string(_required(caption, "textStatus", label), f"{label}.textStatus", 64)
    provenance = _string(_required(caption, "provenance", label), f"{label}.provenance", 64)
    if status not in _TEXT_STATUSES or provenance not in _PROVENANCES:
        raise AutoCaptionContractError(f"{label} has an unsupported status")
    return {
        "id": _identifier(_required(caption, "id", label), f"{label}.id"),
        "text": _string(_required(caption, "text", label), f"{label}.text", _MAX_TEXT_LENGTH),
        "sourceWordIds": ids,
        "startUs": start,
        "endUs": end,
        "textStatus": status,
        "provenance": provenance,
    }


def _parse_diagnostics(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise AutoCaptionContractError("diagnostics must be an array")
    result = []
    for index, item in enumerate(value):
        diagnostic = _record(item, f"diagnostics[{index}]")
        _keys(diagnostic, {"level", "code", "message"}, f"diagnostics[{index}]")
        level = _string(_required(diagnostic, "level", f"diagnostics[{index}]"), f"diagnostics[{index}].level", 16)
        if level not in {"info", "warning", "error"}:
            raise AutoCaptionContractError(f"diagnostics[{index}].level is invalid")
        result.append({
            "level": level,
            "code": _identifier(_required(diagnostic, "code", f"diagnostics[{index}]"), f"diagnostics[{index}].code"),
            "message": _string(_required(diagnostic, "message", f"diagnostics[{index}]"), f"diagnostics[{index}].message", _MAX_TEXT_LENGTH),
        })
    return result


def _parse_fallback(value: object) -> dict:
    fallback = _record(value, "fallback")
    _keys(fallback, {"used", "reason"}, "fallback")
    if not isinstance(fallback.get("used"), bool):
        raise AutoCaptionContractError("fallback.used must be boolean")
    result = {"used": fallback["used"]}
    if "reason" in fallback:
        result["reason"] = _string(fallback["reason"], "fallback.reason", _MAX_TEXT_LENGTH)
    return result


def parse_resegment_request(value: object) -> dict:
    """Validate a segmentation-only request: the words to re-split plus the layout limits."""
    request = _record(value, "resegment request")
    _keys(request, {"version", "jobId", "language", "maxChars", "maxLines", "lineCapacity", "hidePunctuation", "segmentationMode", "words"}, "resegment request")
    if _required(request, "version", "request") != RESEGMENT_REQUEST_VERSION:
        raise AutoCaptionContractError("unsupported resegment request version")
    mode = _string(_required(request, "segmentationMode", "request"), "segmentationMode", 16)
    if mode not in _SEGMENTATION_MODES:
        raise AutoCaptionContractError("unsupported segmentationMode")
    raw_words = _required(request, "words", "request")
    if not isinstance(raw_words, list) or not raw_words or len(raw_words) > _MAX_ITEMS:
        raise AutoCaptionContractError("words must be a non-empty array")
    words = []
    seen: set[str] = set()
    for index, item in enumerate(raw_words):
        label = f"words[{index}]"
        word = _record(item, label)
        _keys(word, {"id", "text", "startUs", "endUs"}, label)
        word_id = _identifier(_required(word, "id", label), f"{label}.id")
        if word_id in seen:
            raise AutoCaptionContractError(f"{label}.id is duplicated")
        seen.add(word_id)
        start = _integer_us(_required(word, "startUs", label), f"{label}.startUs")
        end = _integer_us(_required(word, "endUs", label), f"{label}.endUs")
        if end < start:
            raise AutoCaptionContractError(f"{label} ends before it starts")
        words.append({"id": word_id, "text": _string(_required(word, "text", label), f"{label}.text", 400), "startUs": start, "endUs": end})
    result = {
        "version": RESEGMENT_REQUEST_VERSION,
        "jobId": _identifier(_required(request, "jobId", "request"), "jobId"),
        "language": _string(_required(request, "language", "request"), "language", 35),
        "maxChars": _bounded_integer(_required(request, "maxChars", "request"), "maxChars", 1, 10_000),
        "maxLines": _bounded_integer(_required(request, "maxLines", "request"), "maxLines", 1, 16),
        "segmentationMode": mode,
        "words": words,
    }
    if "lineCapacity" in request:
        result["lineCapacity"] = _bounded_integer(request["lineCapacity"], "lineCapacity", 1, 200)
    if "hidePunctuation" in request:
        result["hidePunctuation"] = _boolean(request["hidePunctuation"], "hidePunctuation")
    return result


def parse_result(value: object) -> dict:
    result = _record(value, "auto-caption result")
    _no_forbidden_fields(result)
    _keys(result, {"version", "jobId", "sourceStartUs", "sourceEndUs", "language", "provider", "model", "rawWords", "captions", "diagnostics", "fallback"}, "auto-caption result")
    if _required(result, "version", "result") != RESULT_VERSION:
        raise AutoCaptionContractError("unsupported auto-caption result version")
    source_start = _integer_us(_required(result, "sourceStartUs", "result"), "sourceStartUs")
    source_end = _integer_us(_required(result, "sourceEndUs", "result"), "sourceEndUs", positive=True)
    if source_end <= source_start:
        raise AutoCaptionContractError("result source range is invalid")
    raw_words = _required(result, "rawWords", "result")
    if not isinstance(raw_words, list) or len(raw_words) > _MAX_ITEMS:
        raise AutoCaptionContractError("rawWords must be an array")
    words = [_parse_raw_word(word, f"rawWords[{index}]") for index, word in enumerate(raw_words)]
    if len({word["id"] for word in words}) != len(words):
        raise AutoCaptionContractError("rawWords IDs must be unique")
    if any(word["startUs"] < source_start or word["endUs"] > source_end for word in words):
        raise AutoCaptionContractError("rawWords exceed the requested source range")
    raw_captions = _required(result, "captions", "result")
    if not isinstance(raw_captions, list) or len(raw_captions) > _MAX_ITEMS:
        raise AutoCaptionContractError("captions must be an array")
    captions = [_parse_caption(caption, f"captions[{index}]", {word["id"] for word in words}) for index, caption in enumerate(raw_captions)]
    if len({caption["id"] for caption in captions}) != len(captions):
        raise AutoCaptionContractError("caption IDs must be unique")
    if any(caption["startUs"] < source_start or caption["endUs"] > source_end for caption in captions):
        raise AutoCaptionContractError("captions exceed the requested source range")
    return {
        "version": RESULT_VERSION,
        "jobId": _identifier(_required(result, "jobId", "result"), "jobId"),
        "sourceStartUs": source_start,
        "sourceEndUs": source_end,
        "language": _string(_required(result, "language", "result"), "language", 35),
        "provider": _string(_required(result, "provider", "result"), "provider", 128),
        "model": _string(_required(result, "model", "result"), "model", 256),
        "rawWords": words,
        "captions": captions,
        "diagnostics": _parse_diagnostics(_required(result, "diagnostics", "result")),
        "fallback": _parse_fallback(_required(result, "fallback", "result")),
    }


def source_time_to_timeline_us(clip: dict, source_us: object) -> int:
    source = _integer_us(source_us, "sourceUs")
    source_in = _integer_us(_required(clip, "sourceInUs", "clip"), "clip.sourceInUs")
    start = _integer_us(_required(clip, "startUs", "clip"), "clip.startUs")
    duration = _integer_us(_required(clip, "durationUs", "clip"), "clip.durationUs")
    if source < source_in or source > source_in + duration:
        raise AutoCaptionContractError("source time is outside the selected clip")
    return start + (source - source_in)


def build_baseline(*, result: dict, job_id: str, created_at: str, project_fingerprint: str, media_fingerprint: str, source_clip: dict, settings: dict) -> dict:
    parsed = parse_result(result)
    if parsed["jobId"] != job_id:
        raise AutoCaptionContractError("baseline jobId does not match result")
    clip = _record(source_clip, "sourceClip")
    _keys(clip, {"clipId", "assetId", "startUs", "sourceInUs", "durationUs"}, "sourceClip")
    if _integer_us(_required(clip, "durationUs", "sourceClip"), "sourceClip.durationUs", positive=True) <= 0:
        raise AutoCaptionContractError("baseline source clip duration must be positive")
    _keys(settings, {"language", "maxChars", "maxLines", "lineCapacity", "hidePunctuation", "maxWidthPercent", "segmentationMode", "silenceCut", "silenceMinUs", "accelerationMode", "subtitleTemplate"}, "settings")
    parse_request({
        "version": REQUEST_VERSION,
        "jobId": "settings-validation",
        "mediaPath": "redacted",
        "sourceStartUs": 0,
        "sourceEndUs": 1,
        "language": settings.get("language"),
        "maxChars": settings.get("maxChars"),
        "segmentationMode": settings.get("segmentationMode"),
        **{key: settings[key] for key in ("maxLines", "lineCapacity", "hidePunctuation", "silenceCut", "silenceMinUs", "accelerationMode", "subtitleTemplate") if key in settings},
    })
    baseline = {
        "version": BASELINE_VERSION,
        "jobId": _identifier(job_id, "jobId"),
        "createdAt": _string(created_at, "createdAt", 128),
        "projectFingerprint": _string(project_fingerprint, "projectFingerprint", 256),
        "mediaFingerprint": _string(media_fingerprint, "mediaFingerprint", 256),
        "sourceClip": deepcopy(clip),
        "timelineMapping": {"formula": MAPPING_FORMULA, "clipStartUs": clip["startUs"], "sourceInUs": clip["sourceInUs"]},
        "rawWords": deepcopy(parsed["rawWords"]),
        "captions": deepcopy(parsed["captions"]),
        "settings": deepcopy(settings),
        "provider": parsed["provider"],
        "model": parsed["model"],
        "diagnostics": deepcopy(parsed["diagnostics"]),
        "fallback": deepcopy(parsed["fallback"]),
    }
    _no_forbidden_fields(baseline)
    return baseline


def parse_baseline(value: object) -> dict:
    baseline = _record(value, "baseline")
    _no_forbidden_fields(baseline)
    _keys(baseline, {"version", "jobId", "createdAt", "projectFingerprint", "mediaFingerprint", "sourceClip", "timelineMapping", "rawWords", "captions", "settings", "provider", "model", "diagnostics", "fallback"}, "baseline")
    if _required(baseline, "version", "baseline") != BASELINE_VERSION:
        raise AutoCaptionContractError("unsupported baseline version")
    source_clip = _record(_required(baseline, "sourceClip", "baseline"), "sourceClip")
    _keys(source_clip, {"clipId", "assetId", "startUs", "sourceInUs", "durationUs"}, "sourceClip")
    for key in ("startUs", "sourceInUs", "durationUs"):
        _integer_us(_required(source_clip, key, "sourceClip"), f"sourceClip.{key}")
    if source_clip["durationUs"] <= 0:
        raise AutoCaptionContractError("sourceClip.durationUs must be positive")
    mapping = _record(_required(baseline, "timelineMapping", "baseline"), "timelineMapping")
    _keys(mapping, {"formula", "clipStartUs", "sourceInUs"}, "timelineMapping")
    if mapping.get("formula") != MAPPING_FORMULA:
        raise AutoCaptionContractError("unsupported baseline mapping formula")
    result = {
        "version": BASELINE_VERSION,
        "jobId": _identifier(_required(baseline, "jobId", "baseline"), "jobId"),
        "createdAt": _string(_required(baseline, "createdAt", "baseline"), "createdAt", 128),
        "projectFingerprint": _string(_required(baseline, "projectFingerprint", "baseline"), "projectFingerprint", 256),
        "mediaFingerprint": _string(_required(baseline, "mediaFingerprint", "baseline"), "mediaFingerprint", 256),
        "sourceClip": deepcopy(source_clip),
        "timelineMapping": deepcopy(mapping),
        "rawWords": deepcopy(_required(baseline, "rawWords", "baseline")),
        "captions": deepcopy(_required(baseline, "captions", "baseline")),
        "settings": deepcopy(_required(baseline, "settings", "baseline")),
        "provider": _string(_required(baseline, "provider", "baseline"), "provider", 128),
        "model": _string(_required(baseline, "model", "baseline"), "model", 256),
        "diagnostics": _parse_diagnostics(_required(baseline, "diagnostics", "baseline")),
        "fallback": _parse_fallback(_required(baseline, "fallback", "baseline")),
    }
    parsed_result = parse_result({
        "version": RESULT_VERSION,
        "jobId": result["jobId"],
        "sourceStartUs": result["rawWords"][0]["startUs"] if result["rawWords"] else 0,
        "sourceEndUs": result["rawWords"][-1]["endUs"] if result["rawWords"] else 1,
        "language": result["settings"]["language"],
        "provider": result["provider"],
        "model": result["model"],
        "rawWords": result["rawWords"],
        "captions": result["captions"],
        "diagnostics": result["diagnostics"],
        "fallback": result["fallback"],
    })
    result["rawWords"] = parsed_result["rawWords"]
    result["captions"] = parsed_result["captions"]
    return result
