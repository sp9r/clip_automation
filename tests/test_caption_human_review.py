import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

from caption_real_evaluation import (
    _review_summary,
    review_interactive,
    validate_review_document,
    write_review_document,
)


def _sample(sample_id: str, text: str, *, rating: str | None = None) -> dict:
    return {
        "sample_id": sample_id,
        "categories": ["test"],
        "source_words": [
            {"word": text, "start": 0.0, "end": 0.5},
        ],
        "source_text": text,
        "step2": {
            "status": "ok",
            "captions": [{"start": 0.0, "end": 0.5, "text": text}],
            "boundaries": [],
        },
        "metrics": {
            "generic_protected_boundary_count": 0,
            "forced_constraint_boundary_count": 0,
            "generation_status": "ok",
            "exact_source_fidelity": True,
            "timestamp_preserved": True,
        },
        "human_review": {
            "status": "reviewed" if rating else "pending",
            "rating": rating,
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


def _document() -> dict:
    document = {"schema_version": 1, "samples": [
        _sample("real_0001", "今日はいい天気"),
        _sample("real_0002", "新しいモデルを確認しています"),
        _sample("real_0003", "句読点のみ")
    ]}
    return validate_review_document(document)


def _diagnostics(*sample_ids: str) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "caption_diagnostics.v1",
        "samples": [
            {
                "sample_id": sample_id,
                "review_eligible_for_segmentation": {"strict_review_eligible": True},
            }
            for sample_id in sample_ids
        ],
        "aggregate": {"strict_review_eligible_count": len(sample_ids)},
    }


class CaptionHumanReviewTests(unittest.TestCase):
    def test_review_screen_uses_japanese_labels_and_uppercase_shortcuts(self):
        document = _document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "review.json"
            diagnostics_path = root / "diagnostics.json"
            write_review_document(review_path, document)
            diagnostics_path.write_text(json.dumps(_diagnostics("real_0001")), encoding="utf-8")
            output = io.StringIO()
            with patch("builtins.input", side_effect=["q"]), redirect_stdout(output):
                review_interactive(review_path, diagnostics_path=diagnostics_path, review_filter="strict-clean")
            screen = output.getvalue()
            self.assertIn("字幕レビュー", screen)
            self.assertIn("評価してください", screen)
            self.assertIn("入力 >", screen)
            self.assertNotIn("rating:", screen)
            self.assertNotIn("current captions", screen)
            self.assertNotIn("segmentation_bad", screen)

    def test_skip_advances_only_within_session_and_is_pending_again_after_restart(self):
        document = _document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "review.json"
            diagnostics_path = root / "diagnostics.json"
            write_review_document(review_path, document)
            diagnostics_path.write_text(json.dumps(_diagnostics("real_0001", "real_0002")), encoding="utf-8")
            first_output = io.StringIO()
            with patch("builtins.input", side_effect=["s", "q"]), redirect_stdout(first_output):
                first = review_interactive(review_path, diagnostics_path=diagnostics_path, review_filter="strict-clean")
            saved_after_skip = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertIn("サンプルID: real_0001", first_output.getvalue())
            self.assertIn("サンプルID: real_0002", first_output.getvalue())
            self.assertEqual(saved_after_skip["samples"][0]["human_review"]["status"], "pending")
            self.assertIsNone(saved_after_skip["samples"][0]["human_review"]["rating"])
            self.assertEqual(first["reviewed"], 0)
            self.assertEqual(first["pending"], 2)
            second_output = io.StringIO()
            with patch("builtins.input", side_effect=["q"]), redirect_stdout(second_output):
                second = review_interactive(review_path, diagnostics_path=diagnostics_path, review_filter="strict-clean")
            self.assertIn("サンプルID: real_0001", second_output.getvalue())
            self.assertEqual(second["reviewed"], 0)
            self.assertEqual(second["pending"], 2)

    def test_strict_filter_excludes_samples_and_preserves_their_reviews(self):
        document = _document()
        document["samples"][2]["human_review"] = copy.deepcopy(document["samples"][0]["human_review"])
        document["samples"][2]["human_review"].update({"status": "reviewed", "rating": "bad"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "レビュー.json"
            diagnostics_path = root / "診断.json"
            write_review_document(review_path, document)
            diagnostics_path.write_text(json.dumps(_diagnostics("real_0001", "real_0002"), ensure_ascii=False), encoding="utf-8")
            before_excluded = copy.deepcopy(document["samples"][2]["human_review"])
            summary = review_interactive(review_path, diagnostics_path=diagnostics_path, review_filter="strict-clean", limit=0)
            after = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["sample_count"], 2)
            self.assertEqual(summary["pending"], 2)
            self.assertEqual(after["samples"][2]["human_review"], before_excluded)

    def test_resume_starts_at_next_strict_pending_sample_and_maps_one_key_rating(self):
        document = _document()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_path = root / "review.json"
            diagnostics_path = root / "diagnostics.json"
            write_review_document(review_path, document)
            diagnostics_path.write_text(json.dumps(_diagnostics("real_0001", "real_0002")), encoding="utf-8")
            with patch("builtins.input", side_effect=["A"]):
                first = review_interactive(review_path, diagnostics_path=diagnostics_path, review_filter="strict-clean", limit=1)
            saved = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["samples"][0]["human_review"]["rating"], "good")
            self.assertEqual(first["reviewed"], 1)
            with patch("builtins.input", side_effect=["q"]):
                resumed = review_interactive(review_path, diagnostics_path=diagnostics_path, review_filter="strict-clean")
            self.assertEqual(resumed["reviewed"], 1)
            self.assertEqual(resumed["pending"], 1)

    def test_existing_reviewed_sample_is_not_reasked_and_summary_counts_buckets(self):
        document = _document()
        document["samples"][0]["human_review"].update({"status": "reviewed", "rating": "good"})
        document["samples"][1]["human_review"].update({
            "status": "reviewed",
            "rating": "bad",
            "bad_boundaries": [0],
            "reasons": ["semantic chunk", "too long"],
        })
        summary = _review_summary(document, {"real_0001", "real_0002"}, filter_name="strict-clean")
        self.assertEqual(summary["reviewed"], 2)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["rating_counts"], {"good": 1, "acceptable": 0, "bad": 1})
        self.assertEqual(summary["automatic_acceptance_rate"], 0.5)
        self.assertEqual(summary["bad_boundary_count"], 1)
        self.assertEqual(summary["bad_reason_counts"], {"semantic chunk": 1, "too long": 1})
        self.assertEqual(summary["length_bucket_by_rating"]["<=12"]["good"], 1)
        self.assertEqual(summary["length_bucket_by_rating"]["13-16"]["bad"], 1)
        self.assertEqual(summary["duration_bucket_by_rating"]["<1s"]["good"], 1)


if __name__ == "__main__":
    unittest.main()
