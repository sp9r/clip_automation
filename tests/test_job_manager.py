import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_control import PipelineCancelled
from job_manager import JobManager
from subtitle_project import create_caption_project, save_caption_project


class JobManagerTests(unittest.TestCase):
    @staticmethod
    def _settings():
        return {
            "output_width": 1920,
            "output_height": 1080,
            "output_fps": 60,
            "max_chars": 32,
            "use_llm_captions": True,
            "caption_model": "qwen2.5:14b",
            "default_template_id": "standard",
            "video_encoder": "auto",
        }

    def test_adopt_run_resumes_from_render(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runs_dir = Path(temp_dir) / "runs"
            run_dir = runs_dir / "existing_run"
            run_dir.mkdir(parents=True)
            source = run_dir / "source.mp4"
            source.write_bytes(b"source-placeholder")
            transcript = [{"words": [{"start": 0, "end": 1, "word": "テスト"}]}]
            (run_dir / "transcript.json").write_text(
                json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
            )
            (run_dir / "edl.json").write_text(
                json.dumps([{"start": 0, "end": 1}]), encoding="utf-8"
            )
            (run_dir / "clean_transcript.json").write_text(
                json.dumps(transcript, ensure_ascii=False), encoding="utf-8"
            )
            (run_dir / "captions.srt").write_text("subtitle", encoding="utf-8")
            (run_dir / "captions.ass").write_text("subtitle", encoding="utf-8")
            project = create_caption_project(
                captions=[{"start": 0, "end": 1, "text": "テスト"}],
                source_path=str(source),
                edl_path=str(run_dir / "edl.json"),
                run_dir=str(run_dir),
            )
            save_caption_project(project, run_dir / "caption_project.json")

            manager = JobManager(runs_dir)
            manifest = manager.adopt_run(run_dir)

            self.assertEqual(manifest["status"], "canceled")
            self.assertEqual(manifest["current_stage"], "render")
            self.assertEqual(manifest["overall_progress"], 50.0)
            self.assertTrue(manager.get("existing_run")["can_resume"])
            self.assertEqual(manifest["stages"]["captions"]["status"], "completed")

    def test_cancel_event_exception(self):
        event = threading.Event()
        event.set()
        from job_control import raise_if_cancelled

        with self.assertRaises(PipelineCancelled):
            raise_if_cancelled(event)

    def test_repeated_start_request_reuses_pending_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            first = manager.create_job(
                "https://example.com/video",
                is_local=False,
                settings=self._settings(),
                auto_start=False,
                pipeline_version="legacy",
            )
            second = manager.create_job(
                "https://example.com/video",
                is_local=False,
                settings=self._settings(),
                auto_start=False,
                pipeline_version="legacy",
            )

            self.assertEqual(first["job_id"], second["job_id"])
            self.assertFalse(first["request_reused"])
            self.assertTrue(second["request_reused"])
            self.assertEqual(len(list(manager.runs_dir.iterdir())), 1)

    def test_eta_uses_progress_gained_in_current_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            manifest = manager.create_job(
                "https://example.com/video",
                is_local=False,
                settings=self._settings(),
                auto_start=False,
                pipeline_version="legacy",
            )
            job_id = manifest["job_id"]
            stored = manager._load(job_id)
            for stage_id in ("acquire", "transcribe", "jumpcut", "captions"):
                stored["stages"][stage_id]["progress"] = 1.0
                stored["stages"][stage_id]["status"] = "completed"
            stored["started_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=10)
            ).isoformat()
            stored["attempt_started_progress"] = 50.0
            manager._save(stored)

            manager._update_progress(job_id, "render", 0.5, "ETAテスト")
            updated = manager._load(job_id)

            self.assertEqual(updated["overall_progress"], 72.5)
            self.assertGreater(updated["eta_seconds"], 10)
            self.assertLess(updated["eta_seconds"], 15)

    def test_request_rerender_invalidates_render_without_deleting_caption_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            created = manager.create_job(
                "https://example.com/video",
                is_local=False,
                settings=self._settings(),
                auto_start=False,
                pipeline_version="legacy",
            )
            job_id = created["job_id"]
            run_dir = Path(created["run_dir"])
            caption_project = run_dir / "caption_project.json"
            final_video = run_dir / "final_jumpcut_video.mp4"
            caption_project.write_text('{"captions": []}', encoding="utf-8")
            final_video.write_bytes(b"old-render")

            updated = manager.request_rerender(job_id, auto_start=False)

            self.assertTrue(caption_project.is_file())
            self.assertFalse(final_video.exists())
            self.assertEqual(updated["status"], "pending")
            self.assertEqual(updated["current_stage"], "render")
            self.assertEqual(updated["stages"]["render"]["status"], "pending")
            self.assertEqual(updated["stages"]["highlight"]["status"], "pending")
            self.assertTrue(updated["force_render_pending"])

    def test_setting_change_invalidates_only_affected_stage_and_downstream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            created = manager.create_job(
                "https://example.com/video", is_local=False,
                settings=self._settings(), auto_start=False,
                pipeline_version="legacy",
            )
            stored = manager._load(created["job_id"])
            for stage in stored["stages"].values():
                stage.update({"status": "completed", "progress": 1.0})
            manager._save(stored)

            updated = manager.update_settings(created["job_id"], {"output_fps": 30})

            self.assertEqual(updated["stages"]["captions"]["status"], "completed")
            self.assertEqual(updated["stages"]["render"]["status"], "pending")
            self.assertEqual(updated["stages"]["highlight"]["status"], "pending")
            self.assertEqual(updated["current_stage"], "render")

    def test_schema_v1_manifest_is_migrated_in_memory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            created = manager.create_job(
                "https://example.com/video", is_local=False,
                settings=self._settings(), auto_start=False,
                pipeline_version="legacy",
            )
            path = manager._manifest_path(created["job_id"])
            data = json.loads(path.read_text(encoding="utf-8"))
            data["schema_version"] = 1
            for stage in data["stages"].values():
                stage.pop("input_fingerprint", None)
                stage.pop("artifact_fingerprint", None)
            path.write_text(json.dumps(data), encoding="utf-8")

            migrated = manager._load(created["job_id"])

            self.assertEqual(migrated["schema_version"], 3)
            self.assertIn("input_fingerprint", migrated["stages"]["render"])


if __name__ == "__main__":
    unittest.main()
