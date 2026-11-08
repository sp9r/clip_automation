import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from job_manager import JobManager
from job_control import PipelineCancelled
from planning_core import ChunkingConfig, DecisionSettings
from planning_manifest import new_manifest, record_stage, write_manifest as write_planning_manifest
from planning_providers import DeterministicCandidateProvider, DeterministicEvaluationProvider
from pipeline import inspect_run, run_pipeline
from production_pipeline import run_v2_pipeline
from render_pipeline import render_planning_outputs
from video_editor_runner import RenderInvocation, VideoEditorConfig, VideoEditorRunner


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VIDEO_EDITOR_ROOT = (REPOSITORY_ROOT.parent / "video_editor").resolve()
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
REAL_PHASE6_AVAILABLE = bool(
    FFMPEG
    and FFPROBE
    and (VIDEO_EDITOR_ROOT / "scripts" / "video-editor.mjs").is_file()
    and (VIDEO_EDITOR_ROOT / "out" / "cli" / "video-editor.js").is_file()
)


def _write_planning_contract_fixture(run_dir: Path) -> tuple[Path, Path, Path]:
    planning_dir = run_dir / "planning"
    project_path = planning_dir / "clips" / "clip_000001" / "edit_project.json"
    output_path = planning_dir / "clips" / "clip_000001" / "automation" / "rendered.mp4"
    project_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text("{}", encoding="utf-8")
    planning_manifest = new_manifest(input_fingerprint="a" * 64)
    record_stage(
        planning_manifest,
        "contract_generation",
        state="completed",
        artifacts=[
            {
                "clip_id": "clip_000001",
                "path": str(project_path),
                "schema_version": "edit_project.v1",
                "state": "completed",
            }
        ],
        schema_version="edit_project.v1",
    )
    write_planning_manifest(planning_dir / "job_manifest.json", planning_manifest)
    return planning_dir, project_path, output_path


def _fake_runner(*, exit_code: int, payload: bytes | None = None) -> VideoEditorRunner:
    class Process:
        def poll(self):
            return exit_code

        def wait(self, timeout=None):
            return exit_code

    def popen(argv, **kwargs):
        staging_path = Path(argv[argv.index("-o") + 1])
        if payload is not None:
            staging_path.write_bytes(payload)
        kwargs["stdout"].write(b'{"ok":true}')
        kwargs["stderr"].write(b"")
        return Process()

    config = VideoEditorConfig(
        command=(sys.executable, "-c", "pass"),
        source="test",
        require_built_artifact=False,
    )
    return VideoEditorRunner(config, popen_factory=popen)


def _write_deterministic_transcript(path: Path) -> None:
    words = []
    for index, text in enumerate("あいうえ"):
        words.append({"start": float(index), "end": float(index + 1), "word": text, "probability": 0.99})
    path.write_text(json.dumps([{"words": words}], ensure_ascii=False), encoding="utf-8")


def _fake_jumpcut(_transcript: str, edl_path: str, clean_path: str, **_kwargs) -> None:
    Path(edl_path).write_text(json.dumps([{"start": 0.0, "end": 4.0}]), encoding="utf-8")
    Path(clean_path).write_text(
        json.dumps([
            {"start": 0.0, "end": 1.0, "text": "あ", "words": [{"start": 0.0, "end": 1.0, "word": "あ"}]},
            {"start": 1.0, "end": 2.0, "text": "い", "words": [{"start": 1.0, "end": 2.0, "word": "い"}]},
            {"start": 2.0, "end": 3.0, "text": "う", "words": [{"start": 2.0, "end": 3.0, "word": "う"}]},
            {"start": 3.0, "end": 4.0, "text": "え", "words": [{"start": 3.0, "end": 4.0, "word": "え"}]},
        ], ensure_ascii=False),
        encoding="utf-8",
    )


