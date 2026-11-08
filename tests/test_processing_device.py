import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from caption import generate_subtitles
from diagnostics import get_acceleration_status
from processing_device import (
    DEFAULT_PROCESSING_DEVICE,
    load_processing_device,
    resolve_processing_policy,
    save_processing_device,
    validate_processing_device,
)


class ProcessingDeviceTests(unittest.TestCase):
    def test_default_and_user_data_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "userData" / "processing_preferences.json"
            self.assertEqual(load_processing_device(path), DEFAULT_PROCESSING_DEVICE)
            self.assertEqual(save_processing_device("gpu", path), "gpu")
            self.assertEqual(load_processing_device(path), "gpu")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data, {"schema_version": 1, "processing_device": "gpu"})

    def test_malformed_user_data_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "processing_preferences.json"
            path.write_text('{"processing_device":"h264_nvenc"}', encoding="utf-8")
            self.assertEqual(load_processing_device(path), "auto")

    def test_policy_maps_user_choice_without_machine_backend_persistence(self):
        self.assertEqual(resolve_processing_policy("auto")["render_encoder"], "auto")
        self.assertEqual(resolve_processing_policy("gpu")["whisper_mode"], "gpu")
        self.assertEqual(resolve_processing_policy("gpu")["render_encoder"], "h264_nvenc")
        self.assertEqual(resolve_processing_policy("cpu")["decode_mode"], "software")
        self.assertNotIn("h264_nvenc", json.dumps({"processing_device": "gpu"}))

    def test_invalid_choice_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_processing_device("nvenc")

    def test_status_exposes_gpu_whisper_caption_ai_and_render(self):
        with patch("diagnostics._cuda_device_count", return_value=1), \
             patch("diagnostics.nvenc_diagnostic", return_value={"available": True, "reason": "none"}), \
             patch("diagnostics.get_ollama_runtime_diagnostic", return_value={"status": "mixed"}):
            status = get_acceleration_status("gpu", "qwen2.5:14b")
        self.assertTrue(status["gpu_available"])
        self.assertEqual(status["whisper"], "CUDA")
        self.assertEqual(status["caption_ai"], "mixed")
        self.assertEqual(status["render"], "NVENC")
        self.assertTrue(status["errors"])

    def test_gpu_policy_reports_unavailable_without_fallback_claim(self):
        with patch("diagnostics._cuda_device_count", return_value=0), \
             patch("diagnostics.nvenc_diagnostic", return_value={"available": False, "reason": "cuda_runtime_unavailable"}), \
             patch("diagnostics.get_ollama_runtime_diagnostic", return_value={"status": "unavailable"}):
            status = get_acceleration_status("gpu", "qwen2.5:14b")
        self.assertFalse(status["gpu_available"])
        self.assertEqual(status["whisper"], "CPU")
        self.assertEqual(status["render"], "CPU")
        self.assertTrue(status["errors"])

    def test_gpu_caption_policy_rejects_cpu_ollama_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps([{"words": [{"word": "テスト", "start": 0.0, "end": 0.5}]}], ensure_ascii=False),
                encoding="utf-8",
            )
            with ExitStack() as stack:
                stack.enter_context(patch("caption._llm_subtitles", return_value=([{"start": 0.0, "end": 0.5, "text": "テスト"}], "llm")))
                stack.enter_context(patch("caption.get_ollama_runtime_diagnostic", return_value={"status": "cpu"}))
                with self.assertRaisesRegex(RuntimeError, "GPU優先"):
                    generate_subtitles(
                        str(transcript), str(root / "captions.srt"),
                        processing_device="gpu", collect_runtime=True,
                    )


if __name__ == "__main__":
    unittest.main()
