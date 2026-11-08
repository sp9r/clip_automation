import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timeline_preparation import TimelinePreparationService, create_native_timeline_project


class TimelinePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "real_copy"
        self.run.mkdir()
        self.segments = [{"start": 0, "end": 3, "words": [
            {"word": "実", "start": 0, "end": 1},
            {"word": "動画", "start": 1, "end": 2},
            {"word": "です", "start": 2, "end": 3},
        ]}]
        self.v1 = {"schema_version": 1, "captions": [
            {"id": 1, "text": "実動画", "start": 0, "end": 2},
            {"id": 2, "text": "です", "start": 2, "end": 3},
        ]}
        (self.run / "clean_transcript.json").write_text(json.dumps(self.segments), encoding="utf-8")
        (self.run / "caption_project.json").write_text(json.dumps(self.v1), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_succeeds_without_changing_source_files(self):
        before = {name: hashlib.sha256((self.run / name).read_bytes()).hexdigest() for name in ("clean_transcript.json", "caption_project.json")}
        report = TimelinePreparationService(self.run).prepare()
        after = {name: hashlib.sha256((self.run / name).read_bytes()).hexdigest() for name in before}
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["word_count"], 3)
        self.assertEqual(report["caption_count"], 2)
        self.assertEqual(before, after)
        self.assertTrue((self.run / "transcript_words.json").is_file())
        self.assertTrue((self.run / "caption_project.v2.json").is_file())

    def test_mismatch_writes_report_only(self):
        self.v1["captions"][0]["text"] = "別動画"
        (self.run / "caption_project.json").write_text(json.dumps(self.v1), encoding="utf-8")
        report = TimelinePreparationService(self.run).prepare()
        self.assertEqual(report["state"], "needs_review")
        self.assertEqual(report["first_mismatch"]["caption_index"], 1)
        self.assertFalse((self.run / "transcript_words.json").exists())
        self.assertFalse((self.run / "caption_project.v2.json").exists())

    def test_interrupted_save_never_publishes_ready_report(self):
        with patch("timeline_preparation._atomic_json_write", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                TimelinePreparationService(self.run).prepare()
        self.assertFalse((self.run / "migration_report.json").exists())

    def test_native_generation_does_not_require_v1_file(self):
        (self.run / "caption_project.json").unlink()
        report = create_native_timeline_project(
            self.run,
            captions=self.v1["captions"],
            source_path="input.mp4",
            edl_path="edl.json",
            default_template_id="standard",
            width=640,
            height=360,
            fps=30,
            max_chars=24,
        )
        self.assertEqual(report["generation"], "native")
        self.assertTrue((self.run / "caption_project.v2.json").is_file())

    def test_native_generation_can_use_source_timeline_before_encoding(self):
        source_segments = [{"start": 10, "end": 13, "words": [
            {"word": "実", "start": 10, "end": 11},
            {"word": "動画", "start": 11, "end": 12},
            {"word": "です", "start": 12, "end": 13},
        ]}]
        source_transcript = self.run / "transcript.json"
        source_transcript.write_text(json.dumps(source_segments), encoding="utf-8")
        report = create_native_timeline_project(
            self.run,
            captions=self.v1["captions"],
            source_path="input.mp4",
            edl_path="edl.json",
            default_template_id="standard",
            width=640,
            height=360,
            fps=30,
            max_chars=24,
            timeline_transcript_path=source_transcript,
        )
        self.assertEqual(report["state"], "ready")
        project = json.loads((self.run / "caption_project.v2.json").read_text(encoding="utf-8"))
        self.assertEqual(project["captions"][0]["start"], 10)
        self.assertEqual(project["captions"][1]["end"], 13)


if __name__ == "__main__":
    unittest.main()
