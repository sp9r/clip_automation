import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from caption_fidelity_diagnostics import (
    classify_fidelity,
    diagnose_review_document,
    feasible_partition,
    write_diagnostics,
)


def _words(text: str, *, start: float = 0.0, step: float = 0.2) -> list[dict]:
    return [
        {"word": character, "start": start + index * step, "end": start + index * step + 0.1}
        for index, character in enumerate(text)
    ]


def _sample(sample_id: str, source: str, final: str, *, generation_error: bool = False) -> dict:
    source_words = _words(source)
    return {
        "sample_id": sample_id,
        "source_words": source_words,
        "step2": {
            "status": "generation_error" if generation_error else "ok",
            "captions": [] if generation_error else [{"start": source_words[0]["start"], "end": source_words[-1]["end"], "text": final}],
            "boundaries": [],
        },
        "metrics": {"generation_error": {"type": "ValueError", "message": "test"} if generation_error else None},
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


class CaptionFidelityDiagnosticsTests(unittest.TestCase):
    def test_a_to_i_fidelity_categories_keep_raw_and_normalized_separate(self):
        cases = {
            "exact": ("字幕", "字幕"),
            "punctuation_only": ("字幕。", "字幕"),
            "whitespace_only": ("字幕 です", "字幕です"),
            "unicode_normalization_only": ("e\u0301", "é"),
            "omission": ("字幕です", "字幕で"),
            "duplication": ("字幕です", "字幕ですす"),
            "replacement": ("字幕です", "字幕ます"),
            "reordering": ("字幕です", "字幕すで"),
            "mixed_changes": ("abc123xyz", "abQxyz!"),
        }
        for expected, (source, final) in cases.items():
            with self.subTest(expected=expected):
                result = classify_fidelity(source, final)
                self.assertEqual(result["category"], expected)
                self.assertEqual(result["raw_exact"], expected == "exact")
        self.assertTrue(classify_fidelity("字幕。", "字幕")["normalized_exact"])
        self.assertFalse(classify_fidelity("字幕。", "字幕")["raw_exact"])

    def test_j_feasible_partition_and_k_oversized_token(self):
        words = _words("abcdefghij")
        self.assertTrue(feasible_partition(words, max_chars=4, max_duration=5.0)["feasible"])
        oversized = [{"word": "あ" * 5, "start": 0.0, "end": 0.1}]
        result = feasible_partition(oversized, max_chars=4, max_duration=5.0)
        self.assertFalse(result["feasible"])
        self.assertEqual(result["reason"], "oversized_token")

    def test_l_algorithm_failure_is_distinguished_from_impossible_constraints(self):
        source = "あいうえお"
        feasible = diagnose_review_document({
            "schema_version": 1,
            "samples": [_sample("generation", source, "", generation_error=True)],
        })["samples"][0]
        self.assertEqual(feasible["generation_failure_classification"], "algorithm_failed_despite_feasible")
        self.assertTrue(feasible["feasibility"]["feasible"])
        impossible_sample = _sample("impossible", "", "", generation_error=True)
        impossible_sample["source_words"] = [{"word": "あ" * 40, "start": 0.0, "end": 0.1}]
        impossible = diagnose_review_document({"schema_version": 1, "samples": [impossible_sample]})["samples"][0]
        self.assertEqual(impossible["generation_failure_classification"], "constraint_impossible")

    def test_m_strict_and_normalized_review_eligibility(self):
        document = {
            "schema_version": 1,
            "samples": [
                _sample("exact", "字幕", "字幕"),
                _sample("punctuation", "字幕。", "字幕"),
                _sample("error", "字幕", "", generation_error=True),
            ],
        }
        diagnostic = diagnose_review_document(document)
        aggregate = diagnostic["aggregate"]
        self.assertEqual(aggregate["strict_review_eligible_count"], 1)
        self.assertEqual(aggregate["normalized_review_eligible_count"], 2)
        self.assertEqual(aggregate["category_total"], 3)

    def test_n_original_artifact_is_unchanged_and_o_p_japanese_path_is_safe(self):
        document = {"schema_version": 1, "samples": [_sample("日本語_001", "字幕", "字幕")]}
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "日本語 元artifact.json"
            output = Path(directory) / "診断" / "caption_diagnostics.v1.json"
            original.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            before = hashlib.sha256(original.read_bytes()).hexdigest()
            diagnostic = diagnose_review_document(document, source_artifact_sha256=before)
            write_diagnostics(output, diagnostic)
            self.assertEqual(before, hashlib.sha256(original.read_bytes()).hexdigest())
            self.assertEqual(diagnostic["source_artifact_sha256"], before)
            self.assertTrue(output.exists())
            serialized = output.read_text(encoding="utf-8")
            self.assertIn("字幕", serialized)
            self.assertNotIn(str(original), serialized)

    def test_p_earliest_stage_contract_has_a_to_f(self):
        document = {"schema_version": 1, "samples": [_sample("stage", "字幕。", "字幕")]}
        stage = diagnose_review_document(document)["samples"][0]["earliest_divergence"]
        self.assertEqual(stage["earliest_divergence_stage"], "F_final_output")
        self.assertEqual(
            [item["stage"] for item in stage["stages"]],
            [
                "A_word_flatten",
                "B_boundary_generation",
                "C_caption_text_construction",
                "D_cleanup",
                "E_forced_merge_topic_rebuild",
                "F_final_output",
            ],
        )


if __name__ == "__main__":
    unittest.main()