def _fake_subtitles(_input: str, srt_path: str, **_kwargs) -> dict:
    captions = [
        {"id": "caption_000001", "start": 0.0, "end": 1.0, "text": "あ", "template_id": "standard"},
        {"id": "caption_000002", "start": 1.0, "end": 2.0, "text": "い", "template_id": "standard"},
        {"id": "caption_000003", "start": 2.0, "end": 3.0, "text": "う", "template_id": "standard"},
        {"id": "caption_000004", "start": 3.0, "end": 4.0, "text": "え", "template_id": "standard"},
    ]
    Path(srt_path).write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nあ\n\n2\n00:00:01,000 --> 00:00:02,000\nい\n",
        encoding="utf-8",
    )
    return {"method": "deterministic", "captions": captions, "blocks": len(captions)}


@unittest.skipUnless(REAL_PHASE6_AVAILABLE, "video_editor build or ffmpeg/ffprobe unavailable")
class Phase6ProductionE2ETests(unittest.TestCase):
    def test_v2_pipeline_renders_independent_outputs_without_legacy_renderer(self):
        with tempfile.TemporaryDirectory(prefix="phase6-production-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source (日本語).mp4"
            run_dir = root / "run output (space)"
            run_dir.mkdir()
            generated = subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=22",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=22",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", "22", str(source),
                ], check=False, capture_output=True, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8", errors="replace"))

            def fake_transcribe(_source, output_json, **_kwargs):
                _write_deterministic_transcript(Path(output_json))

            provider = DeterministicCandidateProvider(
                selector=lambda _chunk: [
                    {"start": 0.0, "end": 1.0, "reason": "first"},
                    {"start": 2.0, "end": 3.0, "reason": "second"},
                ]
            )
            with patch("production_pipeline.transcribe_video", side_effect=fake_transcribe), \
                 patch("production_pipeline.generate_jumpcut_data", side_effect=_fake_jumpcut), \
                 patch("production_pipeline.generate_subtitles", side_effect=_fake_subtitles), \
                 patch("pipeline.create_clip", side_effect=AssertionError("legacy renderer called")), \
                 patch("highlight.analyze_highlights", side_effect=AssertionError("legacy highlight called")):
                result = run_v2_pipeline(
                    str(source),
                    is_local_file=True,
                    existing_run_dir=run_dir,
                    output_width=320,
                    output_height=180,
                    output_fps=30,
                    candidate_provider=provider,
                    evaluator_provider=DeterministicEvaluationProvider(),
                    planning_options={
                        "chunking": ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
                        "decision_settings": DecisionSettings(max_selected_clips=2, min_duration=0.5),
                    },
                    video_editor_root=VIDEO_EDITOR_ROOT,
                    repo_root=REPOSITORY_ROOT,
                )

            self.assertEqual(result["pipeline_version"], "v2")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["clips"]), 2)
            self.assertEqual(len(result["completed_outputs"]), 2)
            project_hashes = {
                item["clip_id"]: Path(item["edit_project_path"]).read_bytes()
                for item in result["clips"]
            }
            self.assertFalse((run_dir / "final_jumpcut_video.mp4").exists())
            self.assertFalse((run_dir / "preview_proxy.mp4").exists())
            self.assertFalse((run_dir / "highlight.json").exists())
            self.assertFalse((run_dir / "highlights.json").exists())
            self.assertFalse((run_dir / "edit_regions.json").exists())
            self.assertFalse(list((run_dir / "planning").rglob("concat*")))
            inspected = inspect_run(run_dir)
            self.assertEqual(inspected["pipeline_version"], "v2")
            self.assertEqual(inspected["planning_state"], "completed")
            self.assertEqual(inspected["render_summary"]["completed"], 2)
            self.assertEqual(len(inspected["clip_outputs"]), 2)
            for output in result["completed_outputs"]:
                probe = subprocess.run(
                    [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", output],
                    check=True, capture_output=True, text=True, shell=False,
                )
                data = json.loads(probe.stdout)
                video = next(item for item in data["streams"] if item["codec_type"] == "video")
                audio = next(item for item in data["streams"] if item["codec_type"] == "audio")
                self.assertEqual((int(video["width"]), int(video["height"])), (320, 180))
                self.assertGreater(float(data["format"]["duration"]), 0)
                self.assertEqual(audio["codec_type"], "audio")

            with patch("production_pipeline.transcribe_video", side_effect=AssertionError("transcribe rerun")), \
                 patch("production_pipeline.generate_jumpcut_data", side_effect=AssertionError("jumpcut rerun")), \
                 patch("production_pipeline.generate_subtitles", side_effect=AssertionError("captions rerun")), \
                 patch("production_pipeline.render_planning_outputs", side_effect=AssertionError("render rerun")):
                resumed = run_v2_pipeline(
                    str(source), is_local_file=True, existing_run_dir=run_dir, resume=True,
                    output_width=320, output_height=180, output_fps=30,
                    video_editor_root=VIDEO_EDITOR_ROOT, repo_root=REPOSITORY_ROOT,
                )
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(
                project_hashes,
                {item["clip_id"]: Path(item["edit_project_path"]).read_bytes() for item in resumed["clips"]},
            )

    def test_v2_replanning_uses_changed_output_settings_in_contract_and_render(self):
        with tempfile.TemporaryDirectory(prefix="phase61-output-settings-") as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            generated = subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", "6", str(source),
                ], check=False, capture_output=True, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode("utf-8", errors="replace"))

            provider = DeterministicCandidateProvider(
                selector=lambda _chunk: [{"start": 0.0, "end": 1.0, "reason": "output-settings"}]
            )

            def fake_transcribe(_source, output_json, **_kwargs):
                _write_deterministic_transcript(Path(output_json))

            def runner(source_value, **kwargs):
                kwargs.update(
                    {
                        "candidate_provider": provider,
                        "evaluator_provider": DeterministicEvaluationProvider(),
                        "planning_options": {
                            "chunking": ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
                            "decision_settings": DecisionSettings(max_selected_clips=1, min_duration=0.5),
                        },
                        "video_editor_root": VIDEO_EDITOR_ROOT,
                        "repo_root": REPOSITORY_ROOT,
                    }
                )
                return run_pipeline(source_value, **kwargs)

            with patch("production_pipeline.transcribe_video", side_effect=fake_transcribe), \
                 patch("production_pipeline.generate_jumpcut_data", side_effect=_fake_jumpcut), \
                 patch("production_pipeline.generate_subtitles", side_effect=_fake_subtitles), \
                 patch("pipeline.create_clip", side_effect=AssertionError("legacy renderer called")), \
                 patch("highlight.analyze_highlights", side_effect=AssertionError("legacy highlight called")):
                manager = JobManager(root / "runs", pipeline_runner=runner)
                created = manager.create_job(
                    str(source), is_local=True,
                    settings={
                        "output_width": 320, "output_height": 180, "output_fps": 30,
                        "max_chars": 32, "use_llm_captions": False,
                        "caption_model": "deterministic", "default_template_id": "standard",
                        "video_encoder": "auto",
                    },
                )
                initial = manager.wait(created["job_id"], timeout=120)
                self.assertEqual(initial["status"], "completed", initial)
                initial_project = Path(initial["results"]["clips"][0]["edit_project_path"])
                initial_data = json.loads(initial_project.read_text(encoding="utf-8"))
                self.assertEqual(
                    (initial_data["sequence"]["width"], initial_data["sequence"]["height"], initial_data["sequence"]["fps"]),
                    (320, 180, 30),
                )

                updated = manager.update_settings(
                    created["job_id"],
                    {"output_width": 720, "output_height": 1280, "output_fps": 60},
                )
                self.assertEqual(updated["stages"]["captions"]["status"], "completed")
                self.assertEqual(updated["stages"]["planning"]["status"], "pending")
                self.assertEqual(updated["stages"]["render"]["status"], "pending")
                caption_project = Path(updated["run_dir"]) / "caption_project.json"
                old_caption = json.loads(caption_project.read_text(encoding="utf-8"))
                self.assertEqual(
                    (old_caption["output"]["width"], old_caption["output"]["height"], old_caption["output"]["fps"]),
                    (320, 180, 30),
                )

                replanned = manager.start(created["job_id"], resume=True)
                finished = manager.wait(created["job_id"], timeout=120)
                self.assertEqual(finished["status"], "completed", finished)
                project_path = Path(finished["results"]["clips"][0]["edit_project_path"])
                project = json.loads(project_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    (project["sequence"]["width"], project["sequence"]["height"], project["sequence"]["fps"]),
                    (720, 1280, 60),
                )
                output_path = Path(finished["results"]["completed_outputs"][0])
                probed = subprocess.run(
                    [
                        FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json", str(output_path),
                    ], check=True, capture_output=True, text=True, shell=False,
                )
                stream = json.loads(probed.stdout)["streams"][0]
                self.assertEqual((int(stream["width"]), int(stream["height"])), (720, 1280))
                self.assertEqual(stream["avg_frame_rate"], "60/1")
                self.assertEqual(replanned["pipeline_version"], "v2")


