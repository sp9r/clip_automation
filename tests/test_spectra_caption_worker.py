import io
import json
import ast
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_caption_contract import AutoCaptionContractError, parse_result
from job_control import PipelineCancelled
from spectra_caption_worker import (
    ProgressEmitter,
    WorkerCancelled,
    WorkerFailure,
    _repair_point_caption_intervals,
    build_range_extract_command,
    run_worker,
)
from transcribe import WhisperAccelerationError


def request(media_path: str, *, mode: str = "auto") -> dict:
    return {
        "version": "spectra.auto-caption.request.v1",
        "jobId": "worker-job-1",
        "mediaPath": media_path,
        "sourceStartUs": 10_000_000,
        "sourceEndUs": 15_000_000,
        "language": "ja",
        "maxChars": 32,
        "maxLines": 2,
        "segmentationMode": mode,
    }


def fake_transcriber(_path: str, **kwargs) -> list[dict]:
    if kwargs["cancel_event"].is_set():
        raise WorkerCancelled("cancelled")
    kwargs["progress_callback"](1.0, "done")
    return [{
        "start": 0.0,
        "end": 1.2,
        "text": " 私です",
        "words": [
            {"start": 0.5, "end": 0.8, "word": " 私", "probability": 0.91},
            {"start": 0.9, "end": 1.1, "word": "です", "probability": 0.88},
        ],
    }]


class FakeExtractor:
    def __init__(self, *, cancel_after=False):
        self.calls = []
        self.created_path: Path | None = None
        self.cancel_after = cancel_after

    def __call__(self, media_path, source_start_us, source_end_us, temp_dir, cancel_event, progress):
        self.calls.append((media_path, source_start_us, source_end_us))
        self.created_path = Path(temp_dir) / "source-range.wav"
        self.created_path.write_bytes(b"fake wav")
        progress("preparing", 0.1, "prepared")
        if self.cancel_after:
            cancel_event.set()
        return self.created_path


def fake_captions(_transcript_path, _srt_path, **kwargs) -> dict:
    return {
        "method": "llm",
        "captions": [{
            "start": 0.5,
            "end": 1.1,
            "text": " 私です",
            "start_word_id": 0,
            "end_word_id": 1,
            "text_status": "as_transcribed",
        }],
    }


