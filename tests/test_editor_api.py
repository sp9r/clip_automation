import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from editor_api import create_app
from timeline_project import build_transcript_words, migrate_v1_project


class EditorApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        runs = Path(self.temp.name)
        run = runs / "fixture_run"
        run.mkdir()
        words = build_transcript_words([{"words": [
            {"word": "A", "start": 0, "end": 1}, {"word": "B", "start": 1, "end": 2},
            {"word": "C", "start": 2, "end": 3}, {"word": "D", "start": 3, "end": 4},
        ]}])
        project = migrate_v1_project({"schema_version": 1, "captions": [{"text": "AB"}, {"text": "CD"}]}, words)
        project["output"] = {"width": 1920, "height": 1080, "fps": 30}
        source = run / "source.mp4"
        source.write_bytes(b"unrendered-source")
        self.source = source
        project["source_path"] = str(source)
        (run / "transcript_words.json").write_text(json.dumps(words), encoding="utf-8")
        (run / "caption_project.v2.json").write_text(json.dumps(project), encoding="utf-8")
        self.client = TestClient(create_app(runs))

    def tearDown(self):
        self.temp.cleanup()

    def test_lists_only_timeline_readiness_and_loads_project(self):
        self.assertEqual(self.client.get("/api/runs").json()[0]["run_id"], "fixture_run")
        response = self.client.get("/api/projects/fixture_run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["word_count"], 4)

    def test_timeline_returns_only_visible_range(self):
        response = self.client.get("/api/projects/fixture_run/timeline?start=0&end=2.1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["captions"]), 2)

    def test_command_and_revision_conflict(self):
        command = {"command_id": "one", "type": "UpdateCaptionText", "base_revision": 0, "caption_id": "cap_000001", "display_text": "AX"}
        self.assertEqual(self.client.post("/api/projects/fixture_run/commands", json=command).status_code, 200)
        self.assertEqual(self.client.post("/api/projects/fixture_run/commands", json=command).status_code, 409)

    def test_media_endpoint_blocks_arbitrary_file(self):
        self.assertEqual(self.client.get("/api/runs/fixture_run/media/transcript_words.json").status_code, 403)
        self.assertEqual(self.client.get("/api/projects/../project").status_code, 404)

    def test_undo_and_redo(self):
        command = {"command_id": "one", "type": "UpdateCaptionText", "base_revision": 0, "caption_id": "cap_000001", "display_text": "AX"}
        self.client.post("/api/projects/fixture_run/commands", json=command)
        self.assertEqual(self.client.post("/api/projects/fixture_run/undo").json()["captions"][0]["display_text"], "AB")
        self.assertEqual(self.client.post("/api/projects/fixture_run/redo").json()["captions"][0]["display_text"], "AX")

    def test_preview_rejects_long_range_and_returns_source_without_encoding(self):
        self.assertEqual(self.client.post("/api/projects/fixture_run/render-preview", json={"start": 0, "end": 31}).status_code, 422)
        with patch("editor_api.subprocess.run", side_effect=AssertionError("Preview must not encode")):
            response = self.client.post("/api/projects/fixture_run/render-preview", json={"start": 0, "end": 3})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["rendered"])
        self.assertTrue(response.json()["source_media_url"].endswith("/source.mp4"))

    def test_project_prefers_unrendered_source_media(self):
        response = self.client.get("/api/projects/fixture_run")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["source_media_url"].endswith("/source.mp4"))
        self.assertEqual(body["media_url"], body["source_media_url"])
        self.assertEqual(self.client.get("/api/runs/fixture_run/media/source.mp4").content, b"unrendered-source")

    def test_preview_named_file_is_not_treated_as_raw_source(self):
        project_path = Path(self.temp.name) / "fixture_run" / "caption_project.v2.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["source_path"] = str(Path(self.temp.name) / "fixture_run" / "preview_proxy.mp4")
        project_path.write_text(json.dumps(project), encoding="utf-8")
        (project_path.parent / "preview_proxy.mp4").write_bytes(b"rendered-proxy")
        self.source.unlink()

        body = self.client.get("/api/projects/fixture_run").json()
        self.assertIsNone(body["source_media_url"])
        self.assertEqual(self.client.post("/api/projects/fixture_run/render-preview", json={"start": 0, "end": 1}).status_code, 409)

    def test_export_uses_the_same_raw_source_as_preview(self):
        rendered = Path(self.temp.name) / "fixture_run" / "final_jumpcut_video.mp4"
        rendered.write_bytes(b"old-rendered-video")
        commands = []

        def fake_ffmpeg(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"exported-video")
            return type("Completed", (), {"returncode": 0, "stderr": ""})()

        with patch("editor_api.subprocess.run", side_effect=fake_ffmpeg):
            response = self.client.post("/api/projects/fixture_run/exports", json={"revision": 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(commands[0][commands[0].index("-i") + 1], str(self.source))
        self.assertNotIn(str(rendered), commands[0])

    def test_prepares_existing_run_and_reports_state(self):
        run = Path(self.temp.name) / "legacy_copy"
        run.mkdir()
        segments = [{"words": [{"word": "A", "start": 0, "end": 1}, {"word": "B", "start": 1, "end": 2}]}]
        v1 = {"schema_version": 1, "captions": [{"text": "AB"}]}
        (run / "clean_transcript.json").write_text(json.dumps(segments), encoding="utf-8")
        (run / "caption_project.json").write_text(json.dumps(v1), encoding="utf-8")
        response = self.client.post("/api/runs/legacy_copy/prepare-timeline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "ready")
        states = {item["run_id"]: item["state"] for item in self.client.get("/api/runs").json()}
        self.assertEqual(states["legacy_copy"], "ready")
    def test_export_video_conflict_and_missing_media(self):
        # Revision不一致エラー (409)
        response = self.client.post("/api/projects/fixture_run/exports", json={"revision": 999})
        self.assertEqual(response.status_code, 409)

        # 動画が見つからないエラー (409)
        self.source.unlink()
        response = self.client.post("/api/projects/fixture_run/exports", json={"revision": 0})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
