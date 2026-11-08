import json
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

from caption import generate_subtitles
from highlight import _validate_highlight, analyze_highlights
from jumpcut import generate_jumpcut_data
from pipeline import _create_run_dir, expected_edl_duration, validate_rendered_video


class PipelineCoreTests(unittest.TestCase):
    def test_run_directories_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = _create_run_dir(temp_dir)
            second = _create_run_dir(temp_dir)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_jumpcut_and_caption_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            transcript = temp / "transcript.json"
            transcript.write_text(
                json.dumps(
                    [
                        {
                            "words": [
                                {"start": 0.0, "end": 0.3, "word": "えー", "probability": 0.9},
                                {"start": 0.6, "end": 1.0, "word": "今日は", "probability": 0.9},
                                {"start": 1.1, "end": 1.6, "word": "晴れです。", "probability": 0.9},
                            ]
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            edl = temp / "edl.json"
            clean = temp / "clean.json"
            srt = temp / "captions.srt"
            generate_jumpcut_data(str(transcript), str(edl), str(clean))
            generate_subtitles(str(clean), str(srt), use_llm=False)

            edl_data = json.loads(edl.read_text(encoding="utf-8"))
            self.assertEqual(edl_data, [{"start": 0.6, "end": 1.6}])
            self.assertIn("今日は晴れです", srt.read_text(encoding="utf-8"))

    def test_highlight_validation(self):
        valid = _validate_highlight(
            {"start": 2, "end": 15, "reason": "要点"}, transcript_duration=20
        )
        self.assertEqual(valid["end"], 15.0)
        with self.assertRaises(ValueError):
            _validate_highlight(
                {"start": 10, "end": 30, "reason": "範囲外"},
                transcript_duration=20,
            )

    def test_ollama_failure_is_not_silently_completed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            transcript = temp / "clean.json"
            transcript.write_text(
                json.dumps([{"start": 0.0, "end": 5.0, "text": "テスト"}], ensure_ascii=False),
                encoding="utf-8",
            )
            output = temp / "highlight.json"
            with mock.patch(
                "highlight.urllib.request.urlopen",
                side_effect=urllib.error.URLError("offline"),
            ):
                with self.assertRaises(RuntimeError):
                    analyze_highlights(str(transcript), str(output))
            self.assertFalse(output.exists())

    def test_video_validation_rejects_duration_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            edl = temp / "edl.json"
            edl.write_text(json.dumps([{"start": 0, "end": 3}]), encoding="utf-8")
            self.assertEqual(expected_edl_duration(edl), 3.0)
            with mock.patch(
                "pipeline.probe_video",
                return_value={"codec": "h264", "width": 320, "height": 240, "fps": 30.0, "duration": 1.0},
            ):
                with self.assertRaisesRegex(ValueError, "EDL期待尺"):
                    validate_rendered_video(
                        temp / "video.mp4", edl, width=320, height=240, fps=30
                    )


if __name__ == "__main__":
    unittest.main()
