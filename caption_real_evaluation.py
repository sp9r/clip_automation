"""Real faster-whisper caption evaluation and human-review preparation.

This module deliberately keeps real utterance text outside tracked fixtures.  The
evaluation output is written below ``data/evaluations`` (ignored by git) and the
source run artifacts are opened read-only and verified by SHA-256 before/after.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from typing import Any, Iterable

from caption import generate_subtitles
from caption_boundary_v2 import boundary_diagnostic
from caption_quality import evaluate_caption_quality


SCHEMA_VERSION = 1
RATINGS = {"good", "acceptable", "bad"}
REVIEW_STATUSES = {"pending", "reviewed"}
PREFERRED_SYSTEMS = {"heuristic", "llm", "tie", "neither"}
BOUNDARY_REASONS = {
    "particle",
    "auxiliary/predicate",
    "modifier",
    "compound word",
    "English/Japanese",
    "numeric/unit",
    "semantic chunk",
    "too long",
    "too short",
    "timing/pause",
    "transcription error",
    "other",
}

REASON_LABELS = {
    "particle": "助詞",
    "auxiliary/predicate": "述語・補助動詞",
    "modifier": "修飾語",
    "compound word": "複合語",
    "English/Japanese": "英語・日本語混在",
    "numeric/unit": "数値・単位",
    "semantic chunk": "意味のまとまり",
    "too long": "長すぎる",
    "too short": "短すぎる",
    "timing/pause": "間・タイミング",
    "transcription error": "文字起こし誤り",
    "other": "その他",
}
REASON_FROM_LABEL = {label: reason for reason, label in REASON_LABELS.items()}
PREFERRED_SYSTEM_LABELS = {
    "heuristic": "ルール方式",
    "llm": "LLM方式",
    "tie": "同程度",
    "neither": "どちらでもない",
}
PREFERRED_SYSTEM_FROM_LABEL = {label: value for value, label in PREFERRED_SYSTEM_LABELS.items()}


@dataclass(frozen=True)
class RunCandidate:
    path: Path
    transcript_sha256: str
    clean_sha256: str
    segment_count: int
    word_count: int
    missing_word_count: int
    segments: tuple[dict, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_segments(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        return [item for item in payload["segments"] if isinstance(item, dict)]
    return []


def _normal_words(segment: dict) -> tuple[list[dict], int]:
    raw_words = segment.get("words")
    if not isinstance(raw_words, list):
        return [], 1
    words = []
    missing = 0
    for raw_word in raw_words:
        if not isinstance(raw_word, dict):
            missing += 1
            continue
        if not all(key in raw_word for key in ("word", "start", "end")):
            missing += 1
            continue
        try:
            start = float(raw_word["start"])
            end = float(raw_word["end"])
        except (TypeError, ValueError):
            missing += 1
            continue
        if not str(raw_word["word"]):
            missing += 1
            continue
        words.append({"word": str(raw_word["word"]), "start": start, "end": end})
    return words, missing


def load_segments(path: Path) -> tuple[list[dict], int]:
    """Load only transcript words; never writes to ``path``."""

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    segments = []
    missing_words = 0
    for index, raw_segment in enumerate(_payload_segments(payload)):
        words, missing = _normal_words(raw_segment)
        missing_words += missing
        if not words:
            missing_words += 1
            continue
        segments.append({"segment_index": index, "words": words})
    return segments, missing_words


def discover_runs(runs_root: str | Path, *, min_words: int = 20) -> list[RunCandidate]:
    root = Path(runs_root)
    candidates = []
    if not root.exists():
        return candidates
    for run_path in sorted(path for path in root.iterdir() if path.is_dir()):
        transcript = run_path / "transcript.json"
        clean = run_path / "clean_transcript.json"
        if not transcript.exists() or not clean.exists():
            continue
        try:
            segments, missing_word_count = load_segments(clean)
            transcript_segments, _ = load_segments(transcript)
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        word_count = sum(len(segment["words"]) for segment in segments)
        if word_count < min_words or not transcript_segments:
            continue
        candidates.append(
            RunCandidate(
                path=run_path,
                transcript_sha256=sha256_file(transcript),
                clean_sha256=sha256_file(clean),
                segment_count=len(segments),
                word_count=word_count,
                missing_word_count=missing_word_count,
                segments=tuple(segments),
            )
        )
    return candidates


def select_distinct_runs(candidates: Iterable[RunCandidate], run_count: int) -> list[RunCandidate]:
    """Select deterministic runs, ignoring duplicate source artifact pairs."""

    selected = []
    seen = set()
    for candidate in candidates:
        key = (candidate.transcript_sha256, candidate.clean_sha256)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= run_count:
            break
    return selected


def _utterance_categories(words: list[dict]) -> list[str]:
    text = "".join(word["word"] for word in words)
    duration = max(0.0, words[-1]["end"] - words[0]["start"])
    pauses = [
        max(0.0, words[index + 1]["start"] - words[index]["end"])
        for index in range(len(words) - 1)
    ]
    categories = []
    if len(text) <= 12:
        categories.append("short")
    if len(text) >= 29:
        categories.append("long")
    if duration / max(len(words), 1) < 0.18:
        categories.append("fast")
    if any(pause >= 0.35 for pause in pauses):
        categories.append("pause")
    if any(token in text for token in ("えっと", "あの", "えー", "まあ", "なんか")):
        categories.append("filler")
    if any(("A" <= char <= "Z") or ("a" <= char <= "z") for char in text):
        categories.append("english_mixed")
    if any(char.isnumeric() for char in text):
        categories.append("numeric")
    if any(mark in text for mark in ("、", "。", "？", "！", "?", "!")):
        categories.append("punctuation")
    if len(words) >= 5:
        categories.append("long_token_sequence")
    if not categories:
        categories.append("ordinary")
    return categories


def _stratified_samples(runs: list[RunCandidate], target_count: int) -> list[dict]:
    pool = []
    for run_index, run in enumerate(runs):
        for segment in run.segments:
            words = segment["words"]
            categories = _utterance_categories(words)
            stable_key = hashlib.sha256(
                f"{run.clean_sha256}:{segment['segment_index']}".encode("utf-8")
            ).hexdigest()
            pool.append(
                {
                    "run_index": run_index,
                    "segment_index": segment["segment_index"],
                    "words": words,
                    "categories": categories,
                    "stable_key": stable_key,
                }
            )
    by_category: dict[str, list[dict]] = defaultdict(list)
    for item in pool:
        for category in item["categories"]:
            by_category[category].append(item)
    for values in by_category.values():
        values.sort(key=lambda item: item["stable_key"])
    selected = []
    selected_keys = set()
    category_names = sorted(by_category)
    cursor = 0
    while len(selected) < min(target_count, len(pool)) and category_names:
        category = category_names[cursor % len(category_names)]
        cursor += 1
        values = by_category[category]
        while values and values[0]["stable_key"] in selected_keys:
            values.pop(0)
        if not values:
            category_names = [name for name in category_names if by_category[name]]
            continue
        item = values.pop(0)
        selected.append(item)
        selected_keys.add(item["stable_key"])
    if len(selected) < min(target_count, len(pool)):
        for item in sorted(pool, key=lambda value: value["stable_key"]):
            if item["stable_key"] not in selected_keys:
                selected.append(item)
                selected_keys.add(item["stable_key"])
            if len(selected) >= target_count:
                break
    return selected


def _caption_ranges(words: list[dict], captions: list[dict]) -> list[tuple[int, int]]:
    source_text = "".join(word["word"] for word in words)
    cumulative = []
    total = 0
    for word in words:
        total += len(word["word"])
        cumulative.append(total)
    cursor = 0
    ranges = []
    for caption in captions:
        text = str(caption.get("text", ""))
        if not source_text.startswith(text, cursor):
            return []
        end_cursor = cursor + len(text)
        start_word = next((index for index, end in enumerate(cumulative) if end > cursor), None)
        end_word = next((index for index, end in enumerate(cumulative) if end >= end_cursor), None)
        if start_word is None or end_word is None or cumulative[end_word] != end_cursor:
            return []
        ranges.append((start_word, end_word))
        cursor = end_cursor
    return ranges if cursor == len(source_text) else []


def _percentile(values: list[int | float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _bucket_counts(values: list[float], buckets: list[tuple[str, float, float | None]]) -> dict[str, int]:
    counts = {name: 0 for name, _, _ in buckets}
    for value in values:
        for name, lower, upper in buckets:
            if value >= lower and (upper is None or value < upper):
                counts[name] += 1
                break
    return counts


def _length_buckets(lengths: list[int]) -> dict[str, int]:
    return _bucket_counts(
        [float(length) for length in lengths],
        [("8-12", 8, 13), ("13-16", 13, 17), ("17-20", 17, 21), ("21-24", 21, 25), ("25-28", 25, 29), ("29-32", 29, 33), ("over_32", 33, None)],
    )


def _duration_buckets(durations: list[float]) -> dict[str, int]:
    return _bucket_counts(
        durations,
        [("under_1s", 0, 1), ("1-2s", 1, 2), ("2-3s", 2, 3), ("3-4s", 3, 4), ("4-5s", 4, 5), ("over_5s", 5, None)],
    )


def _preanalyze(words: list[dict], captions: list[dict], max_chars: int, max_duration: float) -> tuple[dict, list[dict]]:
    ranges = _caption_ranges(words, captions)
    lengths = [len(str(caption.get("text", ""))) for caption in captions]
    durations = [max(0.0, float(caption["end"]) - float(caption["start"])) for caption in captions]
    boundaries = []
    generic_protected_count = 0
    forced_count = 0
    for caption_index, ((_, end_word), caption) in enumerate(zip(ranges[:-1], captions[:-1])):
        next_start, next_end = ranges[caption_index + 1]
        diagnostic = boundary_diagnostic(words, end_word)
        if diagnostic.get("generic_protected"):
            generic_protected_count += 1
        combined_text_length = len(str(caption.get("text", ""))) + len(str(captions[caption_index + 1].get("text", "")))
        combined_duration = float(captions[caption_index + 1]["end"]) - float(caption["start"])
        forced = combined_text_length > max_chars or combined_duration > max_duration
        if forced:
            forced_count += 1
        pause = max(0.0, words[next_start]["start"] - words[end_word]["end"])
        boundaries.append(
            {
                "boundary_index": end_word,
                "caption_index": caption_index,
                "pause_before": pause,
                "pause_after": pause,
                "diagnostic": diagnostic,
                "forced_due_to_constraint": forced,
            }
        )
    metrics = {
        "caption_count": len(captions),
        "word_count": len(words),
        "average_char_count": (sum(lengths) / len(lengths)) if lengths else 0.0,
        "median_char_count": _percentile(lengths, 0.5),
        "p75_char_count": _percentile(lengths, 0.75),
        "p90_char_count": _percentile(lengths, 0.90),
        "max_char_count": max(lengths) if lengths else 0,
        "average_duration_seconds": (sum(durations) / len(durations)) if durations else 0.0,
        "length_buckets": _length_buckets(lengths),
        "duration_buckets": _duration_buckets(durations),
        "generic_protected_boundary_count": generic_protected_count,
        "forced_constraint_boundary_count": forced_count,
        "max_chars_violation_count": sum(length > max_chars for length in lengths),
        "max_lines_application": "renderer_owned",
        "visual_max_lines_verified": False,
    }
    return metrics, boundaries


def _json_safe_words(words: list[dict]) -> list[dict]:
    return [{"word": word["word"], "start": word["start"], "end": word["end"]} for word in words]


def _sample_document(sample: dict, run: RunCandidate, sample_id: str, max_chars: int, max_lines: int) -> dict:
    words = sample["words"]
    generation_error = None
    captions = []
    with tempfile.TemporaryDirectory() as temp_dir:
        transcript_path = Path(temp_dir) / "clean_transcript.json"
        output_path = Path(temp_dir) / "captions.srt"
        transcript_path.write_text(
            json.dumps([{"words": words}], ensure_ascii=False), encoding="utf-8"
        )
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                result = generate_subtitles(
                    str(transcript_path),
                    str(output_path),
                    use_llm=False,
                    max_chars=max_chars,
                    max_lines=max_lines,
                )
            captions = result["captions"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            generation_error = {"type": type(exc).__name__, "message": str(exc)}
    metrics, boundaries = _preanalyze(words, captions, max_chars, 5.0)
    quality = evaluate_caption_quality(
        words,
        captions,
        max_chars=max_chars,
        max_lines=max_lines,
    )
    metrics.update(
        {
            "generation_status": "generation_error" if generation_error else "ok",
            "generation_error": generation_error,
            "exact_source_fidelity": quality.exact_source_fidelity and generation_error is None,
            "order_preserved": quality.order_preserved,
            "timestamp_preserved": quality.timestamp_preserved and generation_error is None,
            "omitted_character_count": quality.omitted_character_count,
            "duplicated_character_count": quality.duplicated_character_count,
        }
    )
    return {
        "sample_id": sample_id,
        "source_ref": {
            "run_index": sample["run_index"],
            "segment_index": sample["segment_index"],
            "clean_sha256": run.clean_sha256,
        },
        "categories": sample["categories"],
        "source_words": _json_safe_words(words),
        "source_text": "".join(word["word"] for word in words),
        "step2": {
            "status": "generation_error" if generation_error else "ok",
            "captions": captions,
            "boundaries": boundaries,
        },
        "llm_assisted": {
            "available": False,
            "status": "not_run",
            "captions": [],
            "boundaries": [],
        },
        "metrics": metrics,
        "human_review": {
            "status": "pending",
            "rating": None,
            "segmentation_bad": False,
            "transcription_bad": False,
            "exclude_from_boundary_evaluation": False,
            "bad_boundaries": [],
            "preferred_boundaries": [],
            "reasons": [],
            "preferred_system": None,
            "notes": "",
        },
    }


def _aggregate(samples: list[dict]) -> dict:
    lengths = [length for sample in samples for length in [len(caption["text"]) for caption in sample["step2"]["captions"]]]
    durations = [
        max(0.0, float(caption["end"]) - float(caption["start"]))
        for sample in samples
        for caption in sample["step2"]["captions"]
    ]
    ratings = Counter(sample["human_review"]["rating"] for sample in samples if sample["human_review"]["rating"])
    return {
        "sample_count": len(samples),
        "caption_count": sum(len(sample["step2"]["captions"]) for sample in samples),
        "word_count": sum(len(sample["source_words"]) for sample in samples),
        "average_char_count": (sum(lengths) / len(lengths)) if lengths else 0.0,
        "median_char_count": _percentile(lengths, 0.5),
        "p75_char_count": _percentile(lengths, 0.75),
        "p90_char_count": _percentile(lengths, 0.90),
        "max_char_count": max(lengths) if lengths else 0,
        "length_buckets": _length_buckets(lengths),
        "duration_buckets": _duration_buckets(durations),
        "generic_protected_boundary_count": sum(sample["metrics"]["generic_protected_boundary_count"] for sample in samples),
        "forced_constraint_boundary_count": sum(sample["metrics"]["forced_constraint_boundary_count"] for sample in samples),
        "generation_error_count": sum(sample["metrics"].get("generation_status") == "generation_error" for sample in samples),
        "exact_source_fidelity_count": sum(sample["metrics"]["exact_source_fidelity"] for sample in samples),
        "timestamp_preserved_count": sum(sample["metrics"]["timestamp_preserved"] for sample in samples),
        "human_rated_count": sum(sample["human_review"]["status"] == "reviewed" for sample in samples),
        "pending_review_count": sum(sample["human_review"]["status"] == "pending" for sample in samples),
        "rating_counts": dict(ratings),
        "automatic_acceptance_count": ratings.get("good", 0) + ratings.get("acceptable", 0),
    }


def build_evaluation_document(
    runs_root: str | Path,
    *,
    run_count: int = 5,
    target_utterances: int = 100,
    max_chars: int = 32,
    max_lines: int = 2,
) -> dict:
    candidates = discover_runs(runs_root)
    runs = select_distinct_runs(candidates, run_count)
    if not runs:
        raise ValueError("評価対象のtranscript.json / clean_transcript.jsonがありません。")
    samples = _stratified_samples(runs, target_utterances)
    if not samples:
        raise ValueError("word timestampを持つ評価sampleがありません。")
    sample_documents = []
    for index, sample in enumerate(samples, start=1):
        run = runs[sample["run_index"]]
        sample_id = f"real_{index:04d}"
        sample_documents.append(_sample_document(sample, run, sample_id, max_chars, max_lines))
    source_integrity = []
    for run_index, run in enumerate(runs):
        transcript = run.path / "transcript.json"
        clean = run.path / "clean_transcript.json"
        transcript_after = sha256_file(transcript)
        clean_after = sha256_file(clean)
        source_integrity.append(
            {
                "run_index": run_index,
                "transcript_sha256_before": run.transcript_sha256,
                "transcript_sha256_after": transcript_after,
                "clean_sha256_before": run.clean_sha256,
                "clean_sha256_after": clean_after,
                "unchanged": transcript_after == run.transcript_sha256 and clean_after == run.clean_sha256,
                "segment_count": run.segment_count,
                "word_count": run.word_count,
                "missing_word_count": run.missing_word_count,
            }
        )
    document = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "caption_review.v1",
        "corpus_id": "caption_quality_real_step3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "orientation": "horizontal",
        "settings": {
            "max_chars": max_chars,
            "max_lines": max_lines,
            "max_lines_application": "renderer_owned",
            "visual_max_lines_verified": False,
            "primary_algorithm": "heuristic_step2",
            "llm_comparison": "not_run",
        },
        "source_policy": {
            "read_only": True,
            "source_paths_embedded": False,
            "real_text_tracked": False,
            "human_rating_automated": False,
        },
        "source_integrity": source_integrity,
        "sampled_run_count": len(runs),
        "sampled_utterance_count": len(sample_documents),
        "samples": sample_documents,
        "aggregate": _aggregate(sample_documents),
    }
    if not all(item["unchanged"] for item in source_integrity):
        raise RuntimeError("評価中にsource artifactのhashが変化しました。")
    return document


def validate_rating(rating: Any) -> None:
    if rating is not None and rating not in RATINGS:
        raise ValueError(f"ratingは{sorted(RATINGS)}またはnullです。")


def validate_review_document(document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("caption_review.v1のschema_versionが不正です。")
    samples = document.get("samples")
    if not isinstance(samples, list):
        raise ValueError("samplesが配列ではありません。")
    for sample in samples:
        if not isinstance(sample, dict) or not sample.get("sample_id"):
            raise ValueError("sample_idがありません。")
        review = sample.get("human_review")
        if not isinstance(review, dict) or review.get("status") not in REVIEW_STATUSES:
            raise ValueError("human_review.statusが不正です。")
        validate_rating(review.get("rating"))
        if not isinstance(review.get("segmentation_bad"), bool) or not isinstance(review.get("transcription_bad"), bool):
            raise ValueError("segmentation_bad / transcription_badはboolです。")
        if not isinstance(review.get("exclude_from_boundary_evaluation"), bool):
            raise ValueError("exclude_from_boundary_evaluationはboolです。")
        for field in ("bad_boundaries", "preferred_boundaries", "reasons"):
            if not isinstance(review.get(field), list):
                raise ValueError(f"{field}が配列ではありません。")
        if any(isinstance(index, bool) or not isinstance(index, int) for index in review["bad_boundaries"] + review["preferred_boundaries"]):
            raise ValueError("boundary annotationは整数配列です。")
        if any(reason not in BOUNDARY_REASONS for reason in review["reasons"]):
            raise ValueError("未知のboundary reasonです。")
        preferred_system = review.get("preferred_system")
        if preferred_system is not None and preferred_system not in PREFERRED_SYSTEMS:
            raise ValueError("preferred_systemが不正です。")
    return document


def refresh_aggregate(document: dict) -> dict:
    document["aggregate"] = _aggregate(document.get("samples", []))
    document["sampled_utterance_count"] = len(document.get("samples", []))
    return document


def load_review_document(path: str | Path) -> dict:
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            document = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"review artifactを読み込めません: {exc}") from exc
    return validate_review_document(document)


def write_review_document(path: str | Path, document: dict) -> None:
    validate_review_document(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def apply_review(
    document: dict,
    sample_id: str,
    *,
    rating: str,
    bad_boundaries: list[int] | None = None,
    preferred_boundaries: list[int] | None = None,
    reasons: list[str] | None = None,
    segmentation_bad: bool = False,
    transcription_bad: bool = False,
    exclude_from_boundary_evaluation: bool = False,
    preferred_system: str | None = None,
    notes: str = "",
) -> dict:
    validate_rating(rating)
    if rating is None:
        raise ValueError("review済みsampleにはratingが必要です。")
    if reasons and any(reason not in BOUNDARY_REASONS for reason in reasons):
        raise ValueError("未知のboundary reasonです。")
    if preferred_system is not None and preferred_system not in PREFERRED_SYSTEMS:
        raise ValueError("preferred_systemが不正です。")
    matches = [sample for sample in document.get("samples", []) if sample.get("sample_id") == sample_id]
    if len(matches) != 1:
        raise ValueError("sample_idが一意に見つかりません。")
    matches[0]["human_review"] = {
        "status": "reviewed",
        "rating": rating,
        "segmentation_bad": bool(segmentation_bad),
        "transcription_bad": bool(transcription_bad),
        "exclude_from_boundary_evaluation": bool(exclude_from_boundary_evaluation),
        "bad_boundaries": sorted(set(bad_boundaries or [])),
        "preferred_boundaries": sorted(set(preferred_boundaries or [])),
        "reasons": sorted(set(reasons or [])),
        "preferred_system": preferred_system,
        "notes": str(notes),
    }
    return refresh_aggregate(validate_review_document(document))


def _parse_int_list(value: str) -> list[int]:
    if not value.strip():
        return []
    try:
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("区切り番号はカンマ区切りの整数です。") from exc


def _parse_reason_input(value: str) -> list[str]:
    items = [item.strip() for item in value.replace("、", ",").split(",") if item.strip()]
    reasons = []
    for item in items:
        reason = REASON_FROM_LABEL.get(item, item if item in BOUNDARY_REASONS else None)
        if reason is None:
            raise ValueError(f"理由「{item}」は選択肢にありません。")
        reasons.append(reason)
    return sorted(set(reasons))


def _parse_preferred_system_input(value: str) -> str | None:
    item = value.strip()
    if not item:
        return None
    value = PREFERRED_SYSTEM_FROM_LABEL.get(item, item if item in PREFERRED_SYSTEMS else None)
    if value is None:
        raise ValueError("比較方式が選択肢にありません。")
    return value


def _caption_marker(index: int) -> str:
    return chr(0x2460 + index) if 0 <= index < 20 else f"{index + 1}."


def _japanese_boundary_category(category: str) -> str:
    return {
        "neutral": "通常の候補",
        "soft_discouraged": "できれば避ける",
        "preferred": "推奨候補",
        "hard_forbidden": "分割非推奨",
    }.get(category, "未分類")


def _print_review_summary(summary: dict) -> None:
    print("\nレビュー集計")
    print(f"対象: {summary['sample_count']}件")
    print(f"評価済み: {summary['reviewed']}件")
    print(f"未評価: {summary['pending']}件")
    print(f"このまま採用: {summary['rating_counts']['good']}件")
    print(f"修正不要: {summary['rating_counts']['acceptable']}件")
    print(f"修正したい: {summary['rating_counts']['bad']}件")
    print(f"自動採用率: {summary['automatic_acceptance_rate']:.1%}")


def _print_review_header(
    *,
    position: int,
    total: int,
    summary: dict,
    session_skipped_count: int,
) -> None:
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"字幕レビュー  {position} / {total}")
    print(f"完了: {summary['reviewed']}件")
    print(f"残り: {summary['pending']}件")
    if session_skipped_count:
        print(f"今回保留: {session_skipped_count}件")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def _load_strict_clean_ids(path: str | Path) -> set[str]:
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            diagnostics = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"diagnostic artifactを読み込めません: {exc}") from exc
    if not isinstance(diagnostics, dict) or diagnostics.get("artifact_type") != "caption_diagnostics.v1":
        raise ValueError("caption_diagnostics.v1 artifactではありません。")
    samples = diagnostics.get("samples")
    if not isinstance(samples, list):
        raise ValueError("diagnostic samplesが配列ではありません。")
    strict_ids = set()
    for sample in samples:
        eligibility = sample.get("review_eligible_for_segmentation", {})
        if eligibility.get("strict_review_eligible") is True:
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id:
                raise ValueError("strict diagnostic sample_idが不正です。")
            if sample_id in strict_ids:
                raise ValueError(f"diagnostic sample_idが重複しています: {sample_id}")
            strict_ids.add(sample_id)
    expected = diagnostics.get("aggregate", {}).get("strict_review_eligible_count")
    if isinstance(expected, int) and expected != len(strict_ids):
        raise ValueError("diagnostic strict review件数とsample一覧が一致しません。")
    return strict_ids


def _review_length_bucket(length: int) -> str:
    if length <= 12:
        return "<=12"
    if length <= 16:
        return "13-16"
    if length <= 20:
        return "17-20"
    if length <= 24:
        return "21-24"
    if length <= 28:
        return "25-28"
    if length <= 32:
        return "29-32"
    return "over_32"


def _review_duration_bucket(duration: float) -> str:
    if duration < 1:
        return "<1s"
    if duration < 2:
        return "1-2s"
    if duration < 3:
        return "2-3s"
    if duration < 4:
        return "3-4s"
    if duration < 5:
        return "4-5s"
    return "over_5s"


def _review_summary(document: dict, sample_ids: set[str], *, filter_name: str) -> dict:
    samples = [sample for sample in document["samples"] if sample.get("sample_id") in sample_ids]
    ratings = {"good": 0, "acceptable": 0, "bad": 0}
    reason_counts = Counter()
    length_by_rating = defaultdict(lambda: {"good": 0, "acceptable": 0, "bad": 0})
    duration_by_rating = defaultdict(lambda: {"good": 0, "acceptable": 0, "bad": 0})
    bad_boundary_count = 0
    reviewed = pending = 0
    for sample in samples:
        review = sample["human_review"]
        if review["status"] == "reviewed":
            reviewed += 1
            rating = review.get("rating")
            if rating in ratings:
                ratings[rating] += 1
            if rating == "bad":
                bad_boundary_count += len(review.get("bad_boundaries", []))
                reason_counts.update(review.get("reasons", []))
            captions = sample.get("step2", {}).get("captions", [])
            max_length = max((len(str(caption.get("text", ""))) for caption in captions), default=0)
            max_duration = max((max(0.0, float(caption["end"]) - float(caption["start"])) for caption in captions), default=0.0)
            if rating in ratings:
                length_by_rating[_review_length_bucket(max_length)][rating] += 1
                duration_by_rating[_review_duration_bucket(max_duration)][rating] += 1
        else:
            pending += 1
    return {
        "schema_version": 1,
        "artifact_type": "caption_human_review_summary.v1",
        "filter": filter_name,
        "sample_count": len(samples),
        "reviewed": reviewed,
        "pending": pending,
        "rating_counts": ratings,
        "automatic_acceptance_count": ratings["good"] + ratings["acceptable"],
        "automatic_acceptance_rate": ((ratings["good"] + ratings["acceptable"]) / reviewed) if reviewed else 0.0,
        "bad_boundary_count": bad_boundary_count,
        "bad_reason_counts": dict(sorted(reason_counts.items())),
        "length_bucket_by_rating": {key: value for key, value in sorted(length_by_rating.items())},
        "duration_bucket_by_rating": {key: value for key, value in sorted(duration_by_rating.items())},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_review_summary(path: str | Path, summary: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _print_review_sample(sample: dict) -> None:
    captions = sample.get("step2", {}).get("captions", [])
    ranges = _caption_ranges(sample.get("source_words", []), captions)
    print(f"サンプルID: {sample['sample_id']}")
    print(f"原文: {sample.get('source_text', '')}")
    print("単語: " + " ".join(f"[{index}:{word.get('word', '')}]" for index, word in enumerate(sample.get("source_words", []))))
    print("現在の字幕:")
    for index, caption in enumerate(captions):
        duration = max(0.0, float(caption["end"]) - float(caption["start"]))
        word_range = ranges[index] if index < len(ranges) else None
        range_text = f"、単語範囲={word_range[0]}〜{word_range[1]}番" if word_range else ""
        print(f"  {_caption_marker(index)} {caption['text']} | {caption['start']:.3f}〜{caption['end']:.3f}秒 | 文字数={len(str(caption['text']))} | 長さ={duration:.3f}秒{range_text}")
    print("現在の区切り:")
    if not sample.get("step2", {}).get("boundaries", []):
        print("  （区切り情報なし）")
    for boundary in sample.get("step2", {}).get("boundaries", []):
        diagnostic = boundary.get("diagnostic", {})
        flags = []
        if diagnostic.get("generic_protected"):
            flags.append("保護対象")
        if boundary.get("forced_due_to_constraint"):
            flags.append("制約による区切り")
        flag_text = f"、{ '・'.join(flags)}" if flags else ""
        print(
            "  {index}番の後 | 間={pause:.3f}秒 | 判定={category}{flags}".format(
                index=boundary.get("boundary_index"),
                pause=float(boundary.get("pause_after", boundary.get("pause_before", 0.0))),
                category=_japanese_boundary_category(diagnostic.get("category", "unknown")),
                flags=flag_text,
            )
        )


def review_interactive(
    path: str | Path,
    *,
    limit: int | None = None,
    diagnostics_path: str | Path | None = None,
    review_filter: str = "all",
    summary_output: str | Path | None = None,
) -> dict:
    document = load_review_document(path)
    if review_filter not in {"all", "strict-clean"}:
        raise ValueError("review filterはallまたはstrict-cleanです。")
    strict_ids = _load_strict_clean_ids(diagnostics_path) if review_filter == "strict-clean" else {
        sample["sample_id"] for sample in document["samples"]
    }
    if review_filter == "strict-clean":
        document_ids = {sample["sample_id"] for sample in document["samples"]}
        missing = strict_ids - document_ids
        if missing:
            raise ValueError(f"diagnosticにreview artifactへ存在しないsampleがあります: {sorted(missing)}")
    eligible_samples = [sample for sample in document["samples"] if sample["sample_id"] in strict_ids]
    eligible_positions = {sample["sample_id"]: index + 1 for index, sample in enumerate(eligible_samples)}
    summary_path = Path(summary_output) if summary_output else Path(path).with_name("caption_human_review_summary.v1.json")
    reviewed = 0
    session_skipped_sample_ids: set[str] = set()
    print(f"レビューを開始します。対象は{len(strict_ids)}件です。")
    while True:
        pending = [
            sample for sample in document["samples"]
            if sample["sample_id"] in strict_ids
            and sample["human_review"]["status"] == "pending"
            and sample["sample_id"] not in session_skipped_sample_ids
        ]
        if not pending or (limit is not None and reviewed >= limit):
            break
        sample = pending[0]
        _print_review_header(
            position=eligible_positions[sample["sample_id"]],
            total=len(eligible_samples),
            summary=_review_summary(document, strict_ids, filter_name=review_filter),
            session_skipped_count=len(session_skipped_sample_ids),
        )
        _print_review_sample(sample)
        print("\n評価してください:")
        print("[A] このままで良い")
        print("[B] 少し違和感はあるが修正不要")
        print("[C] 修正したい")
        print("[D] 今回は判断を飛ばす")
        print("[0] 中断して保存")
        print("入力 > ", end="", flush=True)
        raw_rating = input().strip()
        rating = {
            "A": "good", "B": "acceptable", "C": "bad", "D": "skip", "0": "quit",
            "g": "good", "a": "acceptable", "b": "bad", "s": "skip", "q": "quit",
            "good": "good", "acceptable": "acceptable", "bad": "bad", "skip": "skip", "quit": "quit",
        }.get(raw_rating, raw_rating)
        if rating == "quit":
            break
        if rating == "skip":
            session_skipped_sample_ids.add(sample["sample_id"])
            continue
        if rating not in RATINGS:
            print("入力を確認してください。A、B、C、D、0のいずれかを入力します。")
            continue
        bad = preferred = []
        reasons = []
        segmentation_bad = transcription_bad = exclude = False
        preferred_system = None
        notes = ""
        if rating == "bad":
            while True:
                try:
                    bad = _parse_int_list(input("修正したい区切り番号（カンマ区切り、空欄可）: "))
                    preferred = _parse_int_list(input("良いと思う区切り番号（カンマ区切り、空欄可）: "))
                    print("違和感の理由（複数可、カンマ区切り。空欄可）:")
                    print("助詞 / 述語・補助動詞 / 修飾語 / 複合語 / 英語・日本語混在 / 数値・単位 / 意味のまとまり / 長すぎる / 短すぎる / 間・タイミング / 文字起こし誤り / その他")
                    reasons = _parse_reason_input(input("理由 > "))
                    break
                except ValueError as exc:
                    print(str(exc))
            segmentation_bad = input("字幕の区切り自体に問題がありますか？（はいなら y、空欄は いいえ）: ").strip().lower() == "y"
            transcription_bad = input("文字起こし自体に誤りがありますか？（はいなら y、空欄は いいえ）: ").strip().lower() == "y"
            exclude = input("境界評価から除外しますか？（はいなら y、空欄は いいえ）: ").strip().lower() == "y"
            while True:
                try:
                    print("比較方式: ルール方式 / LLM方式 / 同程度 / どちらでもない / 空欄")
                    preferred_system = _parse_preferred_system_input(input("比較方式 > "))
                    break
                except ValueError as exc:
                    print(str(exc))
            notes = input("メモ（空欄可）: ")
        document = apply_review(
            document,
            sample["sample_id"],
            rating=rating,
            bad_boundaries=bad,
            preferred_boundaries=preferred,
            reasons=reasons,
            segmentation_bad=segmentation_bad,
            transcription_bad=transcription_bad,
            exclude_from_boundary_evaluation=exclude,
            preferred_system=preferred_system,
            notes=notes,
        )
        write_review_document(path, document)
        reviewed += 1
        _write_review_summary(summary_path, _review_summary(document, strict_ids, filter_name=review_filter))
        summary = _review_summary(document, strict_ids, filter_name=review_filter)
        print(f"保存しました。評価済み {summary['reviewed']}件、未評価 {summary['pending']}件")
    summary = _review_summary(document, strict_ids, filter_name=review_filter)
    _write_review_summary(summary_path, summary)
    _print_review_summary(summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Real faster-whisper caption evaluation / human review")
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--runs-root", default="data/runs")
    evaluate.add_argument("--output", required=True)
    evaluate.add_argument("--run-count", type=int, default=5)
    evaluate.add_argument("--target-utterances", type=int, default=100)
    evaluate.add_argument("--max-chars", type=int, default=32)
    evaluate.add_argument("--max-lines", type=int, default=2)
    validate = subparsers.add_parser("validate")
    validate.add_argument("artifact")
    review = subparsers.add_parser("review")
    review.add_argument("artifact")
    review.add_argument("--limit", type=int, default=None)
    review.add_argument("--diagnostics", default=None)
    review.add_argument("--filter", dest="review_filter", choices=("all", "strict-clean"), default="all")
    review.add_argument("--summary-output", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "evaluate":
        output = Path(args.output)
        if output.exists():
            raise SystemExit(f"output already exists: {output}")
        document = build_evaluation_document(
            args.runs_root,
            run_count=args.run_count,
            target_utterances=args.target_utterances,
            max_chars=args.max_chars,
            max_lines=args.max_lines,
        )
        write_review_document(output, document)
        print(json.dumps({
            "artifact": str(output),
            "sampled_run_count": document["sampled_run_count"],
            "sampled_utterance_count": document["sampled_utterance_count"],
            "word_count": document["aggregate"]["word_count"],
            "pending_review_count": document["aggregate"]["pending_review_count"],
            "source_unchanged": all(item["unchanged"] for item in document["source_integrity"]),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate":
        document = load_review_document(args.artifact)
        print(json.dumps({"valid": True, "pending_review_count": document["aggregate"]["pending_review_count"]}, ensure_ascii=False))
        return 0
    if args.review_filter == "strict-clean" and not args.diagnostics:
        raise SystemExit("strict-clean filterには--diagnosticsが必要です。")
    review_interactive(
        args.artifact,
        limit=args.limit,
        diagnostics_path=args.diagnostics,
        review_filter=args.review_filter,
        summary_output=args.summary_output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
