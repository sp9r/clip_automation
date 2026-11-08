import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from job_manager import JobManager
from job_control import PipelineCancelled
from publishing.credentials import (
    CredentialPaths,
    ensure_required_scopes,
    load_youtube_credentials,
    refresh_if_needed,
    required_youtube_scopes,
)
from publishing.errors import InsufficientOAuthScopeError, PublishingError, UploadError
from publishing.manifest import load_manifest
from publishing.models import validate_metadata
from publishing.pipeline import prepare_publish_jobs, publish_prepared_jobs, refresh_processing_status
from publishing.thumbnail import validate_thumbnail
from publishing.youtube import Publisher, YouTubePublisher
from render_manifest import new_render_manifest, upsert_clip, write_manifest as write_render_manifest


FFMPEG = shutil.which("ffmpeg")


class FakePublisher(Publisher):
    platform = "youtube"

    def __init__(self, *, failures=None, thumbnail_failure=False, statuses=None, secret_values=()):
        self.failures = set(failures or ())
        self.thumbnail_failure = thumbnail_failure
        self.statuses = dict(statuses or {})
        self.remote_calls = []
        self.thumbnail_calls = []
        self.status_calls = []
        self.secret_values = tuple(secret_values)

    def credentials_available(self):
        return True

    def publish_video(self, video_path, metadata, **_kwargs):
        clip_id = metadata["title"]
        self.remote_calls.append((clip_id, str(video_path)))
        if clip_id in self.failures:
            raise PublishingError("fake upload failed", clip_id=clip_id, retryable=True)
        video_id = f"video-{len(self.remote_calls)}"
        return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}

    def set_thumbnail(self, video_id, thumbnail_path):
        self.thumbnail_calls.append((video_id, str(thumbnail_path)))
        if self.thumbnail_failure:
            raise PublishingError("fake thumbnail failed", retryable=False)
        return {"ok": True}

    def get_status(self, video_id):
        self.status_calls.append(video_id)
        value = self.statuses.get(video_id, "processing")
        return {"processing_status": value, "channel_id": "UC-test", "channel_title": "Test Channel"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_run(root: Path, states=("completed", "failed", "completed")) -> tuple[Path, list[Path]]:
    run_dir = root / "run 日本語 (space)"
    planning_dir = run_dir / "planning"
    planning_dir.mkdir(parents=True)
    planning_manifest = planning_dir / "job_manifest.json"
    planning_manifest.write_text("planning fixture", encoding="utf-8")
    render_path = planning_dir / "render_manifest.v1.json"
    render = new_render_manifest(
        planning_dir=planning_dir,
        planning_manifest_path=planning_manifest,
        renderer={"source": "fixture"},
    )
    outputs = []
    for index, state in enumerate(states, start=1):
        clip_id = f"clip_{index:06d}"
        clip_dir = planning_dir / "clips" / clip_id
        project_path = clip_dir / "edit_project.json"
        output_path = clip_dir / "automation" / "rendered.mp4"
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text("{}", encoding="utf-8")
        if state == "completed":
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(f"rendered-{clip_id}".encode())
            outputs.append(output_path)
        upsert_clip(
            render,
            {
                "clip_id": clip_id,
                "state": state,
                "edit_project": {"path": f"clips/{clip_id}/edit_project.json", "sha256": _sha(project_path)},
                "output": {
                    "path": f"clips/{clip_id}/automation/rendered.mp4",
                    "sha256": _sha(output_path) if output_path.is_file() else None,
                    "size": output_path.stat().st_size if output_path.is_file() else 0,
                },
                "logs": {"stdout": f"clips/{clip_id}/automation/render.stdout.log", "stderr": f"clips/{clip_id}/automation/render.stderr.log"},
                "exit_code": 0 if state == "completed" else 1,
                "stdout_json": None,
                "error": None if state == "completed" else "fixture failure",
            },
        )
    write_render_manifest(render_path, render)
    return run_dir, outputs


class Phase7ManifestTests(unittest.TestCase):
    def test_prepare_is_deterministic_and_completed_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir, _outputs = _make_run(Path(temp_dir))
            first = prepare_publish_jobs(run_dir)
            first_bytes = Path(first["manifest_path"]).read_bytes()
            second = prepare_publish_jobs(run_dir)
            self.assertEqual(first_bytes, Path(second["manifest_path"]).read_bytes())
            self.assertEqual([item["clip_id"] for item in first["clips"]], ["clip_000001", "clip_000003"])
            self.assertEqual(first["summary"]["prepared"], 2)
            self.assertEqual(first["clips"][0]["metadata"]["privacy_status"], "private")

    def test_hash_mismatch_blocks_prepare(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir, outputs = _make_run(Path(temp_dir), states=("completed",))
            outputs[0].write_bytes(b"tampered")
            with self.assertRaises(Exception):
                prepare_publish_jobs(run_dir)

    def test_metadata_limits_and_default_privacy(self):
        self.assertEqual(validate_metadata({"title": "ok"}).privacy_status, "private")
        with self.assertRaises(Exception):
            validate_metadata({"title": ""})
        with self.assertRaises(Exception):
            validate_metadata({"title": "x" * 101})
        with self.assertRaises(Exception):
            validate_metadata({"title": "ok", "privacy_status": "public-ish"})
        with self.assertRaises(Exception):
            validate_metadata({"title": "ok", "description": "あ" * 5001})

    def test_paths_with_japanese_and_spaces_are_portable(self):
        with tempfile.TemporaryDirectory(prefix="phase7 日本語 ") as temp_dir:
            run_dir, _outputs = _make_run(Path(temp_dir))
            result = prepare_publish_jobs(run_dir)
            manifest = load_manifest(result["manifest_path"])
            self.assertTrue(manifest["source_render_manifest"]["path"].startswith("../"))
            self.assertNotIn("\\", json.dumps(manifest, ensure_ascii=False))

    def test_reprepare_preserves_uploaded_remote_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir, _outputs = _make_run(Path(temp_dir), states=("completed",))
            first = prepare_publish_jobs(run_dir, metadata_by_clip={"default": {"title": "first"}})
            publisher = FakePublisher()
            publish_prepared_jobs(first["manifest_path"], publisher=publisher)
            second = prepare_publish_jobs(run_dir, metadata_by_clip={"default": {"title": "second"}})
            clip = second["clips"][0]
            self.assertEqual(clip["remote"]["video_id"], "video-1")
            self.assertTrue(clip["diagnostics"]["metadata_changed_after_upload"])
            publish_prepared_jobs(second["manifest_path"], publisher=publisher)
            self.assertEqual(len(publisher.remote_calls), 1)

    def test_reprepare_changed_render_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir, outputs = _make_run(Path(temp_dir), states=("completed",))
            first = prepare_publish_jobs(run_dir)
            publisher = FakePublisher()
            publish_prepared_jobs(first["manifest_path"], publisher=publisher)
            outputs[0].write_bytes(b"new rendered video")
            render_path = run_dir / "planning" / "render_manifest.v1.json"
            render = json.loads(render_path.read_text(encoding="utf-8"))
            render["clips"][0]["output"]["sha256"] = _sha(outputs[0])
            render["clips"][0]["output"]["size"] = outputs[0].stat().st_size
            render_path.write_text(json.dumps(render, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            changed = prepare_publish_jobs(run_dir)
            clip = changed["clips"][0]
            self.assertEqual(clip["state"], "needs_republish_confirmation")
            self.assertIsNone(clip["remote"]["video_id"])
            self.assertEqual(clip["previous_remote"]["video_id"], "video-1")
            changed_again = prepare_publish_jobs(run_dir)
            self.assertEqual(changed_again["clips"][0]["state"], "needs_republish_confirmation")
            self.assertEqual(changed_again["clips"][0]["previous_remote"]["video_id"], "video-1")
            publish_prepared_jobs(changed_again["manifest_path"], publisher=publisher)
            self.assertEqual(len(publisher.remote_calls), 1)
            publish_prepared_jobs(changed_again["manifest_path"], publisher=publisher, confirm_changed_render=True)
            self.assertEqual(len(publisher.remote_calls), 2)
            self.assertEqual(load_manifest(changed["manifest_path"])["clips"][0]["remote"]["video_id"], "video-2")

    def test_reprepare_thumbnail_change_preserves_remote_and_retries_thumbnail_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir, _outputs = _make_run(root, states=("completed",))
            first_thumbnail = root / "first.jpg"
            second_thumbnail = root / "second.jpg"
            Image.new("RGB", (32, 32), "red").save(first_thumbnail)
            Image.new("RGB", (32, 32), "blue").save(second_thumbnail)
            first = prepare_publish_jobs(run_dir, thumbnail_by_clip={"clip_000001": first_thumbnail})
            publisher = FakePublisher()
            publish_prepared_jobs(first["manifest_path"], publisher=publisher)
            second = prepare_publish_jobs(run_dir, thumbnail_by_clip={"clip_000001": second_thumbnail})
            self.assertEqual(second["clips"][0]["remote"]["video_id"], "video-1")
            self.assertEqual(second["clips"][0]["thumbnail"]["state"], "prepared")
            publish_prepared_jobs(second["manifest_path"], publisher=publisher)
            self.assertEqual(len(publisher.remote_calls), 1)
            self.assertEqual(len(publisher.thumbnail_calls), 2)


class Phase7SafetyTests(unittest.TestCase):
    def _prepared(self, root: Path, states=("completed",)):
        run_dir, _outputs = _make_run(root, states=states)
        result = prepare_publish_jobs(run_dir, metadata_by_clip={"default": {"title": "clip title"}})
        return Path(result["manifest_path"])

    def test_dry_run_has_zero_remote_calls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._prepared(Path(temp_dir))
            publisher = FakePublisher()
            result = publish_prepared_jobs(manifest_path, publisher=publisher, dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["remote_calls"], 0)
            self.assertEqual(publisher.remote_calls, [])
            self.assertEqual(load_manifest(manifest_path)["clips"][0]["state"], "prepared")

    def test_upload_success_persists_video_id_and_restart_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._prepared(Path(temp_dir))
            publisher = FakePublisher()
            first = publish_prepared_jobs(manifest_path, publisher=publisher)
            with patch("publishing.pipeline.atomic_write_manifest", side_effect=AssertionError("idempotent publish must not rewrite manifest")):
                second = publish_prepared_jobs(manifest_path, publisher=publisher)
            self.assertEqual(first["summary"]["completed"], 1)
            self.assertEqual(second["manifest"]["clips"][0]["remote"]["video_id"], "video-1")
            self.assertEqual(len(publisher.remote_calls), 1)

    def test_upload_failure_is_failed_and_secret_is_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._prepared(Path(temp_dir))
            publisher = FakePublisher(failures={"clip title"}, secret_values=("VERY_SECRET_ACCESS_TOKEN",))
            logs = []

            def failure(*args, **kwargs):
                raise RuntimeError("request failed VERY_SECRET_ACCESS_TOKEN")

            publisher.publish_video = failure
            result = publish_prepared_jobs(manifest_path, publisher=publisher, log_callback=logs.append)
            data = load_manifest(manifest_path)
            serialized = json.dumps(data, ensure_ascii=False)
            self.assertEqual(result["summary"]["failed"], 1)
            self.assertNotIn("VERY_SECRET_ACCESS_TOKEN", serialized)
            self.assertNotIn("VERY_SECRET_ACCESS_TOKEN", "\n".join(logs))

    def test_partial_failure_continues_later_clips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir, _outputs = _make_run(Path(temp_dir), states=("completed", "completed", "completed"))
            manifest = prepare_publish_jobs(
                run_dir,
                metadata_by_clip={
                    "clip_000001": {"title": "A"},
                    "clip_000002": {"title": "B"},
                    "clip_000003": {"title": "C"},
                },
            )
            publisher = FakePublisher(failures={"B"})
            result = publish_prepared_jobs(manifest["manifest_path"], publisher=publisher)
            self.assertEqual(result["summary"]["completed"], 2)
            self.assertEqual(result["summary"]["failed"], 1)
            self.assertEqual([item[0] for item in publisher.remote_calls], ["A", "B", "C"])

    def test_thumbnail_failure_preserves_remote_video_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir, _outputs = _make_run(root, states=("completed",))
            thumbnail = root / "thumbnail 日本語.jpg"
            Image.new("RGB", (1280, 720), (255, 0, 0)).save(thumbnail, format="JPEG")
            manifest = prepare_publish_jobs(run_dir, thumbnail_by_clip={"clip_000001": thumbnail})
            publisher = FakePublisher(thumbnail_failure=True)
            result = publish_prepared_jobs(manifest["manifest_path"], publisher=publisher)
            clip = result["manifest"]["clips"][0]
            self.assertEqual(clip["state"], "uploaded_with_thumbnail_error")
            self.assertEqual(clip["remote"]["video_id"], "video-1")
            self.assertEqual(clip["thumbnail"]["state"], "failed")

    def test_thumbnail_hash_is_rechecked_before_remote_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir, _outputs = _make_run(root, states=("completed",))
            thumbnail = root / "thumbnail.jpg"
            Image.new("RGB", (1280, 720), (255, 0, 0)).save(thumbnail, format="JPEG")
            manifest = prepare_publish_jobs(run_dir, thumbnail_by_clip={"clip_000001": thumbnail})
            managed_thumbnail = Path(manifest["manifest_path"]).parent / "clip_000001" / "thumbnail.jpg"
            Image.new("RGB", (1280, 720), (0, 0, 255)).save(managed_thumbnail, format="JPEG")
            publisher = FakePublisher()
            result = publish_prepared_jobs(manifest["manifest_path"], publisher=publisher)
            clip = result["manifest"]["clips"][0]
            self.assertEqual(publisher.thumbnail_calls, [])
            self.assertEqual(clip["state"], "uploaded_with_thumbnail_error")
            self.assertEqual(clip["remote"]["video_id"], "video-1")

    def test_processing_pending_succeeded_and_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_path = self._prepared(Path(temp_dir) / "pending")
            pending = publish_prepared_jobs(
                pending_path, publisher=FakePublisher(statuses={"video-1": "processing"}),
                wait_processing=True, processing_timeout=0,
            )
            self.assertEqual(pending["manifest"]["clips"][0]["state"], "processing_pending")

            success_path = self._prepared(Path(temp_dir) / "success")
            success = publish_prepared_jobs(
                success_path, publisher=FakePublisher(statuses={"video-1": "succeeded"}),
                wait_processing=True, processing_timeout=1,
            )
            self.assertEqual(success["manifest"]["clips"][0]["state"], "completed")

            failed_path = self._prepared(Path(temp_dir) / "failed")
            failed = publish_prepared_jobs(
                failed_path, publisher=FakePublisher(statuses={"video-1": "failed"}),
                wait_processing=True, processing_timeout=1,
            )
            self.assertEqual(failed["manifest"]["clips"][0]["state"], "failed")
            self.assertIsNotNone(failed["manifest"]["clips"][0]["remote"]["video_id"])

    def test_status_command_path_never_uploads_prepared_clips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = self._prepared(Path(temp_dir))
            publisher = FakePublisher()
            result = refresh_processing_status(manifest_path, publisher=publisher)
            self.assertEqual(publisher.remote_calls, [])
            self.assertEqual(result["manifest"]["clips"][0]["state"], "prepared")

    def test_fake_oauth_refresh_seam(self):
        class Credentials:
            expired = True
            refresh_token = "refresh"
            refreshed = False

            def refresh(self, request):
                self.refreshed = request == "request"
                self.expired = False

        credentials = refresh_if_needed(Credentials(), "request")
        self.assertTrue(credentials.refreshed)

    def test_standard_oauth_scopes_cover_upload_and_processing_status(self):
        from publishing.credentials import YOUTUBE_READONLY_SCOPE, YOUTUBE_UPLOAD_SCOPE
        self.assertEqual(set(required_youtube_scopes()), {YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE})
        class Credentials:
            granted_scopes = {YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE}
        ensure_required_scopes(Credentials(), required_youtube_scopes())

    def test_existing_upload_only_token_fails_before_api_use(self):
        from publishing.credentials import YOUTUBE_UPLOAD_SCOPE
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "token.json"
            token_file.write_text(json.dumps({
                "refresh_token": "REFRESH_SECRET",
                "client_id": "client-id",
                "client_secret": "CLIENT_SECRET",
                "scopes": [YOUTUBE_UPLOAD_SCOPE],
            }), encoding="utf-8")
            paths = CredentialPaths(Path(temp_dir) / "client.json", token_file)
            with self.assertRaises(InsufficientOAuthScopeError) as raised:
                load_youtube_credentials(paths)
            self.assertIn("authをやり直してください", str(raised.exception))
            self.assertNotIn("REFRESH_SECRET", str(raised.exception))
            self.assertTrue(token_file.is_file())

    def test_job_completion_does_not_create_or_execute_publishing(self):
        def runner(_source, **kwargs):
            for stage in ("acquire", "transcribe", "jumpcut", "captions", "planning", "render"):
                kwargs["progress_callback"](stage, 1.0, stage)
            return {"pipeline_version": "v2", "status": "completed", "clips": [], "completed_outputs": [], "failed_outputs": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs", pipeline_runner=runner)
            created = manager.create_job(
                "source.mp4", is_local=True,
                settings={
                    "output_width": 320, "output_height": 180, "output_fps": 30,
                    "max_chars": 32, "use_llm_captions": False,
                    "caption_model": "deterministic", "default_template_id": "standard",
                    "video_encoder": "auto",
                },
            )
            finished = manager.wait(created["job_id"], timeout=5)
            self.assertEqual(finished["status"], "completed")
            self.assertFalse((Path(finished["run_dir"]) / "publishing").exists())


class Phase7ThumbnailTests(unittest.TestCase):
    def test_thumbnail_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "valid.png"
            Image.new("RGB", (10, 10), "blue").save(image)
            info = validate_thumbnail(image)
            self.assertEqual(info["mime_type"], "image/png")
            invalid = root / "invalid.txt"
            invalid.write_text("not image", encoding="utf-8")
            with self.assertRaises(Exception):
                validate_thumbnail(invalid)


class Phase7ResumableUploadTests(unittest.TestCase):
    class _HttpFailure(Exception):
        def __init__(self, status=503):
            super().__init__(f"HTTP {status}")
            self.resp = types.SimpleNamespace(status=status)

    class _Request:
        def __init__(self, sequence, on_call=None):
            self.sequence = list(sequence)
            self.calls = 0
            self.on_call = on_call

        def next_chunk(self):
            self.calls += 1
            if self.on_call:
                self.on_call(self.calls)
            value = self.sequence.pop(0)
            if isinstance(value, BaseException):
                raise value
            return None, value

    class _Videos:
        def __init__(self, request):
            self.request = request

        def insert(self, **_kwargs):
            return self.request

    class _Service:
        def __init__(self, request):
            self._videos = Phase7ResumableUploadTests._Videos(request)

        def videos(self):
            return self._videos

    def _publish(self, sequence, *, max_retries=5, cancel_event=None, on_call=None):
        request = self._Request(sequence, on_call=on_call)
        service = self._Service(request)

        class MediaFileUpload:
            def __init__(self, _path, **kwargs):
                self.kwargs = kwargs

        google_package = types.ModuleType("googleapiclient")
        google_http = types.ModuleType("googleapiclient.http")
        google_http.MediaFileUpload = MediaFileUpload
        google_package.http = google_http
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "large video.mp4"
            video.write_bytes(b"video")
            with patch.dict(sys.modules, {"googleapiclient": google_package, "googleapiclient.http": google_http}):
                publisher = YouTubePublisher(
                    service=service, credentials=object(), max_retries=max_retries,
                    sleep=lambda _seconds: None,
                )
                result = publisher.publish_video(video, {"title": "large", "description": "", "tags": [], "category_id": "22", "privacy_status": "private"}, cancel_event=cancel_event)
        return result, request

    def test_twenty_successful_chunks_then_final_response(self):
        result, request = self._publish([None] * 20 + [{"id": "large-21"}])
        self.assertEqual(result["video_id"], "large-21")
        self.assertEqual(request.calls, 21)

    def test_retry_after_progress_and_counter_reset(self):
        sequence = [None] * 10 + [self._HttpFailure(), None, self._HttpFailure(), {"id": "retry-reset"}]
        result, request = self._publish(sequence, max_retries=1)
        self.assertEqual(result["video_id"], "retry-reset")
        self.assertEqual(request.calls, 14)

    def test_six_consecutive_failures_exceed_max_retries(self):
        with self.assertRaises(UploadError):
            self._publish([self._HttpFailure()] * 6, max_retries=5)

    def test_cancel_at_chunk_boundary_raises_pipeline_cancelled(self):
        cancel_event = threading.Event()
        def after_first(call_count):
            if call_count == 1:
                cancel_event.set()
        with self.assertRaises(PipelineCancelled):
            self._publish([None, {"id": "never"}], cancel_event=cancel_event, on_call=after_first)


class Phase7YouTubeAdapterTests(unittest.TestCase):
    def test_fake_google_service_uses_resumable_insert_thumbnail_and_status(self):
        class UploadRequest:
            def __init__(self):
                self.calls = 0

            def next_chunk(self):
                self.calls += 1
                return None, {"id": "yt-123"}

        class ExecuteRequest:
            def __init__(self, response):
                self.response = response

            def execute(self):
                return self.response

        class Videos:
            def __init__(self):
                self.insert_body = None
                self.insert_media = None

            def insert(self, *, part, body, media_body):
                self.insert_body = body
                self.insert_media = media_body
                return UploadRequest()

            def list(self, **_kwargs):
                return ExecuteRequest({"items": [{"processingDetails": {"processingStatus": "processing"}, "snippet": {"channelId": "UC1", "channelTitle": "Channel"}}]})

        class Thumbnails:
            def __init__(self):
                self.video_id = None

            def set(self, *, videoId, media_body):
                self.video_id = videoId
                return ExecuteRequest({"ok": True})

        class Service:
            def __init__(self):
                self.video = Videos()
                self.thumbnail = Thumbnails()

            def videos(self):
                return self.video

            def thumbnails(self):
                return self.thumbnail

        class MediaFileUpload:
            def __init__(self, path, **kwargs):
                self.path = path
                self.kwargs = kwargs

        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "video.mp4"
            thumb = Path(temp_dir) / "thumb.jpg"
            video.write_bytes(b"video")
            thumb.write_bytes(b"jpeg")
            service = Service()
            google_package = types.ModuleType("googleapiclient")
            google_http = types.ModuleType("googleapiclient.http")
            google_http.MediaFileUpload = MediaFileUpload
            google_package.http = google_http
            with patch.dict(sys.modules, {"googleapiclient": google_package, "googleapiclient.http": google_http}):
                publisher = YouTubePublisher(service=service, credentials=object(), sleep=lambda _seconds: None)
                response = publisher.publish_video(video, {"title": "title", "description": "", "tags": [], "category_id": "22", "privacy_status": "private"})
                publisher.set_thumbnail("yt-123", thumb)
                status = publisher.get_status("yt-123")
            self.assertEqual(response["video_id"], "yt-123")
            self.assertTrue(service.video.insert_media.kwargs["resumable"])
            self.assertEqual(service.video.insert_body["status"]["privacyStatus"], "private")
            self.assertEqual(service.thumbnail.video_id, "yt-123")
            self.assertEqual(status["channel_id"], "UC1")


@unittest.skipUnless(FFMPEG, "ffmpeg unavailable")
class Phase7GeneratedDryRunTests(unittest.TestCase):
    def test_generated_media_prepare_and_dry_run(self):
        with tempfile.TemporaryDirectory(prefix="phase7-generated-") as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run"
            planning = run_dir / "planning"
            planning.mkdir(parents=True)
            planning_manifest = planning / "job_manifest.json"
            planning_manifest.write_text("planning", encoding="utf-8")
            rendered = planning / "clips" / "clip_000001" / "automation" / "rendered.mp4"
            rendered.parent.mkdir(parents=True)
            generated = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10:duration=1", "-c:v", "libx264", str(rendered)],
                capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            render = new_render_manifest(planning_dir=planning, planning_manifest_path=planning_manifest, renderer={"source": "generated"})
            upsert_clip(render, {
                "clip_id": "clip_000001", "state": "completed",
                "edit_project": {"path": "clips/clip_000001/edit_project.json", "sha256": None},
                "output": {"path": "clips/clip_000001/automation/rendered.mp4", "sha256": _sha(rendered), "size": rendered.stat().st_size},
                "logs": {"stdout": "stdout.log", "stderr": "stderr.log"}, "exit_code": 0, "stdout_json": None, "error": None,
            })
            write_render_manifest(planning / "render_manifest.v1.json", render)
            result = prepare_publish_jobs(run_dir, metadata_by_clip={"clip_000001": {"title": "generated"}})
            dry = publish_prepared_jobs(result["manifest_path"], publisher=FakePublisher(), dry_run=True)
            self.assertEqual(dry["remote_calls"], 0)
            self.assertEqual(result["clips"][0]["video"]["sha256"], _sha(rendered))


if __name__ == "__main__":
    unittest.main()