class SpectraCaptionWorkerTests(unittest.TestCase):
    def test_progress_callback_is_forwarded_without_stream_and_marks_measured_stage(self):
        events = []
        emitter = ProgressEmitter("progress-job", callback=events.append)
        emitter.emit("transcribing", 0.45, "Whisper", progress_mode="determinate", stage_ratio=0.5)
        self.assertEqual(events[0]["progressMode"], "determinate")
        self.assertEqual(events[0]["stageRatio"], 0.5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="spectra-worker-test-")
        self.root = Path(self.temp.name)
        self.media = self.root / "source with spaces.mp4"
        self.media.write_bytes(b"fixture")
        # 既存テストは固定の2語を前提にするため文節結合は無効化し、専用テストだけ有効にする。
        self.merge_patch = patch("spectra_caption_worker.merge_bunsetsu_units", side_effect=lambda transcript, *_args: transcript)
        self.merge_patch.start()

    def tearDown(self):
        self.merge_patch.stop()
        self.temp.cleanup()

    def test_range_command_is_argv_only_and_targets_requested_interval(self):
        command = build_range_extract_command(
            str(self.media), 10_000_000, 15_000_000, str(self.root / "range.wav"), ffmpeg_path="ffmpeg.exe"
        )
        self.assertEqual(command[0], "ffmpeg.exe")
        self.assertIn("-ss", command)
        self.assertEqual(command[command.index("-ss") + 1], "10.000000")
        self.assertEqual(command[command.index("-t") + 1], "5.000000")
        self.assertNotIn(" ".join(command), command)

    def test_valid_request_preserves_raw_words_maps_range_and_cleans_temp(self):
        extractor = FakeExtractor()
        progress = io.StringIO()
        output = self.root / "result.json"
        with patch("spectra_caption_worker.generate_subtitles", side_effect=fake_captions):
            result = run_worker(
                request(str(self.media)), output_path=output, progress_stream=progress,
                range_extractor=extractor, transcriber=fake_transcriber,
            )
        self.assertEqual(extractor.calls[0][1:], (10_000_000, 15_000_000))
        self.assertEqual(result["rawWords"][0]["text"], " 私")
        self.assertEqual(result["rawWords"][0]["startUs"], 10_500_000)
        self.assertEqual(result["rawWords"][0]["id"], "source-word:0")
        self.assertEqual(result["captions"][0]["sourceWordIds"], ["source-word:0", "source-word:1"])
        self.assertEqual(result["captions"][0]["startUs"], 10_500_000)
        self.assertEqual(result["diagnostics"][0]["code"], "NO_FINE_ACOUSTIC_ALIGNMENT")
        self.assertTrue(output.is_file())
        self.assertFalse(extractor.created_path.exists())
        events = [json.loads(line) for line in progress.getvalue().splitlines()]
        self.assertEqual([event["state"] for event in events], ["preparing", "preparing", "transcribing", "transcribing", "segmenting", "segmenting", "completed"])
        self.assertEqual(events[-2]["message"], "字幕結果を検証中…")
        self.assertNotIn(str(self.media), output.read_text(encoding="utf-8"))

    def test_high_quality_success_and_rule_fallback_are_explicit(self):
        with patch("spectra_caption_worker.generate_subtitles", return_value={
            "method": "llm", "captions": [{"start": 0.5, "end": 1.1, "text": " 私です", "start_word_id": 0, "end_word_id": 1, "text_status": "as_transcribed"}]
        }):
            result = run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)
        self.assertFalse(result["fallback"]["used"])
        with patch("spectra_caption_worker.generate_subtitles", return_value={
            "method": "llm+fallback", "captions": [{"start": 0.5, "end": 1.1, "text": " 私です", "start_word_id": 0, "end_word_id": 1, "text_status": "as_transcribed"}]
        }):
            result = run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)
        self.assertTrue(result["fallback"]["used"])
        self.assertEqual(result["diagnostics"][1]["code"], "LLM_SEGMENTATION_FALLBACK")

    def test_rule_mode_does_not_claim_llm_fallback(self):
        captured = {}

        def generator(_transcript_path, _srt_path, **kwargs):
            captured.update(kwargs)
            return {"method": "heuristic", "captions": [{"start": 0.5, "end": 1.1, "text": " 私です", "start_word_id": 0, "end_word_id": 1, "text_status": "as_transcribed"}]}

        with patch("spectra_caption_worker.generate_subtitles", side_effect=generator):
            result = run_worker(request(str(self.media), mode="rule"), range_extractor=FakeExtractor(), transcriber=fake_transcriber)
        self.assertFalse(captured["use_llm"])
        self.assertFalse(result["fallback"]["used"])
        self.assertEqual(result["diagnostics"][1]["code"], "RULE_SEGMENTATION")

    def test_failsafe_segmentation_is_success_with_path_free_diagnostics(self):
        with patch("spectra_caption_worker.generate_subtitles", return_value={
            "method": "failsafe",
            "diagnostics": [
                {"level": "warning", "code": "CAPTION_HEURISTIC_FALLBACK", "message": "heuristic failed; safe segmentation used"},
                {"level": "warning", "code": "CAPTION_FAILSAFE_FALLBACK", "message": "deterministic segmentation used"},
            ],
            "captions": [{"start": 0.5, "end": 1.1, "text": " 私です", "start_word_id": 0, "end_word_id": 1, "text_status": "as_transcribed"}],
        }):
            result = run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)

        self.assertTrue(result["fallback"]["used"])
        self.assertEqual(result["fallback"]["reason"], "Deterministic lossless caption segmentation was used.")
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn("CAPTION_HEURISTIC_FALLBACK", codes)
        self.assertIn("CAPTION_FAILSAFE_FALLBACK", codes)

    def test_real_worker_path_failsafe_result_round_trips_through_v1_contract(self):
        output = self.root / "failsafe-result.json"
        with patch("caption._llm_subtitles", side_effect=RuntimeError("synthetic LLM failure")), \
             patch("caption._heuristic_break_indices", side_effect=RuntimeError("synthetic heuristic failure")):
            result = run_worker(
                request(str(self.media)), output_path=output,
                range_extractor=FakeExtractor(), transcriber=fake_transcriber,
            )

        parsed = parse_result(json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(parsed["jobId"], "worker-job-1")
        self.assertEqual([word["id"] for word in parsed["rawWords"]], ["source-word:0", "source-word:1"])
        self.assertEqual([word_id for caption in parsed["captions"] for word_id in caption["sourceWordIds"]], ["source-word:0", "source-word:1"])
        self.assertEqual(parsed["rawWords"][-1]["endUs"], parsed["captions"][-1]["endUs"])
        self.assertTrue(parsed["fallback"]["used"])
        self.assertIn("CAPTION_FAILSAFE_FALLBACK", {item["code"] for item in parsed["diagnostics"]})
        self.assertEqual(result["rawWords"], parsed["rawWords"])

    def test_result_assembly_failure_rebuilds_from_raw_words(self):
        bad_ownership = {
            "method": "llm",
            "captions": [{"start": 0.5, "end": 1.1, "text": " 私", "start_word_id": 0, "end_word_id": 0}],
        }
        with patch("spectra_caption_worker.generate_subtitles", return_value=bad_ownership):
            result = run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)

        owned = [word_id for caption in result["captions"] for word_id in caption["sourceWordIds"]]
        self.assertEqual(owned, ["source-word:0", "source-word:1"])
        self.assertTrue(result["fallback"]["used"])
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn("CAPTION_RESULT_ASSEMBLY_FALLBACK", codes)
        self.assertIn("CAPTION_OWNERSHIP_FAILED", codes)

    def test_no_usable_captions_rebuilds_from_raw_words(self):
        with patch("spectra_caption_worker.generate_subtitles", return_value={"method": "llm", "captions": []}):
            result = run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)

        self.assertGreater(len(result["captions"]), 0)
        self.assertEqual(
            [word_id for caption in result["captions"] for word_id in caption["sourceWordIds"]],
            ["source-word:0", "source-word:1"],
        )
        self.assertIn("CAPTION_RESULT_ASSEMBLY_FAILED", {item["code"] for item in result["diagnostics"]})

    def test_contract_parse_failure_is_reported_if_second_boundary_cannot_validate(self):
        valid = {"method": "heuristic", "captions": [{
            "start": 0.5,
            "end": 1.1,
            "text": " 私です",
            "start_word_id": 0,
            "end_word_id": 1,
            "text_status": "as_transcribed",
        }]}
        with patch("spectra_caption_worker.generate_subtitles", return_value=valid), \
                patch("spectra_caption_worker.parse_result", side_effect=AutoCaptionContractError("contract rejected")):
            with self.assertRaises(WorkerFailure) as context:
                run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)
        self.assertEqual(context.exception.code, "CAPTION_RESULT_CONTRACT_FAILED")

    def test_invalid_raw_timing_fails_without_segmentation_fallback(self):
        def invalid_transcriber(_path: str, **_kwargs) -> list[dict]:
            return [{"start": 0.0, "end": 1.0, "words": [{"start": 0.8, "end": 0.2, "word": "不正"}]}]

        with patch("spectra_caption_worker.generate_subtitles", return_value={"method": "heuristic", "captions": []}):
            with self.assertRaises(WorkerFailure) as context:
                run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=invalid_transcriber)
        self.assertEqual(context.exception.code, "WHISPER_TIMING_INVALID")

    def test_point_word_repair_edge_cases(self):
        def repaired(captions, start, end):
            return _repair_point_caption_intervals(captions, source_start_us=start, source_end_us=end)

        beginning = [{"startUs": 0, "endUs": 0}, {"startUs": 0, "endUs": 0}, {"startUs": 2, "endUs": 4}]
        self.assertTrue(repaired(beginning, 0, 4))
        self.assertTrue(all(item["endUs"] > item["startUs"] for item in beginning))
        ending = [{"startUs": 0, "endUs": 2}, {"startUs": 2, "endUs": 2}, {"startUs": 2, "endUs": 2}]
        self.assertTrue(repaired(ending, 0, 4))
        all_points = [{"startUs": 0, "endUs": 0} for _ in range(4)]
        self.assertTrue(repaired(all_points, 0, 4))
        with self.assertRaises(WorkerFailure) as context:
            repaired([{"startUs": 0, "endUs": 0}], 0, 0)
        self.assertEqual(context.exception.code, "CAPTION_POINT_TIMING_FAILED")

    def test_failure_artifact_survives_cleanup_rotates_and_redacts(self):
        bad_ownership = {"method": "llm", "captions": [{"start": 0.5, "end": 1.1, "text": " 私", "start_word_id": 0, "end_word_id": 0}]}
        artifact_root = self.root / "failure-root"
        artifact_root.mkdir()
        with patch("spectra_caption_worker.tempfile.gettempdir", return_value=str(artifact_root)), \
                patch("spectra_caption_worker.generate_subtitles", return_value=bad_ownership), \
                patch("spectra_caption_worker.deterministic_failsafe_captions", side_effect=ValueError("safe rebuild failed")):
            for index in range(4):
                with self.assertRaises(WorkerFailure):
                    run_worker({**request(str(self.media)), "jobId": f"failure-job-{index}"}, range_extractor=FakeExtractor(), transcriber=fake_transcriber)
        failure_dirs = list((artifact_root / "spectra-auto-caption-failures").iterdir())
        self.assertEqual(len(failure_dirs), 3)
        failure_json = next(directory / "failure.json" for directory in failure_dirs)
        payload = json.loads(failure_json.read_text(encoding="utf-8"))
        self.assertEqual(payload["request"]["mediaPath"], "[redacted]")
        self.assertNotIn(str(self.media), failure_json.read_text(encoding="utf-8"))
        self.assertTrue((failure_json.parent / "transcript.json").is_file())

    def test_failure_artifact_error_does_not_mask_original_failure(self):
        with patch("spectra_caption_worker._write_failure_artifact", side_effect=RuntimeError("artifact write failed")):
            with patch("spectra_caption_worker.generate_subtitles", side_effect=ValueError("caption failed")):
                with self.assertRaises(WorkerFailure) as context:
                    run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=fake_transcriber)
        self.assertEqual(context.exception.code, "CAPTION_SEGMENTATION_FAILED")

    def test_point_word_caption_run_gets_derived_display_interval_without_mutating_raw_words(self):
        def point_transcriber(_path: str, **kwargs) -> list[dict]:
            return [{
                "start": 0.1,
                "end": 1.0,
                "text": "責任point wordsです",
                "words": [
                    {"start": 0.1, "end": 0.5, "word": "責任"},
                    {"start": 0.5, "end": 0.5, "word": "point"},
                    {"start": 0.5, "end": 0.5, "word": "words"},
                    {"start": 0.52, "end": 1.0, "word": "です"},
                ],
            }]

        point_captions = {
            "method": "heuristic",
            "captions": [
                {"start": 0.1, "end": 0.5, "text": "責任", "start_word_id": 0, "end_word_id": 0, "text_status": "as_transcribed"},
                {"start": 0.5, "end": 0.5, "text": "point", "start_word_id": 1, "end_word_id": 1, "text_status": "as_transcribed"},
                {"start": 0.5, "end": 0.5, "text": "words", "start_word_id": 2, "end_word_id": 2, "text_status": "as_transcribed"},
                {"start": 0.52, "end": 1.0, "text": "です", "start_word_id": 3, "end_word_id": 3, "text_status": "as_transcribed"},
            ],
        }
        with patch("spectra_caption_worker.generate_subtitles", return_value=point_captions):
            result = run_worker(
                request(str(self.media), mode="rule"),
                range_extractor=FakeExtractor(),
                transcriber=point_transcriber,
            )
        self.assertEqual(result["rawWords"][1]["startUs"], result["rawWords"][1]["endUs"])
        self.assertTrue(all(caption["endUs"] > caption["startUs"] for caption in result["captions"]))
        self.assertEqual([caption["startUs"] for caption in result["captions"]], [10_100_000, 10_500_000, 10_510_000, 10_520_000])
        self.assertIn("POINT_WORD_DISPLAY_TIMING", {item["code"] for item in result["diagnostics"]})

    def test_malformed_request_and_missing_media_fail_without_result(self):
        malformed = request(str(self.media))
        malformed["sourceStartUs"] = 1.5
        with self.assertRaises(AutoCaptionContractError):
            run_worker(malformed)
        progress = io.StringIO()
        missing = request(str(self.root / "missing.mp4"))
        with self.assertRaises(WorkerFailure):
            run_worker(missing, progress_stream=progress)
        self.assertIn('"errorCode":"MEDIA_NOT_FOUND"', progress.getvalue())

    def test_empty_whisper_transcript_reports_caption_stage_and_no_speech(self):
        progress = io.StringIO()

        def empty_transcriber(_path: str, **_kwargs) -> list[dict]:
            return []

        with self.assertRaisesRegex(WorkerFailure, "字幕区切り") as raised:
            run_worker(
                request(str(self.media)),
                range_extractor=FakeExtractor(),
                transcriber=empty_transcriber,
                progress_stream=progress,
            )

        self.assertEqual(raised.exception.code, "NO_SPEECH")
        events = [json.loads(line) for line in progress.getvalue().splitlines()]
        self.assertEqual(events[-1]["state"], "failed")
        self.assertEqual(events[-1]["errorCode"], "NO_SPEECH")
        self.assertEqual(events[-1]["diagnostics"][0]["code"], "NO_SPEECH")

    def test_whisper_no_speech_value_error_reports_transcription_stage(self):
        progress = io.StringIO()

        def no_speech_transcriber(_path: str, **_kwargs) -> list[dict]:
            raise ValueError("発話を検出できませんでした。")

        with self.assertRaisesRegex(WorkerFailure, "文字起こし") as raised:
            run_worker(
                request(str(self.media)),
                range_extractor=FakeExtractor(),
                transcriber=no_speech_transcriber,
                progress_stream=progress,
            )

        self.assertEqual(raised.exception.code, "NO_SPEECH")
        events = [json.loads(line) for line in progress.getvalue().splitlines()]
        self.assertEqual(events[-1]["state"], "failed")
        self.assertEqual(events[-1]["errorCode"], "NO_SPEECH")

    def test_cancel_and_transcriber_failure_cleanup_temporary_range(self):
        cancelled = FakeExtractor(cancel_after=True)
        with self.assertRaises(WorkerCancelled):
            run_worker(request(str(self.media)), range_extractor=cancelled, transcriber=fake_transcriber)
        self.assertFalse(cancelled.created_path.exists())
        failed = FakeExtractor()

        def failing_transcriber(_path: str, **_kwargs):
            raise RuntimeError("transcriber failed")

        with self.assertRaises(RuntimeError):
            run_worker(request(str(self.media)), range_extractor=failed, transcriber=failing_transcriber)
        self.assertFalse(failed.created_path.exists())

        cancelled_by_pipeline = FakeExtractor()

        def pipeline_cancelled_transcriber(_path: str, **_kwargs):
            raise PipelineCancelled("cancelled")

        with self.assertRaises(WorkerCancelled):
            run_worker(
                request(str(self.media)),
                range_extractor=cancelled_by_pipeline,
                transcriber=pipeline_cancelled_transcriber,
            )
        self.assertFalse(cancelled_by_pipeline.created_path.exists())

    def test_interrupted_transcription_does_not_leave_empty_transcript_sentinel(self):
        prepared = self.root / "prepared-range.wav"
        prepared.write_bytes(b"wav")

        def interrupted_transcriber(_path: str, **_kwargs):
            raise RuntimeError("transcription interrupted")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            run_worker(
                request(str(self.media)),
                prepared_audio_path=prepared,
                transcriber=interrupted_transcriber,
            )

        self.assertFalse((prepared.parent / "transcript.json").exists())

    def test_worker_imports_only_transcription_and_caption_contracts(self):
        worker_path = Path(__file__).resolve().parents[1] / "spectra_caption_worker.py"
        tree = ast.parse(worker_path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            for alias in node.names
        )
        self.assertNotIn("pipeline", imported)
        self.assertNotIn("planning_pipeline", imported)
        self.assertNotIn("highlight", imported)

    def test_japanese_requests_merge_char_words_into_phrases(self):
        chars = list("毎日のように会いましょう")

        def transcriber(_path, **kwargs):
            kwargs["progress_callback"](1.0, "done")
            words = [{"start": 0.1 * i, "end": 0.1 * i + 0.08, "word": c, "probability": 0.9} for i, c in enumerate(chars)]
            return [{"start": 0.0, "end": 1.4, "text": "".join(chars), "words": words}]

        self.merge_patch.stop()
        self.addCleanup(self.merge_patch.start)
        with patch("spectra_caption_worker.generate_subtitles", side_effect=fake_captions):
            ja = run_worker(request(str(self.media)), range_extractor=FakeExtractor(), transcriber=transcriber)
            en = run_worker({**request(str(self.media)), "language": "en"}, range_extractor=FakeExtractor(), transcriber=transcriber)
        self.assertLess(len(ja["rawWords"]), len(chars))
        self.assertEqual("".join(w["text"] for w in ja["rawWords"]), "".join(chars))
        self.assertEqual(len(en["rawWords"]), len(chars))

    def test_auto_language_uses_whisper_language_detection(self):
        captured = {}

        def transcriber(_path: str, **kwargs):
            captured["language"] = kwargs["language"]
            return fake_transcriber(_path, **kwargs)

        with patch("spectra_caption_worker.generate_subtitles", side_effect=fake_captions):
            run_worker({**request(str(self.media)), "language": "auto"}, range_extractor=FakeExtractor(), transcriber=transcriber)
        self.assertIsNone(captured["language"])

    def test_acceleration_mode_and_runtime_diagnostics_reach_result_and_progress(self):
        captured = {}
        progress = io.StringIO()

        def transcriber(_path: str, **kwargs):
            captured["acceleration_mode"] = kwargs["acceleration_mode"]
            kwargs["diagnostics_callback"]({
                "device": "cuda",
                "computeType": "float16",
                "model": "large-v3",
                "cudaAvailable": True,
                "fallbackReason": "none",
            })
            return fake_transcriber(_path, **kwargs)

        with patch("spectra_caption_worker.generate_subtitles", side_effect=fake_captions):
            result = run_worker(
                request(str(self.media)),
                progress_stream=progress,
                range_extractor=FakeExtractor(),
                transcriber=transcriber,
                acceleration_mode="gpu",
            )
        self.assertEqual(captured["acceleration_mode"], "gpu")
        messages = {item["code"]: item["message"] for item in result["diagnostics"]}
        self.assertEqual(messages["WHISPER_DEVICE"], "device=cuda")
        self.assertEqual(messages["WHISPER_COMPUTE_TYPE"], "computeType=float16")
        self.assertEqual(messages["WHISPER_CUDA_AVAILABILITY"], "cudaAvailable=true")
        events = [json.loads(line) for line in progress.getvalue().splitlines()]
        runtime_events = [event for event in events if event.get("diagnostics")]
        self.assertTrue(runtime_events)
        self.assertEqual(runtime_events[0]["diagnostics"][0]["message"], "device=cuda")
        self.assertNotIn(str(self.media), json.dumps(result, ensure_ascii=False))

    def test_gpu_failure_is_not_silent_and_keeps_path_free_diagnostics(self):
        progress = io.StringIO()

        def failing_transcriber(_path: str, **_kwargs):
            raise WhisperAccelerationError(
                "GPU unavailable",
                code="GPU_UNAVAILABLE",
                diagnostics={
                    "device": "unavailable",
                    "computeType": "float16",
                    "model": "large-v3",
                    "cudaAvailable": False,
                    "fallbackReason": "cuda_unavailable",
                },
            )

        with self.assertRaises(WorkerFailure) as context:
            run_worker(
                request(str(self.media)),
                progress_stream=progress,
                range_extractor=FakeExtractor(),
                transcriber=failing_transcriber,
                acceleration_mode="gpu",
            )
        self.assertEqual(context.exception.code, "GPU_UNAVAILABLE")
        events = [json.loads(line) for line in progress.getvalue().splitlines()]
        failure = [event for event in events if event.get("state") == "failed"][-1]
        self.assertEqual(failure["errorCode"], "GPU_UNAVAILABLE")
        self.assertEqual(failure["diagnostics"][0]["message"], "device=unavailable")
        self.assertNotIn(str(self.media), json.dumps(events, ensure_ascii=False))

    def test_cli_failure_progress_keeps_request_job_id(self):
        missing = request(str(self.root / "missing.mp4"))
        request_path = self.root / "request.json"
        progress_path = self.root / "progress.jsonl"
        request_path.write_text(json.dumps(missing), encoding="utf-8")
        from spectra_caption_worker import main

        self.assertEqual(
            main([
                "--request", str(request_path),
                "--output", str(self.root / "result.json"),
                "--progress", str(progress_path),
            ]),
            1,
        )
        events = [json.loads(line) for line in progress_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["jobId"], "worker-job-1")
        self.assertEqual(events[-1]["errorCode"], "WORKER_FAILED")


if __name__ == "__main__":
    unittest.main()