class Phase6JobManagerRoutingTests(unittest.TestCase):
    @staticmethod
    def _settings():
        return {
            "output_width": 320, "output_height": 180, "output_fps": 30,
            "max_chars": 32, "use_llm_captions": False,
            "caption_model": "deterministic", "default_template_id": "standard",
            "video_encoder": "auto",
        }

    def test_new_job_defaults_to_v2_and_dispatches_injected_runner(self):
        calls = []

        def runner(source, **kwargs):
            calls.append({"source": source, **kwargs})
            callback = kwargs["progress_callback"]
            for stage in ("acquire", "transcribe", "jumpcut", "captions", "planning", "render"):
                callback(stage, 1.0, stage)
            return {
                "pipeline_version": "v2", "status": "completed", "clips": [],
                "completed_outputs": [], "failed_outputs": [],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs", pipeline_runner=runner)
            created = manager.create_job("local.mp4", is_local=True, settings=self._settings())
            finished = manager.wait(created["job_id"], timeout=5)
            self.assertEqual(finished["pipeline_version"], "v2")
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(calls[0]["pipeline_version"], "v2")
            self.assertEqual(set(finished["stages"]), {"acquire", "transcribe", "jumpcut", "captions", "planning", "render"})
            self.assertEqual(finished["results"]["pipeline_version"], "v2")

    def test_explicit_legacy_job_keeps_legacy_stage_order(self):
        def runner(_source, **kwargs):
            for stage in ("acquire", "transcribe", "jumpcut", "captions", "render", "highlight"):
                kwargs["progress_callback"](stage, 1.0, stage)
            return {"status": "completed", "final_video_path": "legacy.mp4"}

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs", pipeline_runner=runner)
            created = manager.create_job("legacy.mp4", is_local=True, settings=self._settings(), pipeline_version="legacy")
            finished = manager.wait(created["job_id"], timeout=5)
            self.assertEqual(finished["pipeline_version"], "legacy")
            self.assertEqual(list(finished["stages"]), ["acquire", "transcribe", "jumpcut", "captions", "render", "highlight"])

    def test_v2_partial_result_is_not_marked_completed(self):
        def runner(_source, **kwargs):
            kwargs["progress_callback"]("render", 0.66, "クリップ 2/3")
            return {
                "pipeline_version": "v2", "status": "failed", "clips": [
                    {"clip_id": "clip_000001", "state": "completed"},
                    {"clip_id": "clip_000002", "state": "failed"},
                ],
                "completed_outputs": ["a.mp4"], "failed_outputs": [{"clip_id": "clip_000002"}],
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs", pipeline_runner=runner)
            created = manager.create_job("partial.mp4", is_local=True, settings=self._settings())
            finished = manager.wait(created["job_id"], timeout=5)
            self.assertEqual(finished["status"], "failed")
            self.assertEqual(finished["results"]["failed_outputs"][0]["clip_id"], "clip_000002")

    def test_v2_setting_invalidation_preserves_human_spectra(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            created = manager.create_job("source.mp4", is_local=True, settings=self._settings(), auto_start=False)
            stored = manager._load(created["job_id"])
            for stage in stored["stages"].values():
                stage.update({"status": "completed", "progress": 1.0})
            run_dir = Path(stored["run_dir"])
            (run_dir / "planning" / "clips" / "clip_000001").mkdir(parents=True)
            (run_dir / "planning" / "clips" / "clip_000001" / "manual.spectra").write_text("human", encoding="utf-8")
            manager._save(stored)

            updated = manager.update_settings(created["job_id"], {"output_width": 720})

            self.assertEqual(updated["stages"]["captions"]["status"], "completed")
            self.assertEqual(updated["stages"]["planning"]["status"], "pending")
            self.assertEqual(updated["stages"]["render"]["status"], "pending")
            self.assertTrue((run_dir / "planning" / "clips" / "clip_000001" / "manual.spectra").is_file())

    def test_manifest_without_pipeline_version_is_adopted_as_legacy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = JobManager(Path(temp_dir) / "runs")
            created = manager.create_job("old.mp4", is_local=True, settings=self._settings(), auto_start=False, pipeline_version="legacy")
            path = manager._manifest_path(created["job_id"])
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("pipeline_version", None)
            data["schema_version"] = 2
            path.write_text(json.dumps(data), encoding="utf-8")

            migrated = manager._load(created["job_id"])

            self.assertEqual(migrated["pipeline_version"], "legacy")
            self.assertEqual(migrated["schema_version"], 3)
            self.assertIn("highlight", migrated["stages"])


class _CancelableProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return -15 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return -15


class Phase6CancellationTests(unittest.TestCase):
    def test_external_render_termination_is_scoped_and_preserves_final_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "edit_project.json"
            project.write_text("{}", encoding="utf-8")
            output = root / "rendered.mp4"
            output.write_bytes(b"previous")
            cancel_event = threading.Event()
            cancel_event.set()
            process = _CancelableProcess()

            def popen(_argv, **kwargs):
                kwargs["stdout"].write(b"{}")
                kwargs["stderr"].write(b"cancelled")
                return process

            config = VideoEditorConfig(command=("node", "fake-renderer"), source="test", require_built_artifact=False)
            invocation = RenderInvocation(
                "clip_000001", project, output, root / ".render.mp4", root / "stdout.log", root / "stderr.log", cancel_event
            )
            with self.assertRaises(PipelineCancelled):
                VideoEditorRunner(config, popen_factory=popen).render(invocation)
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertTrue(process.terminated)


class Phase61RerenderTests(unittest.TestCase):
    @staticmethod
    def _settings():
        return {
            "output_width": 320, "output_height": 180, "output_fps": 30,
            "max_chars": 32, "use_llm_captions": False,
            "caption_model": "deterministic", "default_template_id": "standard",
            "video_encoder": "auto",
        }

    def _create_fixture(self, root: Path):
        manager = JobManager(root / "runs")
        created = manager.create_job(
            "source.mp4", is_local=True, settings=self._settings(),
            auto_start=False, pipeline_version="v2",
        )
        run_dir = Path(created["run_dir"])
        planning_dir, project_path, output_path = _write_planning_contract_fixture(run_dir)
        output_path.write_bytes(b"old")
        (planning_dir / "render_manifest.v1.json").write_text("old-manifest", encoding="utf-8")
        spectra_path = project_path.parent / "manual.spectra"
        spectra_path.write_text("human", encoding="utf-8")
        return manager, created["job_id"], project_path, output_path, spectra_path

    def test_successful_v2_rerender_preserves_inputs_and_atomically_replaces_output(self):
        with tempfile.TemporaryDirectory(prefix="phase61-rerender-success-") as temp_dir:
            manager, job_id, project_path, output_path, spectra_path = self._create_fixture(Path(temp_dir))
            project_hash = hashlib.sha256(project_path.read_bytes()).hexdigest()
            updated = manager.request_rerender(job_id, auto_start=False)

            self.assertEqual(updated["pipeline_version"], "v2")
            self.assertEqual(output_path.read_bytes(), b"old")
            self.assertFalse((output_path.parents[3] / "render_manifest.v1.json").exists())
            self.assertEqual(hashlib.sha256(project_path.read_bytes()).hexdigest(), project_hash)
            self.assertEqual(spectra_path.read_text(encoding="utf-8"), "human")

            invocation = RenderInvocation(
                "clip_000001", project_path, output_path,
                output_path.with_name(".render-success.mp4"),
                output_path.with_name("render.stdout.log"),
                output_path.with_name("render.stderr.log"),
            )
            result = _fake_runner(exit_code=0, payload=b"new").render(invocation)
            self.assertEqual(result.state, "completed")
            self.assertEqual(output_path.read_bytes(), b"new")
            self.assertFalse(invocation.staging_path.exists())

    def test_failed_v2_rerender_keeps_old_output_and_records_failed_manifest(self):
        with tempfile.TemporaryDirectory(prefix="phase61-rerender-failed-") as temp_dir:
            manager, job_id, _project_path, output_path, _spectra_path = self._create_fixture(Path(temp_dir))
            manager.request_rerender(job_id, auto_start=False)
            planning_dir = output_path.parents[3]
            result = render_planning_outputs(
                planning_dir,
                runner=_fake_runner(exit_code=1),
            )

            self.assertEqual(result.manifest["summary"]["failed"], 1)
            self.assertEqual(result.manifest["clips"][0]["state"], "failed")
            self.assertEqual(output_path.read_bytes(), b"old")
            self.assertFalse(list(output_path.parent.glob(".render-*.mp4")))

    def test_failed_v2_rerender_marks_job_failed_without_deleting_old_output(self):
        with tempfile.TemporaryDirectory(prefix="phase61-rerender-job-failed-") as temp_dir:
            manager, job_id, _project_path, output_path, _spectra_path = self._create_fixture(Path(temp_dir))

            def runner(_source, **kwargs):
                rendered = render_planning_outputs(
                    Path(kwargs["existing_run_dir"]) / "planning",
                    runner=_fake_runner(exit_code=1),
                )
                failed = [
                    {"clip_id": item["clip_id"], "state": item["state"]}
                    for item in rendered.manifest["clips"]
                    if item["state"] != "completed"
                ]
                return {
                    "pipeline_version": "v2",
                    "status": "failed",
                    "clips": rendered.manifest["clips"],
                    "completed_outputs": [],
                    "failed_outputs": failed,
                }

            manager._pipeline_runner = runner
            manager.request_rerender(job_id, auto_start=True)
            finished = manager.wait(job_id, timeout=10)
            render_manifest = Path(finished["run_dir"]) / "planning" / "render_manifest.v1.json"
            render_data = json.loads(render_manifest.read_text(encoding="utf-8"))

            self.assertEqual(finished["status"], "failed")
            self.assertEqual(render_data["summary"]["failed"], 1)
            self.assertEqual(output_path.read_bytes(), b"old")

    def test_canceled_v2_rerender_keeps_old_output_and_removes_staging(self):
        with tempfile.TemporaryDirectory(prefix="phase61-rerender-canceled-") as temp_dir:
            manager, job_id, project_path, output_path, _spectra_path = self._create_fixture(Path(temp_dir))
            manager.request_rerender(job_id, auto_start=False)
            cancel_event = threading.Event()
            cancel_event.set()
            process = _CancelableProcess()

            def popen(_argv, **kwargs):
                kwargs["stdout"].write(b'{"ok":true}')
                kwargs["stderr"].write(b"cancelled")
                return process

            invocation = RenderInvocation(
                "clip_000001", project_path, output_path,
                output_path.with_name(".render-cancel.mp4"),
                output_path.with_name("render.stdout.log"),
                output_path.with_name("render.stderr.log"),
                cancel_event,
            )
            config = VideoEditorConfig(
                command=(sys.executable, "-c", "pass"),
                source="test",
                require_built_artifact=False,
            )
            with self.assertRaises(PipelineCancelled):
                VideoEditorRunner(config, popen_factory=popen).render(invocation)
            self.assertEqual(output_path.read_bytes(), b"old")
            self.assertFalse(invocation.staging_path.exists())
            self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
