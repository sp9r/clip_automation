import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diagnostics import normalize_local_path, run_preflight
from job_manager import JobManager


class PreflightTests(unittest.TestCase):
    def test_quoted_local_path_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "sample video.mp4"
            source.write_bytes(b"video")
            self.assertEqual(normalize_local_path(f'"{source}"'), source.resolve())

    def test_ollama_unavailable_and_model_missing_are_distinct(self):
        with mock.patch("diagnostics.check_ollama", return_value={"available": False, "models": [], "error_code": "OLLAMA_UNAVAILABLE"}):
            report = run_preflight("https://example.com", is_local=False, require_ollama=True, required_model="gemma3:latest")
            self.assertEqual(report["errors"][0]["error_code"], "OLLAMA_UNAVAILABLE")
        with mock.patch("diagnostics.check_ollama", return_value={"available": True, "models": ["qwen2.5:14b"], "error_code": None}):
            report = run_preflight("https://example.com", is_local=False, require_ollama=True, required_model="gemma3:latest")
            self.assertEqual(report["errors"][0]["error_code"], "OLLAMA_MODEL_MISSING")

    def test_stale_running_manifest_is_recovered_on_manager_startup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs_dir = Path(temp_dir) / "runs"
            manager = JobManager(runs_dir)
            created = manager.create_job("https://example.com", is_local=False, settings={
                "output_width": 640, "output_height": 360, "output_fps": 30,
                "max_chars": 32, "use_llm_captions": False, "caption_model": "gemma3:latest",
                "default_template_id": "standard", "video_encoder": "libx264",
            }, auto_start=False)
            path = manager._manifest_path(created["job_id"])
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = "running"
            data["current_stage"] = "render"
            path.write_text(json.dumps(data), encoding="utf-8")

            restarted = JobManager(runs_dir)
            recovered = restarted.get(created["job_id"])
            self.assertEqual(recovered["status"], "interrupted")
            self.assertTrue(recovered["can_resume"])


if __name__ == "__main__":
    unittest.main()
