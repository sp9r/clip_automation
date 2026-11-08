import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edit_project_adapter import (
    EditProjectBuildError,
    build_edit_project_from_artifacts,
)
from edit_project_io import load_edit_project, write_edit_project


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "edit_project_v1"


class EditProjectAdapterTests(unittest.TestCase):
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

    def _hashes(self):
        names = [
            "caption_project.v2.json",
            "transcript_words.json",
            "edl.json",
            "edit_regions.json",
            "source.mp4",
        ]
        return {name: hashlib.sha256((self.run / name).read_bytes()).hexdigest() for name in names}

    def test_cross_edl_fixture_generates_matching_video_audio_and_caption_sequence(self):
        result = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        project = result.project
        self.assertFalse([item for item in result.diagnostics if item.severity == "error"])
        self.assertEqual(project["schema_version"], "edit_project.v1")
        self.assertEqual(project["sequence"]["duration"], 2.0)
        video = project["tracks"][0]["clips"]
        audio = project["tracks"][1]["clips"]
        self.assertEqual(len(video), 2)
        self.assertEqual(len(audio), 2)
        self.assertEqual(
            [(clip["source"]["start"], clip["source"]["duration"]) for clip in video],
            [(11.0, 1.0), (20.0, 1.0)],
        )
        self.assertEqual(
            [(clip["timeline"]["start"], clip["timeline"]["duration"]) for clip in video],
            [(0.0, 1.0), (1.0, 1.0)],
        )
        self.assertEqual(
            [(video_clip["source"], video_clip["timeline"]) for video_clip in video],
            [(audio_clip["source"], audio_clip["timeline"]) for audio_clip in audio],
        )
        self.assertEqual([caption["text"] for caption in project["captions"]], ["い", "う"])
        self.assertEqual([caption["timeline"] for caption in project["captions"]], [
            {"start": 0.0, "duration": 1.0},
            {"start": 1.0, "duration": 1.0},
        ])
        transcript_words = json.loads((self.run / "transcript_words.json").read_text(encoding="utf-8"))
        self.assertEqual(project["captions"][0]["words"], [
            {"id": "source-word:1", "text": transcript_words[1]["text"], "timeline": {"start": 0.0, "duration": 1.0}},
        ])
        self.assertEqual(project["captions"][1]["words"], [
            {"id": "source-word:2", "text": transcript_words[2]["text"], "timeline": {"start": 1.0, "duration": 1.0}},
        ])
        self.assertFalse([item for item in result.diagnostics if item.code == "caption_word_metadata_omitted_text_mismatch"])

    def test_generation_is_deterministic_and_inputs_are_not_modified(self):
        before = self._hashes()
        first = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        second = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertEqual(first.project, second.project)
        self.assertEqual(first.diagnostics, second.diagnostics)
        self.assertEqual(before, self._hashes())
        self.assertEqual(first.project["metadata"]["schema_version"], "edit_project.v1")
        self.assertEqual(first.project["metadata"]["source_run_id"], "fixture_run")
        self.assertEqual(len(first.project["metadata"]["input_hashes"]), 5)

    def test_atomic_write_load_validate_round_trip_is_semantic(self):
        result = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        path = self.run / "edit_project.json"
        write_edit_project(path, result.project)
        loaded = load_edit_project(path)
        self.assertEqual(loaded, result.project)
        self.assertFalse((self.run / "edit_project.json.tmp").exists())

    def test_manually_corrected_caption_crossing_delete_is_explicitly_unsupported(self):
        path = self.run / "caption_project.v2.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["captions"][0]["display_text"] = "手動修正"
        data["captions"][0]["text_status"] = "manually_corrected"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.assertRaises(EditProjectBuildError) as context:
            build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertTrue(any(item.code == "caption_split_unsafe" and item.severity == "unsupported" for item in context.exception.diagnostics))

    def test_fully_deleted_caption_is_omitted_without_changing_caption_input(self):
        path = self.run / "caption_project.v2.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["captions"][1]["deleted"] = True
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        result = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertEqual([caption["source_caption_id"] for caption in result.project["captions"]], ["cap_000001"])

    def test_manually_corrected_caption_omits_words_with_warning_but_remains_renderable(self):
        path = self.run / "caption_project.v2.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["captions"][0]["display_text"] = "手動修正"
        data["captions"][0]["text_status"] = "manually_corrected"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.run / "edit_regions.json").write_text(json.dumps([
            {"id": "keep_all", "start": 0.0, "end": 4.0, "mode": "keep"}
        ]), encoding="utf-8")
        result = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertNotIn("words", result.project["captions"][0])
        self.assertTrue(any(item.code == "caption_word_metadata_omitted_text_mismatch" and item.severity == "warning" for item in result.diagnostics))
        self.assertEqual(result.project["captions"][0]["text"], "手動修正")

    def test_word_ids_and_sequence_timing_are_deterministic_across_builds(self):
        first = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        second = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertEqual(first.project["captions"][0]["words"], second.project["captions"][0]["words"])
        self.assertEqual(first.project["metadata"]["writer_version"], "clip_automation.edit_project_v1/3")

    def test_display_normalized_caption_projects_each_word_without_punctuation_tokens(self):
        transcript_path = self.run / "transcript_words.json"
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        transcript[2]["text"] = "う、"
        transcript_path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        caption_path = self.run / "caption_project.v2.json"
        caption_project = json.loads(caption_path.read_text(encoding="utf-8"))
        caption_project["captions"][1]["source_text"] = "う、え"
        caption_project["captions"][1]["display_text"] = "うえ"
        caption_project["captions"][1]["text_status"] = "display_normalized"
        caption_path.write_text(json.dumps(caption_project, ensure_ascii=False), encoding="utf-8")
        (self.run / "edit_regions.json").write_text(
            json.dumps([{"id": "keep_all", "start": 0.0, "end": 4.0, "mode": "keep"}]),
            encoding="utf-8",
        )

        result = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertEqual([word["text"] for word in result.project["captions"][1]["words"]], ["う", "え"])
        self.assertEqual("".join(word["text"] for word in result.project["captions"][1]["words"]), "うえ")

    def test_partial_word_crossing_deleted_interval_remains_an_error(self):
        (self.run / "edit_regions.json").write_text(json.dumps([
            {"id": "keep_head", "start": 0.0, "end": 0.5, "mode": "keep"},
            {"id": "delete_middle", "start": 0.5, "end": 1.0, "mode": "delete"},
            {"id": "keep_tail", "start": 1.0, "end": 3.0, "mode": "keep"},
            {"id": "delete_end", "start": 3.0, "end": 4.0, "mode": "delete"},
        ]), encoding="utf-8")
        with self.assertRaises(EditProjectBuildError) as context:
            build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        self.assertTrue(any(item.code == "word_crosses_delete_boundary" and item.severity == "error" for item in context.exception.diagnostics))

    def test_source_path_is_relative_to_contract_directory_when_same_directory(self):
        result = build_edit_project_from_artifacts(
            self.run,
            source_media_probe=self.probe,
            contract_path=self.run / "edit_project.json",
        )
        self.assertEqual(result.project["source_media"][0]["path"], "source.mp4")

    def test_source_path_is_relative_to_nested_contract_directory(self):
        external = Path(self.temp.name) / "media" / "source.mp4"
        external.parent.mkdir(parents=True)
        external.write_bytes(b"external source")
        caption_path = self.run / "caption_project.v2.json"
        caption = json.loads(caption_path.read_text(encoding="utf-8"))
        caption["source_path"] = str(external)
        caption_path.write_text(json.dumps(caption, ensure_ascii=False, indent=2), encoding="utf-8")
        contract_path = Path(self.temp.name) / "planning" / "clips" / "clip_1" / "edit_project.json"
        result = build_edit_project_from_artifacts(
            self.run,
            source_media_probe=self.probe,
            contract_path=contract_path,
        )
        expected = Path(os.path.relpath(external.resolve(), contract_path.parent.resolve())).as_posix()
        self.assertEqual(result.project["source_media"][0]["path"], expected)
        self.assertNotIn("\\", result.project["source_media"][0]["path"])

    def test_relative_source_path_survives_moving_common_parent(self):
        original = Path(self.temp.name) / "original"
        run = original / "source_run"
        run.mkdir(parents=True)
        for path in FIXTURE.iterdir():
            shutil.copy2(path, run / path.name)
        source = run / "source.mp4"
        source.write_bytes(b"movable source")
        contract_path = original / "planning" / "clips" / "clip_1" / "edit_project.json"
        result = build_edit_project_from_artifacts(
            run,
            source_media_probe=self.probe,
            contract_path=contract_path,
        )
        moved = Path(self.temp.name) / "moved"
        shutil.move(str(original), str(moved))
        moved_source = moved / "source_run" / "source.mp4"
        moved_contract = moved / "planning" / "clips" / "clip_1" / "edit_project.json"
        resolved = (moved_contract.parent / result.project["source_media"][0]["path"]).resolve()
        self.assertEqual(resolved, moved_source.resolve())

    def test_unportable_source_path_is_an_explicit_diagnostic(self):
        with patch("edit_project_adapter.os.path.relpath", side_effect=ValueError("different drives")):
            with self.assertRaises(EditProjectBuildError) as context:
                build_edit_project_from_artifacts(
                    self.run,
                    source_media_probe=self.probe,
                    contract_path=Path(self.temp.name) / "other" / "edit_project.json",
                )
        self.assertTrue(any(item.code == "source_media_path_not_portable" for item in context.exception.diagnostics))

    def test_writer_rejects_absolute_source_path(self):
        result = build_edit_project_from_artifacts(self.run, source_media_probe=self.probe)
        result.project["source_media"][0]["path"] = str((self.run / "source.mp4").resolve())
        with self.assertRaises(ValueError):
            write_edit_project(self.run / "edit_project.json", result.project)


if __name__ == "__main__":
    unittest.main()
