import json
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from spectra_auto_processing_worker import main, run_auto_processing


def _request(media: Path) -> dict:
    return {
        "version": "spectra.auto-caption.request.v1",
        "jobId": "parallel-job-1",
        "mediaPath": str(media),
        "sourceStartUs": 10_000_000,
        "sourceEndUs": 11_000_000,
        "language": "ja",
        "maxChars": 32,
        "maxLines": 2,
        "segmentationMode": "rule",
    }


class ParallelAutoProcessingTests(unittest.TestCase):
    def test_cli_emits_one_terminal_failure_for_missing_media(self):
        with tempfile.TemporaryDirectory(prefix="spectra-parallel-missing-media-") as directory:
            root = Path(directory)
            request_path = root / "request.json"
            output_path = root / "result.json"
            silence_path = root / "silence.json"
            request_path.write_text(json.dumps({**_request(root / "missing.mp4"), "jobId": "missing-media-job"}), encoding="utf-8")
            progress = io.StringIO()
            with patch.object(sys, "stdout", progress):
                exit_code = main([
                    "--request", str(request_path),
                    "--output", str(output_path),
                    "--silence-output", str(silence_path),
                ])
            events = [json.loads(line) for line in progress.getvalue().splitlines() if line.strip()]
            self.assertEqual(exit_code, 1)
            self.assertEqual([(event["state"], event.get("errorCode")) for event in events], [("failed", "MEDIA_NOT_FOUND")])
            self.assertFalse(output_path.exists())
            self.assertFalse(silence_path.exists())

    def test_silence_off_skips_signal_analysis_and_publishes_caption_only(self):
        with tempfile.TemporaryDirectory(prefix="spectra-caption-only-") as directory:
            root = Path(directory)
            media = root / "source.mp4"
            media.write_bytes(b"fixture")
            output = root / "result.json"
            silence_output = root / "silence_analysis.json"
            prepared = root / "range.wav"

            def fake_extract(_media, _start_us, _end_us, _temp_dir, _cancel_event, _progress):
                prepared.write_bytes(b"wav")
                return prepared

            caption_result = {
                "version": "spectra.auto-caption.result.v1", "jobId": "parallel-job-1",
                "sourceStartUs": 10_000_000, "sourceEndUs": 11_000_000, "language": "ja",
                "provider": "faster-whisper+caption.py", "model": "test", "rawWords": [],
                "captions": [], "diagnostics": [], "fallback": {"used": False},
            }

            def fake_run_worker(request, **kwargs):
                self.assertEqual(kwargs["acceleration_mode"], "gpu")
                return caption_result

            with patch("spectra_auto_processing_worker.extract_source_range", side_effect=fake_extract), \
                 patch("spectra_auto_processing_worker.analyze_silences", side_effect=AssertionError("must not analyze silence")), \
                 patch("spectra_auto_processing_worker.run_worker", side_effect=fake_run_worker):
                result = run_auto_processing(
                    {**_request(media), "silenceCut": "off", "accelerationMode": "gpu"},
                    output_path=output, silence_output_path=silence_output,
                )
            self.assertEqual(result["jobId"], "parallel-job-1")
            self.assertTrue(output.is_file())
            self.assertFalse(silence_output.exists())

    def test_shared_range_preparation_and_parallel_jobs(self):
        with tempfile.TemporaryDirectory(prefix="spectra-parallel-test-") as directory:
            root = Path(directory)
            media = root / "source.mp4"
            media.write_bytes(b"fixture")
            output = root / "result.json"
            silence_output = root / "silence_analysis.json"
            started = {"whisper": threading.Event(), "silence": threading.Event()}
            prepared_paths = []

            def fake_extract(_media, start_us, end_us, temp_dir, cancel_event, progress):
                self.assertEqual((start_us, end_us), (10_000_000, 11_000_000))
                path = Path(temp_dir) / "source-range.wav"
                path.write_bytes(b"wav")
                prepared_paths.append(path)
                return path

            def fake_analyze(audio_path, **kwargs):
                self.assertEqual(audio_path, prepared_paths[0])
                self.assertEqual(kwargs["profile"], "youtube")
                self.assertEqual(kwargs["min_silence_us"], 150_000)
                started["silence"].set()
                started["whisper"].wait(2)
                return {
                    "silences": [{"startUs": 200_000, "endUs": 800_000, "durationUs": 600_000}],
                    "keepRanges": [{"startUs": 0, "endUs": 1_000_000, "durationUs": 1_000_000}],
                    "removedDurationUs": 600_000,
                    "settings": {
                        "profile": "youtube", "minSilenceUs": 150_000,
                        "retainPauseUs": 0, "noiseDb": -35.0,
                        "speechProtectionUs": 80_000, "fillerRemoval": False,
                    },
                    "diagnostics": [
                        {"level": "info", "code": "AUDIO_SIGNAL_AUTHORITY", "message": "signal"},
                        {"level": "info", "code": "WHISPER_WORD_PROTECTION", "message": "none"},
                        {"level": "info", "code": "FILLER_REMOVAL_DISABLED", "message": "disabled"},
                    ],
                }

            def fake_run_worker(request, **kwargs):
                self.assertEqual(Path(kwargs["prepared_audio_path"]), prepared_paths[0])
                started["whisper"].set()
                started["silence"].wait(2)
                kwargs["progress_callback"]({"state": "transcribing", "ratio": 0.425, "progressMode": "determinate", "stageRatio": 0.5, "message": "Whisper"})
                return {
                    "version": "spectra.auto-caption.result.v1",
                    "jobId": request["jobId"],
                    "sourceStartUs": 10_000_000,
                    "sourceEndUs": 11_000_000,
                    "language": "ja",
                    "provider": "faster-whisper+caption.py",
                    "model": "test",
                    "rawWords": [{"id": "source-word:0", "text": "テスト", "startUs": 10_500_000, "endUs": 10_600_000}],
                    "captions": [{
                        "id": "caption:0", "text": "テスト", "sourceWordIds": ["source-word:0"],
                        "startUs": 10_500_000, "endUs": 10_600_000,
                        "textStatus": "as_transcribed", "provenance": "caption_algorithm",
                    }],
                    "diagnostics": [],
                    "fallback": {"used": False},
                }

            started_at = time.perf_counter()
            progress = io.StringIO()
            with patch("spectra_auto_processing_worker.extract_source_range", side_effect=fake_extract), \
                 patch("spectra_auto_processing_worker.analyze_silences", side_effect=fake_analyze), \
                 patch("spectra_auto_processing_worker.run_worker", side_effect=fake_run_worker):
                result = run_auto_processing(
                    {**_request(media), "silenceCut": "youtube"}, output_path=output, silence_output_path=silence_output,
                    progress_stream=progress,
                )
            elapsed = time.perf_counter() - started_at
            self.assertLess(elapsed, 1.0)
            self.assertEqual(result["jobId"], "parallel-job-1")
            self.assertTrue(output.is_file())
            analysis = json.loads(silence_output.read_text(encoding="utf-8"))
            self.assertEqual(analysis["silences"][0]["startUs"], 10_000_000 + 200_000)
            self.assertEqual(analysis["settings"]["fillerRemoval"], False)
            self.assertTrue(all(set(item) == {"level", "code", "message"} for item in analysis["diagnostics"]))
            states = [json.loads(line)["state"] for line in progress.getvalue().splitlines()]
            self.assertIn("transcribing", states)
            self.assertIn("silence_analyzing", states)
            transcribing = next(json.loads(line) for line in progress.getvalue().splitlines() if json.loads(line)["state"] == "transcribing" and json.loads(line).get("stageRatio") is not None)
            self.assertEqual(transcribing["progressMode"], "determinate")
            self.assertEqual(transcribing["stageRatio"], 0.5)
            silence_events = [json.loads(line) for line in progress.getvalue().splitlines() if json.loads(line)["state"] == "silence_analyzing"]
            self.assertTrue(all(event["progressMode"] == "indeterminate" for event in silence_events))
            self.assertEqual(states[-1], "completed")

    def test_shared_cancel_does_not_publish_partial_outputs(self):
        with tempfile.TemporaryDirectory(prefix="spectra-parallel-cancel-") as directory:
            root = Path(directory)
            media = root / "source.mp4"
            media.write_bytes(b"fixture")
            output = root / "result.json"
            silence_output = root / "silence_analysis.json"

            def fake_extract(_media, _start_us, _end_us, temp_dir, _cancel_event, _progress):
                path = Path(temp_dir) / "source-range.wav"
                path.write_bytes(b"wav")
                return path

            def cancel_worker(_request, **kwargs):
                kwargs["cancel_event"].set()
                raise RuntimeError("cancelled")

            with patch("spectra_auto_processing_worker.extract_source_range", side_effect=fake_extract), \
                 patch("spectra_auto_processing_worker.analyze_silences", return_value={"silences": [], "keepRanges": [], "removedDurationUs": 0, "settings": {}, "diagnostics": []}), \
                 patch("spectra_auto_processing_worker.run_worker", side_effect=cancel_worker):
                with self.assertRaises(RuntimeError):
                    run_auto_processing(_request(media), output_path=output, silence_output_path=silence_output)
            self.assertFalse(output.exists())
            self.assertFalse(silence_output.exists())

    def test_result_pair_rolls_back_when_second_artifact_publish_fails(self):
        with tempfile.TemporaryDirectory(prefix="spectra-parallel-publish-") as directory:
            root = Path(directory)
            media = root / "source.mp4"
            media.write_bytes(b"fixture")
            output = root / "result.json"
            silence_output = root / "silence_analysis.json"

            def fake_extract(_media, _start_us, _end_us, temp_dir, _cancel_event, _progress):
                path = Path(temp_dir) / "source-range.wav"
                path.write_bytes(b"wav")
                return path

            signal_result = {
                "silences": [],
                "keepRanges": [{"startUs": 0, "endUs": 1_000_000, "durationUs": 1_000_000}],
                "removedDurationUs": 0,
                "settings": {"fillerRemoval": False},
                "diagnostics": [],
            }
            caption_result = {
                "version": "spectra.auto-caption.result.v1",
                "jobId": "parallel-job-1",
                "sourceStartUs": 10_000_000,
                "sourceEndUs": 11_000_000,
                "language": "ja",
                "rawWords": [],
                "captions": [],
            }

            with patch("spectra_auto_processing_worker.extract_source_range", side_effect=fake_extract), \
                 patch("spectra_auto_processing_worker.analyze_silences", return_value=signal_result), \
                 patch("spectra_auto_processing_worker.run_worker", return_value=caption_result), \
                 patch("spectra_auto_processing_worker._write_json_atomically", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    run_auto_processing(
                        _request(media), output_path=output, silence_output_path=silence_output,
                    )
            self.assertFalse(output.exists())
            self.assertFalse(silence_output.exists())


if __name__ == "__main__":
    unittest.main()
