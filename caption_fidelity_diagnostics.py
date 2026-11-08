"""Evaluation-only diagnosis for real caption fidelity and generation failures.

The module reads an existing ``caption_review.v1`` artifact, never rewrites it,
and writes a separate diagnostic artifact in the ignored evaluation directory.
It does not change caption generation behavior.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import unicodedata
import caption as caption_module
from caption_real_evaluation import (
    load_review_document,
    validate_review_document,
)


DIAGNOSTIC_SCHEMA_VERSION = 1
PUNCTUATION = set("、。！？!?…・〜～")
WHITESPACE_RE = re.compile(r"\s+")


def _without_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub("", text)


def _without_punctuation_or_whitespace(text: str) -> str:
    return "".join(character for character in text if character not in PUNCTUATION and not character.isspace())


def _unicode_forms(text: str) -> tuple[str, str]:
    return unicodedata.normalize("NFC", text), unicodedata.normalize("NFKC", text)


def _opcode_spans(source: str, final: str) -> tuple[list[dict], int, int, int]:
    matcher = difflib.SequenceMatcher(a=source, b=final, autojunk=False)
    deleted = []
    inserted = []
    replaced = []
    first_difference = None
    omitted = duplicated = replaced_chars = 0
    has_delete = has_insert = has_replace = False
    for tag, source_start, source_end, final_start, final_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if first_difference is None:
            first_difference = min(source_start, final_start)
        source_text = source[source_start:source_end]
        final_text = final[final_start:final_end]
        has_delete = has_delete or tag == "delete"
        has_insert = has_insert or tag == "insert"
        has_replace = has_replace or tag == "replace"
        if tag in {"delete", "replace"} and source_text:
            deleted.append({"start": source_start, "end": source_end, "text": source_text})
            omitted += len(source_text)
        if tag in {"insert", "replace"} and final_text:
            inserted.append({"start": final_start, "end": final_end, "text": final_text})
            duplicated += len(final_text)
        if tag == "replace":
            replaced.append(
                {
                    "source_start": source_start,
                    "source_end": source_end,
                    "final_start": final_start,
                    "final_end": final_end,
                    "source_text": source_text,
                    "final_text": final_text,
                }
            )
            replaced_chars += min(len(source_text), len(final_text))
    return (
        {
            "first_differing_position": first_difference,
            "deleted_spans": deleted,
            "inserted_spans": inserted,
            "replaced_spans": replaced,
            "source_length": len(source),
            "final_length": len(final),
            "has_delete": has_delete,
            "has_insert": has_insert,
            "has_replace": has_replace,
        },
        omitted,
        duplicated,
        replaced_chars,
    )


def classify_fidelity(source: str, final: str, *, generation_error: bool = False) -> dict:
    """Classify raw and descriptive-normalized fidelity without hiding raw loss."""

    source = str(source)
    final = str(final)
    source_nfc, source_nfkc = _unicode_forms(source)
    final_nfc, final_nfkc = _unicode_forms(final)
    raw_exact = source == final
    whitespace_equal = _without_whitespace(source) == _without_whitespace(final)
    punctuation_equal = _without_punctuation_or_whitespace(source) == _without_punctuation_or_whitespace(final)
    unicode_equal = source_nfc == final_nfc or source_nfkc == final_nfkc
    diff, omitted, duplicated, replaced = _opcode_spans(source, final)

    if generation_error:
        category = "generation_error"
    elif raw_exact:
        category = "exact"
    elif whitespace_equal:
        category = "whitespace_only"
    elif unicode_equal:
        category = "unicode_normalization_only"
    elif punctuation_equal:
        category = "punctuation_only"
    else:
        source_counter = Counter(source)
        final_counter = Counter(final)
        if source_counter == final_counter and len(source) == len(final):
            category = "reordering"
        elif diff["has_delete"] and not diff["has_insert"] and not diff["has_replace"]:
            category = "omission"
        elif diff["has_insert"] and not diff["has_delete"] and not diff["has_replace"]:
            category = "duplication"
        elif diff["has_replace"] and not diff["has_delete"] and not diff["has_insert"]:
            category = "replacement"
        else:
            category = "mixed_changes"
    return {
        "category": category,
        "raw_exact": raw_exact,
        "normalized_exact": punctuation_equal,
        "unicode_nfc_equal": source_nfc == final_nfc,
        "unicode_nfkc_equal": source_nfkc == final_nfkc,
        "diff": diff,
        "omitted_chars": omitted,
        "duplicated_chars": duplicated,
        "replaced_chars": replaced,
    }


def feasible_partition(words: list[dict], *, max_chars: int = 32, max_duration: float = 5.0) -> dict:
    """Check whether any word-aligned partition satisfies hard constraints."""

    if not words:
        return {"feasible": False, "reason": "empty_words", "partition": []}
    count = len(words)
    reachable = [False] * (count + 1)
    previous = [None] * (count + 1)
    reachable[0] = True
    for start in range(count):
        if not reachable[start]:
            continue
        chars = 0
        for end in range(start, count):
            chars += len(str(words[end].get("word", "")))
            duration = float(words[end]["end"]) - float(words[start]["start"])
            if chars > max_chars or duration > max_duration:
                break
            if not reachable[end + 1]:
                reachable[end + 1] = True
                previous[end + 1] = start
    if not reachable[count]:
        oversized = [index for index, word in enumerate(words) if len(str(word.get("word", ""))) > max_chars]
        return {
            "feasible": False,
            "reason": "oversized_token" if oversized else "no_partition",
            "oversized_token_indices": oversized,
            "partition": [],
        }
    partition = []
    cursor = count
    while cursor:
        start = previous[cursor]
        partition.append({"start_word": start, "end_word": cursor - 1})
        cursor = start
    partition.reverse()
    return {"feasible": True, "reason": "feasible", "partition": partition}


def _stage_text(captions: list[dict]) -> str:
    return "".join(str(caption.get("text", "")) for caption in captions)


def _earliest_divergence(words: list[dict], artifact_captions: list[dict], *, max_chars: int = 32) -> dict:
    source = "".join(str(word.get("word", "")) for word in words)
    stages = [{"stage": "A_word_flatten", "text": source}]
    try:
        breaks = caption_module._heuristic_break_indices(words, max_chars=max_chars)
        subtitles = caption_module._build_subtitles(words, breaks)
        stages.append({"stage": "B_boundary_generation", "text": _stage_text(subtitles)})
        # The current production boundary function already constructs timed
        # caption text. Keep this as a named diagnostic stage so a future
        # evaluation hook can distinguish boundary selection from text
        # construction without changing production behavior.
        stages.append({"stage": "C_caption_text_construction", "text": _stage_text(subtitles)})
        cleaned = caption_module._clean_unnatural_subtitles(subtitles, max_chars=max_chars)
        stages.append({"stage": "D_cleanup", "text": _stage_text(cleaned)})
        split_texts = caption_module._split_obvious_topic_boundaries([item["text"] for item in cleaned])
        rebuilt = caption_module._build_subtitles_from_caption_texts(words, split_texts)
        stages.append({"stage": "E_forced_merge_topic_rebuild", "text": _stage_text(rebuilt)})
        final = []
        for subtitle in rebuilt:
            final.append({**subtitle, "text": re.sub(r"[、。！？!?…・〜～]+", "", subtitle["text"])})
        stages.append({"stage": "F_final_output", "text": _stage_text(final)})
    except Exception as exc:  # evaluation artifact must preserve a diagnosis, not abort the corpus
        return {
            "earliest_divergence_stage": "B_boundary_generation",
            "stage_error": {"type": type(exc).__name__, "message": str(exc)},
            "stages": stages,
        }
    for stage in stages:
        if stage["text"] != source:
            return {
                "earliest_divergence_stage": stage["stage"],
                "stage_error": None,
                "stages": [{"stage": item["stage"], "source_equal": item["text"] == source} for item in stages],
            }
    artifact_text = _stage_text(artifact_captions)
    return {
        "earliest_divergence_stage": "artifact_mismatch_after_final" if artifact_text != source else None,
        "stage_error": None,
        "stages": [{"stage": item["stage"], "source_equal": item["text"] == source} for item in stages],
    }


def _timestamp_diagnosis(sample: dict, fidelity: dict) -> dict:
    words = sample.get("source_words", [])
    captions = sample.get("step2", {}).get("captions", [])
    status = sample.get("step2", {}).get("status")
    if status == "generation_error":
        return {
            "mismatch": True,
            "cause": "generation_error",
            "source_first_word": words[0] if words else None,
            "source_last_word": words[-1] if words else None,
            "generated_caption_timestamps": [],
            "boundary_ranges": [],
        }
    generated = [{"start": item.get("start"), "end": item.get("end"), "text": item.get("text", "")} for item in captions]
    mismatch = not bool(generated) or any(item["start"] is None or item["end"] is None for item in generated)
    if not mismatch and words:
        mismatch = abs(float(generated[0]["start"]) - float(words[0]["start"])) > 0.001 or abs(float(generated[-1]["end"]) - float(words[-1]["end"])) > 0.001
    return {
        "mismatch": mismatch,
        "cause": "alignment_failure" if mismatch else None,
        "source_first_word": words[0] if words else None,
        "source_last_word": words[-1] if words else None,
        "generated_caption_timestamps": generated,
        "boundary_ranges": [item.get("boundary_index") for item in sample.get("step2", {}).get("boundaries", [])],
    }


def diagnose_sample(sample: dict, *, max_chars: int = 32, max_duration: float = 5.0) -> dict:
    words = sample.get("source_words", [])
    captions = sample.get("step2", {}).get("captions", [])
    source = "".join(str(word.get("word", "")) for word in words)
    final = _stage_text(captions)
    generation_error = sample.get("step2", {}).get("status") == "generation_error"
    fidelity = classify_fidelity(source, final, generation_error=generation_error)
    feasibility = feasible_partition(words, max_chars=max_chars, max_duration=max_duration)
    generation_failure = None
    if generation_error:
        try:
            # _heuristic_break_indices validates the optimizer result and
            # raises before exposing it. Calling the evaluation-visible
            # optimizer separately lets the diagnosis identify whether the
            # failure was caused by the returned path or by validation.
            production_breaks = caption_module.optimize_boundaries(
                words, max_chars=max_chars, max_duration=max_duration
            )
            production_lengths = []
            start = 0
            for end in production_breaks:
                production_lengths.append(sum(len(str(item.get("word", ""))) for item in words[start : end + 1]))
                start = end + 1
            production_validation = {
                "break_indices": production_breaks,
                "caption_char_lengths": production_lengths,
                "max_char_violation": any(length > max_chars for length in production_lengths),
            }
        except Exception as exc:
            production_validation = {
                "break_indices": None,
                "caption_char_lengths": [],
                "max_char_violation": None,
                "diagnostic_error": {"type": type(exc).__name__, "message": str(exc)},
            }
        oversized_duration_tokens = [
            index for index, word in enumerate(words)
            if float(word.get("end", 0.0)) - float(word.get("start", 0.0)) > max_duration
        ]
        oversized_char_tokens = [
            index for index, word in enumerate(words)
            if len(str(word.get("word", ""))) > max_chars
        ]
        generation_failure = {
            "single_token_over_max_chars": bool(oversized_char_tokens),
            "single_token_over_max_chars_indices": oversized_char_tokens,
            "single_token_over_max_duration": bool(oversized_duration_tokens),
            "single_token_over_max_duration_indices": oversized_duration_tokens,
            "production_validation": production_validation,
            "post_generation_validation_failed": production_validation.get("max_char_violation") is True,
            "error": sample.get("metrics", {}).get("generation_error"),
            "likely_root_cause": (
                "boundary_optimizer_returned_oversized_path_after_collapsed_word_timestamps"
                if production_validation.get("max_char_violation") is True
                and not oversized_char_tokens
                and not oversized_duration_tokens
                and feasibility.get("feasible")
                else "undetermined_generation_failure"
            ),
        }
        stage = {
            "earliest_divergence_stage": "B_boundary_generation",
            "stage_error": sample.get("metrics", {}).get("generation_error"),
            "stages": [
                {"stage": "A_word_flatten", "source_equal": True},
                {"stage": "B_boundary_generation", "source_equal": False},
                {"stage": "C_caption_text_construction", "source_equal": None},
                {"stage": "D_cleanup", "source_equal": None},
                {"stage": "E_forced_merge_topic_rebuild", "source_equal": None},
                {"stage": "F_final_output", "source_equal": None},
            ],
        }
    else:
        stage = _earliest_divergence(words, captions, max_chars=max_chars)
    timestamp = _timestamp_diagnosis(sample, fidelity)
    raw_eligible = not generation_error and fidelity["raw_exact"] and not timestamp["mismatch"]
    normalized_eligible = not generation_error and fidelity["normalized_exact"] and not timestamp["mismatch"]
    if generation_error:
        feasibility_classification = "constraint_impossible" if not feasibility["feasible"] else "algorithm_failed_despite_feasible"
    else:
        feasibility_classification = "not_applicable"
    return {
        "sample_id": sample.get("sample_id"),
        "fidelity": fidelity,
        "earliest_divergence": stage,
        "timestamp": timestamp,
        "feasibility": feasibility,
        "generation_failure_classification": feasibility_classification,
        "generation_failure_diagnosis": generation_failure,
        "review_eligible_for_segmentation": {
            "strict_review_eligible": raw_eligible,
            "normalized_review_eligible": normalized_eligible,
            "normalized_policy": "diagnostic_only_not_acceptance_policy",
        },
    }


def _aggregate(diagnostics: list[dict]) -> dict:
    categories = Counter(item["fidelity"]["category"] for item in diagnostics)
    totals = {
        "total_omitted_chars": sum(item["fidelity"]["omitted_chars"] for item in diagnostics),
        "total_duplicated_chars": sum(item["fidelity"]["duplicated_chars"] for item in diagnostics),
        "total_replaced_chars": sum(item["fidelity"]["replaced_chars"] for item in diagnostics),
    }
    category_names = [
        "exact", "punctuation_only", "whitespace_only", "unicode_normalization_only",
        "omission", "duplication", "replacement", "reordering", "mixed_changes", "generation_error",
    ]
    counts = {name: categories.get(name, 0) for name in category_names}
    return {
        "sample_count": len(diagnostics),
        "category_counts": counts,
        "category_total": sum(counts.values()),
        "raw_exact_count": counts["exact"],
        "punctuation_only_count": counts["punctuation_only"],
        "whitespace_only_count": counts["whitespace_only"],
        "unicode_normalization_only_count": counts["unicode_normalization_only"],
        "omission_count": counts["omission"],
        "duplication_count": counts["duplication"],
        "replacement_count": counts["replacement"],
        "reordering_count": counts["reordering"],
        "mixed_changes_count": counts["mixed_changes"],
        "generation_error_count": counts["generation_error"],
        **totals,
        "strict_review_eligible_count": sum(item["review_eligible_for_segmentation"]["strict_review_eligible"] for item in diagnostics),
        "normalized_review_eligible_count": sum(item["review_eligible_for_segmentation"]["normalized_review_eligible"] for item in diagnostics),
        "generation_failure_classifications": dict(Counter(item["generation_failure_classification"] for item in diagnostics)),
        "timestamp_mismatch_count": sum(item["timestamp"]["mismatch"] for item in diagnostics),
        "earliest_divergence_stages": dict(Counter(item["earliest_divergence"]["earliest_divergence_stage"] for item in diagnostics)),
    }


def diagnose_review_document(
    document: dict,
    *,
    max_chars: int = 32,
    max_duration: float = 5.0,
    source_artifact_sha256: str | None = None,
) -> dict:
    validate_review_document(document)
    diagnostics = [diagnose_sample(sample, max_chars=max_chars, max_duration=max_duration) for sample in document["samples"]]
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "artifact_type": "caption_diagnostics.v1",
        "source_artifact_type": document.get("artifact_type"),
        "source_corpus_id": document.get("corpus_id"),
        "source_artifact_sha256": source_artifact_sha256,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "max_chars": max_chars,
            "max_duration_seconds": max_duration,
            "raw_fidelity_metric": True,
            "normalized_fidelity_metric": True,
            "production_changes": False,
        },
        "samples": diagnostics,
        "aggregate": _aggregate(diagnostics),
    }


def write_diagnostics(path: str | Path, diagnostics: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose real caption fidelity and generation failures")
    parser.add_argument("--review-artifact", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-chars", type=int, default=32)
    parser.add_argument("--max-duration", type=float, default=5.0)
    args = parser.parse_args(argv)
    source_path = Path(args.review_artifact)
    original_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    document = load_review_document(source_path)
    diagnostics = diagnose_review_document(
        document,
        max_chars=args.max_chars,
        max_duration=args.max_duration,
        source_artifact_sha256=original_hash,
    )
    write_diagnostics(args.output, diagnostics)
    after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if original_hash != after_hash:
        raise RuntimeError("original review artifactが診断中に変更されました。")
    summary = diagnostics["aggregate"]
    print(json.dumps({
        "diagnostic_artifact": args.output,
        "original_artifact_unchanged": True,
        "category_counts": summary["category_counts"],
        "total_omitted_chars": summary["total_omitted_chars"],
        "total_duplicated_chars": summary["total_duplicated_chars"],
        "total_replaced_chars": summary["total_replaced_chars"],
        "strict_review_eligible_count": summary["strict_review_eligible_count"],
        "normalized_review_eligible_count": summary["normalized_review_eligible_count"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
