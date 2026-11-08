import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edit_project_adapter import EditProjectBuildError, build_edit_project_from_artifacts
from timeline_project import _effective_word_timing, validate_timeline_project


FIXTURE = Path(__file__).parent / "fixtures" / "edit_project_v1"


class WordTimingParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run = Path(self.temp.name) / "synthetic_run"
        self.run.mkdir()
        for path in FIXTURE.iterdir():
            shutil.copy2(path, self.run / path.name)
        (self.run / "source.mp4").write_bytes(b"synthetic source")
        (self.run / "edl.json").write_text(
            json.dumps([
                {"start": 10.0, "end": 12.0},
                {"start": 20.0, "end": 22.0},
                {"start": 25.0, "end": 26.0},
            ]),
            encoding="utf-8",
        )
        (self.run / "edit_regions.json").write_text(
            json.dumps([{"id": "keep_all", "start": 0.0, "end": 5.0, "mode": "keep"}]),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _prepare(self, *, overrides=None, offsets=None):
        caption_path = self.run / "caption_project.v2.json"
        project = json.loads(caption_path.read_text(encoding="utf-8"))
        words = json.loads((self.run / "transcript_words.json").read_text(encoding="utf-8"))
        overrides = overrides or {}
        offsets = offsets or {}
        for index, caption in enumerate(project["captions"]):
            caption_overrides = overrides.get(index, [])
            if caption_overrides:
                caption["word_timing_overrides"] = caption_overrides
            offset = offsets.get(index, 0.0)
            if offset:
                caption["position_offset"] = offset
            elif "position_offset" in caption:
                del caption["position_offset"]
            caption["timing_mode"] = (
                "position_override" if offset else
                "word_override" if caption_overrides else
                "word_bound"
            )
            start, _ = _effective_word_timing(caption, caption["start_word_id"], words)
            _, end = _effective_word_timing(caption, caption["end_word_id"], words)
            caption["start"] = start + offset
            caption["end"] = end + offset
        caption_path.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        return project, words

    def _build(self):
        return build_edit_project_from_artifacts(
            self.run,
            source_media_probe={"duration": 30.0, "width": 1920, "height": 1080, "fps": 30.0},
        ).project

    @staticmethod
    def _word_times(project):
        return {
            word["id"]: (
                word["timeline"]["start"],
                round(word["timeline"]["start"] + word["timeline"]["duration"], 6),
            )
            for caption in project["captions"]
            for word in caption.get("words", [])
        }

    def test_baseline_and_position_offset_match_effective_timeline(self):
        _, words = self._prepare()
        baseline = self._build()
        self.assertEqual(
            self._word_times(baseline),
            {f"source-word:{word['word_id']}": (word["timeline_start"], word["timeline_end"]) for word in words},
        )

        _, words = self._prepare(offsets={0: 0.25, 1: 0.25})
        shifted = self._build()
        self.assertEqual(
            self._word_times(shifted),
            {
                f"source-word:{word['word_id']}": (word["timeline_start"] + 0.25, word["timeline_end"] + 0.25)
                for word in words
            },
        )

    def test_one_word_override_matches_timeline_effective_timing(self):
        self._prepare(overrides={0: [{"word_id": 1, "start": 1.1, "end": 1.8}]})
        project = self._build()
        self.assertEqual(self._word_times(project)["source-word:1"], (1.1, 1.8))

    def test_multiple_overrides_preserve_identity_and_text(self):
        overrides = {
            0: [
                {"word_id": 0, "start": 0.1, "end": 0.8},
                {"word_id": 1, "start": 1.1, "end": 1.8},
            ]
        }
        project_data, _ = self._prepare(overrides=overrides)
        self.assertEqual(validate_timeline_project(project_data, json.loads((self.run / "transcript_words.json").read_text(encoding="utf-8"))), [])
        project = self._build()
        self.assertEqual(
            self._word_times(project),
            {
                "source-word:0": (0.1, 0.8),
                "source-word:1": (1.1, 1.8),
                "source-word:2": (2.0, 3.0),
                "source-word:3": (3.0, 4.0),
            },
        )
        self.assertEqual(
            [word["id"] for caption in project["captions"] for word in caption["words"]],
            ["source-word:0", "source-word:1", "source-word:2", "source-word:3"],
        )
        self.assertEqual("".join(word["text"] for caption in project["captions"] for word in caption["words"]), "あいうえ")

    def test_override_and_position_offset_are_applied_in_that_order(self):
        self._prepare(
            overrides={0: [{"word_id": 1, "start": 1.1, "end": 1.8}]},
            offsets={0: 0.25, 1: 0.25},
        )
        project = self._build()
        self.assertEqual(
            self._word_times(project),
            {
                "source-word:0": (0.25, 1.25),
                "source-word:1": (1.35, 2.05),
                "source-word:2": (2.25, 3.25),
                "source-word:3": (3.25, 4.25),
            },
        )

    def test_zero_duration_effective_word_omits_editor_words_but_keeps_caption(self):
        self._prepare(overrides={0: [{"word_id": 1, "start": 1.0, "end": 1.0}]})
        result = build_edit_project_from_artifacts(
            self.run,
            source_media_probe={"duration": 30.0, "width": 1920, "height": 1080, "fps": 30.0},
        )
        self.assertNotIn("words", result.project["captions"][0])
        self.assertEqual(result.project["captions"][0]["text"], "あい")
        self.assertTrue(any(item.code == "caption_word_metadata_omitted_zero_duration_word" for item in result.diagnostics))

    def test_reversed_override_is_rejected_without_adapter_repair(self):
        project, words = self._prepare(overrides={0: [{"word_id": 1, "start": 1.8, "end": 1.1}]})
        self.assertTrue(any("invalid timing override bounds" in issue for issue in validate_timeline_project(project, words)))
        with self.assertRaises(EditProjectBuildError) as context:
            self._build()
        self.assertTrue(any(item.code == "invalid_caption_project" for item in context.exception.diagnostics))


if __name__ == "__main__":
    unittest.main()
