import unittest
from types import SimpleNamespace
from unittest.mock import patch

from transcribe import (
    WhisperAccelerationError,
    load_whisper_model,
    transcribe_video,
)


class FakeModel:
    def transcribe(self, _path, **_kwargs):
        word = SimpleNamespace(start=0.1, end=0.4, word="テスト", probability=0.9)
        segment = SimpleNamespace(start=0.0, end=0.5, text="テスト", words=[word])
        info = SimpleNamespace(language="ja", language_probability=1.0, duration=0.5)
        return iter([segment]), info


class AccelerationTests(unittest.TestCase):
    def test_gpu_available_loads_cuda_float16(self):
        calls = []

        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeModel()

        model, diagnostics = load_whisper_model(
            "large-v3", acceleration_mode="gpu", model_factory=factory, cuda_device_count=lambda: 1
        )
        self.assertIsInstance(model, FakeModel)
        self.assertEqual(calls[0][1], {"device": "cuda", "compute_type": "float16"})
        self.assertEqual(diagnostics["device"], "cuda")
        self.assertEqual(diagnostics["computeType"], "float16")
        self.assertTrue(diagnostics["cudaAvailable"])
        self.assertEqual(diagnostics["fallbackReason"], "none")

    def test_auto_falls_back_to_cpu_when_cuda_is_unavailable(self):
        calls = []

        def factory(*args, **kwargs):
            calls.append(kwargs)
            return FakeModel()

        _, diagnostics = load_whisper_model(
            acceleration_mode="auto", model_factory=factory, cuda_device_count=lambda: 0
        )
        self.assertEqual(calls, [{"device": "cpu", "compute_type": "int8"}])
        self.assertEqual(diagnostics["device"], "cpu")
        self.assertEqual(diagnostics["computeType"], "int8")
        self.assertFalse(diagnostics["cudaAvailable"])
        self.assertEqual(diagnostics["fallbackReason"], "cuda_unavailable")

    def test_explicit_gpu_fails_without_cuda_and_never_uses_cpu(self):
        calls = []

        def factory(*args, **kwargs):
            calls.append(kwargs)
            return FakeModel()

        with self.assertRaises(WhisperAccelerationError) as context:
            load_whisper_model(
                acceleration_mode="gpu", model_factory=factory, cuda_device_count=lambda: 0
            )
        self.assertEqual(context.exception.code, "GPU_UNAVAILABLE")
        self.assertEqual(context.exception.diagnostics["cudaAvailable"], False)
        self.assertEqual(context.exception.diagnostics["fallbackReason"], "cuda_unavailable")
        self.assertEqual(calls, [])

    def test_explicit_gpu_fails_when_cuda_model_load_fails(self):
        def failing_factory(*_args, **_kwargs):
            raise RuntimeError("CUDA load failed")

        with self.assertRaises(WhisperAccelerationError) as context:
            load_whisper_model(
                acceleration_mode="gpu", model_factory=failing_factory, cuda_device_count=lambda: 1
            )
        self.assertEqual(context.exception.code, "GPU_MODEL_LOAD_FAILED")

    def test_auto_falls_back_when_cuda_model_load_fails(self):
        calls = []

        def factory(*args, **kwargs):
            calls.append(kwargs)
            if kwargs["device"] == "cuda":
                raise RuntimeError("CUDA load failed")
            return FakeModel()

        _, diagnostics = load_whisper_model(
            acceleration_mode="auto", model_factory=factory, cuda_device_count=lambda: 1
        )
        self.assertEqual([call["device"] for call in calls], ["cuda", "cpu"])
        self.assertEqual(diagnostics["device"], "cpu")
        self.assertTrue(diagnostics["cudaAvailable"])
        self.assertEqual(diagnostics["fallbackReason"], "cuda_model_load_failed")

    def test_cpu_mode_is_explicit_even_when_cuda_exists(self):
        calls = []

        def factory(*args, **kwargs):
            calls.append(kwargs)
            return FakeModel()

        _, diagnostics = load_whisper_model(
            acceleration_mode="cpu", model_factory=factory, cuda_device_count=lambda: 1
        )
        self.assertEqual(calls, [{"device": "cpu", "compute_type": "int8"}])
        self.assertEqual(diagnostics["device"], "cpu")
        self.assertEqual(diagnostics["fallbackReason"], "explicit_cpu")

    def test_transcribe_reports_runtime_diagnostics_without_path(self):
        observed = []
        with patch("transcribe.load_whisper_model", return_value=(FakeModel(), {
            "device": "cuda",
            "computeType": "float16",
            "model": "large-v3",
            "cudaAvailable": True,
            "fallbackReason": "none",
        })):
            result = transcribe_video(
                "range.wav",
                diagnostics_callback=observed.append,
                progress_callback=lambda *_args: None,
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(observed[0]["device"], "cuda")
        self.assertNotIn("/", str(observed[0]))
        self.assertNotIn("\\", str(observed[0]))


if __name__ == "__main__":
    unittest.main()
