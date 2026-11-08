import json
import tempfile
import unittest
from pathlib import Path

from caption import generate_subtitles
from caption_quality import (
    evaluate_caption_quality,
    fixture_boundary_sets,
    fixture_segments,
    load_horizontal_fixture,
)
from caption_settings import validate_max_chars, validate_max_lines
from job_manager import JobManager
from project_settings import load_project_settings_presets, validate_project_settings
from subtitle_project import create_caption_project
from timeline_preparation import create_native_timeline_project


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "caption_quality_horizontal_v1.json"


class CaptionQualityStep1Tests(unittest.TestCase):
    def test_boundary_metric_names_have_explicit_precision_semantics(self):
        words = [
            {"word": "A", "start": 0.0, "end": 0.5},
            {"word": "B", "start": 0.5, "end": 1.0},
            {"word": "C", "start": 1.0, "end": 1.5},
        ]
        captions = [
            {"text": "AB", "start": 0.0, "end": 1.0},
            {"text": "C", "start": 1.0, "end": 1.5},
        ]
        metrics = evaluate_caption_quality(
            words, captions, max_chars=32, max_lines=2,
            allowed_break_after={1, 2},
        )
        self.assertEqual(metrics.actual_boundary_count, 1)
        self.assertEqual(metrics.acceptable_boundary_count, 1)
        self.assertEqual(metrics.boundary_precision, 1.0)
        self.assertAlmostEqual(metrics.allowed_boundary_recall, 0.5)
        self.assertEqual(metrics.allowed_boundary_hit_rate, metrics.boundary_precision)

    def test_length_distribution_is_recorded_for_anti_length_hacking_gate(self):
        words = [
            {"word": "あ", "start": 0.0, "end": 0.2},
            {"word": "いう", "start": 0.2, "end": 1.0},
            {"word": "えお", "start": 1.0, "end": 2.0},
        ]
        metrics = evaluate_caption_quality(
            words,
            [
                {"text": "あ", "start": 0.0, "end": 0.2},
                {"text": "いうえお", "start": 0.2, "end": 2.0},
            ],
            max_chars=32, max_lines=2,
        )
        self.assertEqual(metrics.caption_count, 2)
        self.assertEqual(metrics.average_char_count, 2.5)
        self.assertEqual(metrics.median_char_count, 2.5)
        self.assertEqual(metrics.min_char_count, 1)
        self.assertEqual(metrics.max_char_count, 4)
        self.assertAlmostEqual(metrics.average_duration_seconds, 1.0)
        self.assertEqual(metrics.short_caption_count, 1)

    def test_settings_validation_rejects_non_positive_and_bool_values(self):
        self.assertEqual(validate_max_chars(32), 32)
        self.assertEqual(validate_max_lines(2), 2)
        for value in (0, -1, True, False, 81):
            with self.assertRaises(ValueError):
                validate_max_chars(value)
        for value in (0, -1, True, False, 17):
            with self.assertRaises(ValueError):
                validate_max_lines(value)

    def test_horizontal_fixture_has_twenty_to_thirty_utterances_and_word_timestamps(self):
        fixture = load_horizontal_fixture(FIXTURE_PATH)
        self.assertEqual(fixture["sequence"], {"width": 1920, "height": 1080, "fps": 30})
        self.assertGreaterEqual(len(fixture["utterances"]), 20)
        self.assertLessEqual(len(fixture["utterances"]), 30)
        for utterance in fixture["utterances"]:
            self.assertEqual(
                utterance["text"],
                "".join(word["word"] for word in utterance["words"]),
            )
            self.assertEqual(
                utterance["text"],
                "".join(word["word"] for word in fixture_segments({"utterances": [utterance]})[0]["words"]),
            )
            self.assertTrue(all(word["end"] > word["start"] for word in utterance["words"]))

    def test_generator_preserves_existing_max_chars_semantics_and_records_max_lines(self):
        fixture = load_horizontal_fixture(FIXTURE_PATH)
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_path = Path(temp_dir) / "clean_transcript.json"
            srt_path = Path(temp_dir) / "captions.srt"
            transcript_path.write_text(
                json.dumps(fixture_segments(fixture), ensure_ascii=False), encoding="utf-8"
            )
            result = generate_subtitles(
                str(transcript_path), str(srt_path), use_llm=False,
                max_chars=fixture["settings"]["max_chars"],
                max_lines=fixture["settings"]["max_lines"],
            )

        self.assertEqual(result["caption_settings"]["max_chars"], 32)
        self.assertEqual(result["caption_settings"]["max_lines"], 2)
        self.assertEqual(result["caption_settings"]["max_lines_application"], "renderer_owned")
        self.assertTrue(all("\n" not in caption["text"] for caption in result["captions"]))

    def test_quality_metrics_cover_fidelity_timing_and_boundaries_without_claiming_line_application(self):
        fixture = load_horizontal_fixture(FIXTURE_PATH)
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript_path = Path(temp_dir) / "clean_transcript.json"
            srt_path = Path(temp_dir) / "captions.srt"
            transcript_path.write_text(
                json.dumps(fixture_segments(fixture), ensure_ascii=False), encoding="utf-8"
            )
            result = generate_subtitles(str(transcript_path), str(srt_path), use_llm=False, max_chars=32, max_lines=2)
        words = [word for segment in fixture_segments(fixture) for word in segment["words"]]
        allowed, protected = fixture_boundary_sets(fixture)
        metrics = evaluate_caption_quality(
            words, result["captions"], max_chars=32, max_lines=2,
            allowed_break_after=allowed, protected_break_after=protected,
        )

        self.assertTrue(metrics.exact_source_fidelity)
        self.assertTrue(metrics.order_preserved)
        self.assertTrue(metrics.timestamp_preserved)
        self.assertEqual(metrics.empty_caption_count, 0)
        self.assertEqual(metrics.omitted_character_count, 0)
        self.assertEqual(metrics.duplicated_character_count, 0)
        self.assertEqual(metrics.max_lines_application, "renderer_owned")
        self.assertGreaterEqual(metrics.caption_count, 1)

    def test_caption_and_timeline_artifacts_store_the_same_layout_constraints(self):
        fixture = load_horizontal_fixture(FIXTURE_PATH)
        captions = [{"start": 0.0, "end": 1.0, "text": "テスト"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "clean_transcript.json").write_text(
                json.dumps([{"words": [{"word": "テスト", "start": 0.0, "end": 1.0}]}], ensure_ascii=False),
                encoding="utf-8",
            )
            project = create_caption_project(
                captions=captions, source_path="source.mp4", edl_path="edl.json",
                run_dir=str(run_dir), width=1920, height=1080, fps=30,
                max_chars=fixture["settings"]["max_chars"], max_lines=fixture["settings"]["max_lines"],
            )
            native_report = create_native_timeline_project(
                run_dir, captions=captions, source_path="source.mp4", edl_path="edl.json",
                default_template_id="standard", width=1920, height=1080, fps=30,
                max_chars=32, max_lines=2,
            )
            v2 = json.loads((run_dir / "caption_project.v2.json").read_text(encoding="utf-8"))

        expected = {"max_chars": 32, "max_lines": 2, "max_lines_application": "renderer_owned"}
        self.assertEqual(project["caption_settings"], expected)
        self.assertEqual(v2["caption_settings"], expected)
        self.assertEqual(native_report["state"], "ready")

    def test_job_manifest_persists_max_lines_and_invalidates_caption_stage(self):
        settings = {
            "output_width": 1920, "output_height": 1080, "output_fps": 30,
            "max_chars": 32, "max_lines": 3, "use_llm_captions": False,
            "caption_model": "unused", "default_template_id": "standard", "video_encoder": "auto",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            manifest = manager.create_job(
                "source.mp4", is_local=True, settings=settings,
                auto_start=False, pipeline_version="v2",
            )
            stored = manager._load(manifest["job_id"])
            for stage in stored["stages"].values():
                stage.update({"status": "completed", "progress": 1.0})
            manager._save(stored)
            updated = manager.update_settings(manifest["job_id"], {"max_lines": 4})
            rejected = False
            try:
                manager.update_settings(manifest["job_id"], {"max_lines": True})
            except ValueError:
                rejected = True

        self.assertEqual(manifest["settings"]["max_lines"], 3)
        self.assertEqual(updated["settings"]["max_lines"], 4)
        self.assertEqual(updated["stages"]["captions"]["status"], "pending")
        self.assertEqual(updated["stages"]["planning"]["status"], "pending")
        self.assertTrue(rejected)

    def test_project_settings_keep_existing_landscape_and_portrait_max_chars(self):
        presets = load_project_settings_presets()
        self.assertEqual(presets["fhd_landscape"]["max_chars"], 32)
        self.assertEqual(presets["fhd_portrait"]["max_chars"], 20)
        self.assertEqual(presets["fhd_landscape"]["max_lines"], 2)
        with self.assertRaises(ValueError):
            validate_project_settings({"max_chars": 0, "max_lines": 2})
        with self.assertRaises(ValueError):
            validate_project_settings({"max_chars": 32, "max_lines": True})


if __name__ == "__main__":
    unittest.main()
