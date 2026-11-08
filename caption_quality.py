"""字幕品質評価のためのrenderer-independentな機械指標。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from statistics import median
from typing import Iterable

from caption_settings import validate_max_chars, validate_max_lines


SHORT_CAPTION_DURATION_SECONDS = 0.55


@dataclass(frozen=True)
class CaptionQualityMetrics:
    caption_count: int
    source_word_count: int
    actual_boundary_count: int
    acceptable_boundary_count: int
    boundary_precision: float
    allowed_boundary_recall: float
    empty_caption_count: int
    max_chars_violation_count: int
    max_lines_violation_count: int
    timing_mismatch_count: int
    omitted_character_count: int
    duplicated_character_count: int
    average_char_count: float
    median_char_count: float
    min_char_count: int
    max_char_count: int
    average_duration_seconds: float
    short_caption_count: int
    order_preserved: bool
    exact_source_fidelity: bool
    timestamp_preserved: bool
    allowed_boundary_count: int
    allowed_boundary_hit_count: int
    allowed_boundary_hit_rate: float
    bad_boundary_count: int
    protected_boundary_violation_count: int
    max_lines_application: str

    def to_dict(self) -> dict:
        return asdict(self)


def load_horizontal_fixture(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as file:
        fixture = json.load(file)
    if fixture.get("orientation") != "horizontal":
        raise ValueError("horizontal fixtureではありません。")
    if not isinstance(fixture.get("utterances"), list) or not fixture["utterances"]:
        raise ValueError("fixtureにutterancesがありません。")
    return fixture


def fixture_segments(fixture: dict) -> list[dict]:
    """fixtureを現行caption generatorの入力形式へ変換する。"""

    segments = []
    for utterance in fixture["utterances"]:
        words = []
        for word in utterance["words"]:
            words.append(
                {
                    "word": str(word["word"]),
                    "start": float(word["start"]),
                    "end": float(word["end"]),
                }
            )
        segments.append(
            {
                "start": float(words[0]["start"]),
                "end": float(words[-1]["end"]),
                "text": str(utterance["text"]),
                "words": words,
            }
        )
    return segments


def fixture_boundary_sets(fixture: dict) -> tuple[set[int], set[int]]:
    """utterance内の境界定義をglobal word indexへ変換する。"""

    allowed: set[int] = set()
    protected: set[int] = set()
    offset = 0
    for utterance in fixture["utterances"]:
        for local_index in utterance.get("allowed_break_after", []):
            allowed.add(offset + int(local_index))
        for local_index in utterance.get("protected_break_after", []):
            protected.add(offset + int(local_index))
        offset += len(utterance["words"])
    return allowed, protected


def _caption_ranges(source_words: list[dict], captions: Iterable[dict]) -> tuple[list[tuple[dict, int, int]], bool]:
    source_text = "".join(str(word["word"]) for word in source_words)
    cursor = 0
    ranges: list[tuple[dict, int, int]] = []
    order_preserved = True
    cumulative = []
    total = 0
    for word in source_words:
        total += len(str(word["word"]))
        cumulative.append(total)

    for caption in captions:
        text = str(caption.get("text", ""))
        if not source_text.startswith(text, cursor):
            order_preserved = False
            match = source_text.find(text, cursor + 1) if text else -1
            if match < 0:
                continue
            cursor = match
        end_cursor = cursor + len(text)
        start_word = next((index for index, end in enumerate(cumulative) if end > cursor), None)
        end_word = next((index for index, end in enumerate(cumulative) if end >= end_cursor), None)
        if start_word is None or end_word is None:
            order_preserved = False
            continue
        if cumulative[end_word] != end_cursor:
            order_preserved = False
        ranges.append((caption, start_word, end_word))
        cursor = end_cursor
    return ranges, order_preserved and cursor == len(source_text)


def evaluate_caption_quality(
    source_words: list[dict],
    captions: list[dict],
    *,
    max_chars: int,
    max_lines: int,
    allowed_break_after: set[int] | None = None,
    protected_break_after: set[int] | None = None,
) -> CaptionQualityMetrics:
    """字幕出力をStep 1の機械指標で評価する。

    ``max_lines`` は現在のcaption generatorが表示行を決めないため、
    明示的に ``renderer_owned`` として記録する。改行を追加して見かけのPASSにしない。
    """

    max_chars = validate_max_chars(max_chars)
    max_lines = validate_max_lines(max_lines)
    captions = list(captions)
    source_words = list(source_words)
    source_text = "".join(str(word["word"]) for word in source_words)
    rendered_text = "".join(str(caption.get("text", "")) for caption in captions)
    ranges, order_preserved = _caption_ranges(source_words, captions)

    empty_count = sum(not str(caption.get("text", "")).strip() for caption in captions)
    max_chars_violations = sum(len(str(caption.get("text", ""))) > max_chars for caption in captions)
    max_lines_violations = sum(
        str(caption.get("text", "")).count("\n") + 1 > max_lines for caption in captions
    )
    timing_mismatches = 0
    for caption, start_word, end_word in ranges:
        if abs(float(caption["start"]) - float(source_words[start_word]["start"])) > 0.001:
            timing_mismatches += 1
        if abs(float(caption["end"]) - float(source_words[end_word]["end"])) > 0.001:
            timing_mismatches += 1

    source_counter = {}
    rendered_counter = {}
    for character in source_text:
        source_counter[character] = source_counter.get(character, 0) + 1
    for character in rendered_text:
        rendered_counter[character] = rendered_counter.get(character, 0) + 1
    omitted = sum(max(0, count - rendered_counter.get(character, 0)) for character, count in source_counter.items())
    duplicated = sum(max(0, count - source_counter.get(character, 0)) for character, count in rendered_counter.items())

    actual_boundaries = {end_word for _, _, end_word in ranges[:-1]}
    allowed = set(allowed_break_after or set())
    protected = set(protected_break_after or set())
    allowed_hits = actual_boundaries & allowed
    bad_boundaries = actual_boundaries - allowed if allowed else set()
    protected_violations = actual_boundaries & protected
    lengths = [len(str(caption.get("text", ""))) for caption in captions]
    durations = [
        max(0.0, float(caption.get("end", 0.0)) - float(caption.get("start", 0.0)))
        for caption in captions
    ]
    actual_boundary_count = len(actual_boundaries)
    acceptable_boundary_count = len(allowed_hits)

    return CaptionQualityMetrics(
        caption_count=len(captions),
        source_word_count=len(source_words),
        actual_boundary_count=actual_boundary_count,
        acceptable_boundary_count=acceptable_boundary_count,
        boundary_precision=(acceptable_boundary_count / actual_boundary_count) if actual_boundary_count else 1.0,
        allowed_boundary_recall=(acceptable_boundary_count / len(allowed)) if allowed else 1.0,
        empty_caption_count=empty_count,
        max_chars_violation_count=max_chars_violations,
        max_lines_violation_count=max_lines_violations,
        timing_mismatch_count=timing_mismatches,
        omitted_character_count=omitted,
        duplicated_character_count=duplicated,
        average_char_count=(sum(lengths) / len(lengths)) if lengths else 0.0,
        median_char_count=float(median(lengths)) if lengths else 0.0,
        min_char_count=min(lengths) if lengths else 0,
        max_char_count=max(lengths) if lengths else 0,
        average_duration_seconds=(sum(durations) / len(durations)) if durations else 0.0,
        short_caption_count=sum(duration < SHORT_CAPTION_DURATION_SECONDS for duration in durations),
        order_preserved=order_preserved,
        exact_source_fidelity=rendered_text == source_text,
        timestamp_preserved=timing_mismatches == 0,
        allowed_boundary_count=len(allowed),
        allowed_boundary_hit_count=len(allowed_hits),
        allowed_boundary_hit_rate=(len(allowed_hits) / len(actual_boundaries)) if actual_boundaries else 1.0,
        bad_boundary_count=len(bad_boundaries),
        protected_boundary_violation_count=len(protected_violations),
        max_lines_application="renderer_owned",
    )
