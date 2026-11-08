import json
import tempfile
import unittest
from pathlib import Path

from feedback import build_feedback_prompt, load_feedback, record_feedback


class FeedbackTests(unittest.TestCase):
    def test_record_corrected_feedback_and_build_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.json"
            entry = record_feedback(
                run_dir="data/runs/example",
                original_highlight={"start": 10, "end": 30, "reason": "test"},
                verdict="corrected",
                corrected_start=8,
                corrected_end=34,
                comment="導入を残す",
                path=path,
            )

            self.assertEqual(entry["corrected_highlight"], {"start": 8.0, "end": 34.0})
            database = load_feedback(path)
            self.assertEqual(len(database["entries"]), 1)
            prompt = build_feedback_prompt(path)
            self.assertIn("区間修正", prompt)
            self.assertIn("導入を残す", prompt)

    def test_corrected_feedback_rejects_over_60_seconds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                record_feedback(
                    run_dir="data/runs/example",
                    original_highlight={"start": 0, "end": 30},
                    verdict="corrected",
                    corrected_start=0,
                    corrected_end=61,
                    path=Path(temp_dir) / "feedback.json",
                )

    def test_empty_legacy_file_is_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.json"
            path.write_text("", encoding="utf-8")
            self.assertEqual(load_feedback(path)["entries"], [])


if __name__ == "__main__":
    unittest.main()
