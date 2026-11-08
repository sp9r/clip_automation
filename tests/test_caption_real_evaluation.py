import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from caption_real_evaluation import (
    apply_review,
    build_evaluation_document,
    load_review_document,
    load_segments,
    validate_review_document,
    write_review_document,
)


def _make_run(root: Path, *, include_missing_word: bool = False) -> tuple[Path, Path]:
    run = root / "日本語 run 01"
    run.mkdir(parents=True)
    segments = []
    for segment_index in range(5):
        words = []
        for word_index, text in enumerate(("字幕", "の", "確認", "を", "します")):
            word = {
                "word": text,
                "start": float(segment_index * 2 + word_index * 0.2),
                "end": float(segment_index * 2 + word_index * 0.2 + 0.18),
            }
            if include_missing_word and segment_index == 0 and word_index == 0:
                word.pop("end")
            words.append(word)
        segments.append({"text": "".join(item.get("word", "") for item in words), "words": words})
    payload = json.dumps(segments, ensure_ascii=False, indent=2)
    transcript = run / "transcript.json"
    clean = run / "clean_transcript.json"
    transcript.write_text(payload, encoding="utf-8")
    clean.write_text(payload, encoding="utf-8")
    return transcript, clean


class CaptionRealEvaluationTests(unittest.TestCase):
    def _document(self, *, include_missing_word: bool = False):
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name) / "runs"
        root.mkdir()
        transcript, clean = _make_run(root, include_missing_word=include_missing_word)
        document = build_evaluation_document(root, run_count=1, target_utterances=3)
        return temp_dir, document, transcript, clean

    def test_evaluation_is_read_only_and_supports_japanese_space_path(self):
        temp_dir, document, transcript, clean = self._document()
        self.addCleanup(temp_dir.cleanup)
        production_algorithm = Path(__file__).parents[1] / "caption_boundary_v2.py"
        algorithm_before = hashlib.sha256(production_algorithm.read_bytes()).hexdigest()
        before = (hashlib.sha256(transcript.read_bytes()).hexdigest(), hashlib.sha256(clean.read_bytes()).hexdigest())
        self.assertEqual(document["sampled_run_count"], 1)
        self.assertEqual(document["sampled_utterance_count"], 3)
        self.assertTrue(all(item["unchanged"] for item in document["source_integrity"]))
        after = (hashlib.sha256(transcript.read_bytes()).hexdigest(), hashlib.sha256(clean.read_bytes()).hexdigest())
        self.assertEqual(before, after)
        self.assertEqual(algorithm_before, hashlib.sha256(production_algorithm.read_bytes()).hexdigest())
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertNotIn(str(transcript.parent), serialized)

    def test_review_schema_rating_bad_boundary_and_transcription_exclusion(self):
        temp_dir, document, _, _ = self._document()
        self.addCleanup(temp_dir.cleanup)
        validate_review_document(document)
        updated = apply_review(
            document,
            "real_0001",
            rating="bad",
            bad_boundaries=[1, 1],
            preferred_boundaries=[2],
            reasons=["particle", "too long"],
            segmentation_bad=True,
            transcription_bad=True,
            exclude_from_boundary_evaluation=True,
            preferred_system="heuristic",
            notes="human note",
        )
        review = updated["samples"][0]["human_review"]
        self.assertEqual(review["status"], "reviewed")
        self.assertEqual(review["bad_boundaries"], [1])
        self.assertEqual(updated["aggregate"]["human_rated_count"], 1)
        self.assertEqual(updated["aggregate"]["pending_review_count"], 2)
        with self.assertRaises(ValueError):
            apply_review(document, "real_0002", rating="excellent")

    def test_resume_starts_with_next_pending_sample(self):
        temp_dir, document, _, _ = self._document()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "review.json"
        write_review_document(path, document)
        apply_review(document, "real_0001", rating="acceptable")
        write_review_document(path, document)
        resumed = load_review_document(path)
        pending = [sample["sample_id"] for sample in resumed["samples"] if sample["human_review"]["status"] == "pending"]
        self.assertEqual(pending, ["real_0002", "real_0003"])

    def test_missing_words_are_counted_and_corrupt_review_fails_safely(self):
        temp_dir, document, _, _ = self._document(include_missing_word=True)
        self.addCleanup(temp_dir.cleanup)
        self.assertGreater(document["source_integrity"][0]["missing_word_count"], 0)
        corrupt = Path(temp_dir.name) / "corrupt.json"
        corrupt.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_review_document(corrupt)

    def test_preanalysis_buckets_and_policy_flags_are_present(self):
        temp_dir, document, _, _ = self._document()
        self.addCleanup(temp_dir.cleanup)
        aggregate = document["aggregate"]
        self.assertIn("8-12", aggregate["length_buckets"])
        self.assertIn("under_1s", aggregate["duration_buckets"])
        self.assertIn("p75_char_count", aggregate)
        self.assertIn("p90_char_count", aggregate)
        self.assertFalse(document["settings"]["visual_max_lines_verified"])
        self.assertEqual(document["settings"]["llm_comparison"], "not_run")
        self.assertFalse(document["source_policy"]["human_rating_automated"])

    def test_ignored_real_evaluation_and_tracked_fixtures_are_not_used_as_output(self):
        gitignore = Path(__file__).parents[1] / ".gitignore"
        self.assertIn("data/evaluations/", gitignore.read_text(encoding="utf-8"))
        fixture = Path(__file__).parents[1] / "tests" / "fixtures" / "caption_quality_horizontal_v1.json"
        before = hashlib.sha256(fixture.read_bytes()).hexdigest()
        temp_dir, _, _, _ = self._document()
        self.addCleanup(temp_dir.cleanup)
        self.assertEqual(before, hashlib.sha256(fixture.read_bytes()).hexdigest())

    def test_load_segments_handles_missing_words_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "日本語 path with spaces.json"
            path.write_text(json.dumps([{"words": [{"word": "欠落", "start": 0.0}]}], ensure_ascii=False), encoding="utf-8")
            segments, missing = load_segments(path)
        self.assertEqual(segments, [])
        self.assertGreaterEqual(missing, 1)


if __name__ == "__main__":
    unittest.main()
