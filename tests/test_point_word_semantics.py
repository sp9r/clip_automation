import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edit_project_adapter import EditProjectBuildError, build_edit_project_from_artifacts
from planning_core import ChunkingConfig, _record_intersects_window, _transcript_records, chunk_transcript


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "edit_project_v1"


class PointWordPlanningTests(unittest.TestCase):
    def test_zero_duration_words_are_valid_and_equal_time_order_is_stable(self):
        records, duration = _transcript_records([{
            "start": 0.0,
            "end": 2.0,
            "words": [
                {"start": 1.0, "end": 2.0, "word": "A"},
                {"start": 1.0, "end": 1.0, "word": "B"},
                {"start": 1.0, "end": 1.0, "word": "C"},
            ],
        }])
        self.assertEqual(duration, 2.0)
        self.assertEqual([record["text"] for record in records], ["A", "B", "C"])
        with self.assertRaises(ValueError):
            _transcript_records([{"start": 0.0, "end": 1.0, "words": [{"start": 0.5, "end": 0.4, "word": "bad"}]}])

    def test_point_records_are_included_at_window_edges_and_final_window(self):
        transcript = [{
            "start": 0.0,
            "end": 3.0,
            "words": [
                {"start": 0.0, "end": 0.0, "word": "A"},
                {"start": 1.0, "end": 1.0, "word": "B"},
                {"start": 2.0, "end": 2.0, "word": "C"},
            ],
        }]
        chunks = chunk_transcript(transcript, ChunkingConfig(max_chars=100, max_duration=1.0, overlap_duration=0.2))
        occurrences = [index for chunk in chunks for index in chunk["provenance"]["record_indexes"]]
        self.assertIn(0, occurrences)
        self.assertIn(1, occurrences)
        self.assertIn(2, occurrences)
        self.assertGreaterEqual(occurrences.count(1), 2)
        self.assertEqual("AB", chunks[0]["text"])
        self.assertIn("C", "".join(chunk["text"] for chunk in chunks))
        self.assertTrue(_record_intersects_window({"start": 1.0, "end": 1.0}, 0.0, 1.0))
        self.assertTrue(_record_intersects_window({"start": 1.0, "end": 1.0}, 1.0, 2.0))


class PointWordAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "fixture_run"
        self.run.mkdir()
        for path in FIXTURE.iterdir():
            shutil.copy2(path, self.run / path.name)
        (self.run / "source.mp4").write_bytes(b"deterministic fixture source")
        self.probe = {"duration": 30.0, "width": 1920, "height": 1080, "fps": 30.0, "codec": "h264"}

    def tearDown(self):
        self.temp.cleanup()

    def _prepare(self, words, regions, *, status="as_transcribed", display=None):
        (self.run / "transcript_words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        text = "".join(word["text"] for word in words)
        caption = json.loads((self.run / "caption_project.v2.json").read_text(encoding="utf-8"))
        caption["captions"] = [{
            "id": "cap_000001",
            "start_word_id": 0,
            "end_word_id": len(words) - 1,
            "start": words[0]["timeline_start"],
            "end": words[-1]["timeline_end"],
            "source_text": text,
            "display_text": display if display is not None else text,
            "text_status": status,
            "timing_mode": "word_bound",
            "word_timing_overrides": [],
            "template_id": "standard",
        }]
        caption["output"] = {"width": 1080, "height": 1920, "fps": 30}
        (self.run / "caption_project.v2.json").write_text(json.dumps(caption, ensure_ascii=False), encoding="utf-8")
        (self.run / "edl.json").write_text(json.dumps([{"start": 0.0, "end": 3.0}]), encoding="utf-8")
        (self.run / "edit_regions.json").unlink(missing_ok=True)
        return build_edit_project_from_artifacts(
            self.run,
            source_media_probe=self.probe,
            regions=regions,
        )

    @staticmethod
    def _word(word_id, text, start, end):
        return {
            "word_id": word_id,
            "text": text,
            "source_start": start,
            "source_end": end,
            "timeline_start": start,
            "timeline_end": end,
        }

    def test_regions_argument_is_authoritative_without_legacy_file(self):
        result = self._prepare([
            self._word(0, "A", 0.0, 1.0),
            self._word(1, "B", 1.0, 1.0),
            self._word(2, "C", 1.0, 2.0),
        ], [{"id": "keep", "start": 0.0, "end": 3.0, "mode": "keep"}])
        self.assertIn("derived:planning_regions.json", result.project["metadata"]["input_hashes"])
        self.assertNotIn("edit_regions.json", result.project["metadata"]["input_hashes"])

    def test_zero_word_is_preserved_in_caption_but_optional_words_are_omitted(self):
        result = self._prepare([
            self._word(0, "A", 0.0, 1.0),
            self._word(1, "B", 1.0, 1.0),
            self._word(2, "C", 1.0, 2.0),
        ], [{"id": "keep", "start": 0.0, "end": 3.0, "mode": "keep"}])
        caption = result.project["captions"][0]
        self.assertEqual(caption["text"], "ABC")
        self.assertNotIn("words", caption)
        warning = [item for item in result.diagnostics if item.code == "caption_word_metadata_omitted_zero_duration_word"]
        self.assertEqual(len(warning), 1)
        self.assertEqual(warning[0].details["source_word_ids"], [1])

    def test_zero_word_at_keep_delete_boundary_is_deleted(self):
        result = self._prepare([
            self._word(0, "A", 0.0, 1.0),
            self._word(1, "B", 1.0, 1.0),
            self._word(2, "C", 2.0, 3.0),
        ], [
            {"id": "keep_head", "start": 0.0, "end": 1.0, "mode": "keep"},
            {"id": "delete_middle", "start": 1.0, "end": 2.0, "mode": "delete"},
            {"id": "keep_tail", "start": 2.0, "end": 3.0, "mode": "keep"},
        ], status="display_normalized", display="ABC")
        self.assertEqual([caption["text"] for caption in result.project["captions"]], ["A", "C"])
        self.assertFalse(any(item.code == "caption_word_metadata_omitted_zero_duration_word" for item in result.diagnostics))

    def test_zero_word_at_delete_keep_boundary_is_kept(self):
        result = self._prepare([
            self._word(0, "A", 0.0, 1.0),
            self._word(1, "B", 1.0, 1.0),
            self._word(2, "C", 1.0, 2.0),
        ], [
            {"id": "delete_head", "start": 0.0, "end": 1.0, "mode": "delete"},
            {"id": "keep_tail", "start": 1.0, "end": 3.0, "mode": "keep"},
        ])
        self.assertEqual(result.project["captions"][0]["text"], "BC")
        self.assertNotIn("words", result.project["captions"][0])

    def test_final_timeline_point_belongs_to_final_region(self):
        result = self._prepare([
            self._word(0, "A", 2.0, 3.0),
            self._word(1, "B", 3.0, 3.0),
        ], [{"id": "keep", "start": 0.0, "end": 3.0, "mode": "keep"}])
        self.assertEqual(result.project["captions"][0]["text"], "AB")
        self.assertTrue(any(item.code == "caption_word_metadata_omitted_zero_duration_word" for item in result.diagnostics))

    def test_zero_only_run_fails_closed_with_diagnostic(self):
        with self.assertRaises(EditProjectBuildError) as context:
            self._prepare([self._word(0, "A", 1.0, 1.0)], [{"id": "keep", "start": 0.0, "end": 3.0, "mode": "keep"}])
        self.assertTrue(any(item.code == "caption_zero_duration_only_run" for item in context.exception.diagnostics))

    def test_manually_corrected_multi_run_is_unsupported(self):
        with self.assertRaises(EditProjectBuildError) as context:
            self._prepare([
                self._word(0, "A", 0.0, 1.0),
                self._word(1, "B", 2.0, 3.0),
            ], [
                {"id": "keep_head", "start": 0.0, "end": 1.5, "mode": "keep"},
                {"id": "delete_gap", "start": 1.5, "end": 2.0, "mode": "delete"},
                {"id": "keep_tail", "start": 2.0, "end": 3.0, "mode": "keep"},
            ], status="manually_corrected", display="手動修正")
        self.assertTrue(any(item.code == "caption_split_unsafe" for item in context.exception.diagnostics))

    def test_zero_word_first_last_and_multiple_have_same_policy(self):
        for words in (
            [self._word(0, "A", 0.0, 0.0), self._word(1, "B", 0.0, 1.0)],
            [self._word(0, "A", 0.0, 1.0), self._word(1, "B", 1.0, 1.0)],
            [self._word(0, "A", 0.0, 0.0), self._word(1, "B", 0.0, 0.0), self._word(2, "C", 0.0, 1.0)],
        ):
            result = self._prepare(words, [{"id": "keep", "start": 0.0, "end": 3.0, "mode": "keep"}])
            self.assertEqual(result.project["captions"][0]["text"], "".join(word["text"] for word in words))
            self.assertNotIn("words", result.project["captions"][0])


if __name__ == "__main__":
    unittest.main()
