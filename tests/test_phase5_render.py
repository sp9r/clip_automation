import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from planning_manifest import new_manifest, record_stage, write_manifest
from render_manifest import load_manifest
from render_pipeline import render_planning_outputs
from video_editor_runner import (
    RenderInvocation,
    VideoEditorConfig,
    VideoEditorNotBuiltError,
    VideoEditorNotFoundError,
    VideoEditorRunner,
    resolve_video_editor_config,
)


class FakeProcess:
    def __init__(self, exit_code):
        self.exit_code = exit_code

    def wait(self):
        return self.exit_code


class Phase5RenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "planning space (日本語)"
        self.root.mkdir(parents=True)
        self.calls = []
        self.behavior = {}
        self._make_planning("clip_000001", "A")
        self._make_planning("clip_000002", "B")
        self._make_planning("clip_000003", "C")

    def tearDown(self):
        self.temp.cleanup()

    def _make_planning(self, clip_id, text):
        clip_dir = self.root / "clips" / clip_id
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "clip_plan.v1.json").write_text(json.dumps({"clip_id": clip_id, "text": text}), encoding="utf-8")
        (clip_dir / "edit_project.json").write_text(json.dumps({"schema_version": "edit_project.v1", "project": {"id": clip_id}}), encoding="utf-8")
        (clip_dir / "manual.spectra").write_bytes(b"human-owned")

    def _write_planning_manifest(self, states=None):
        states = states or {clip_id: "completed" for clip_id in ("clip_000001", "clip_000002", "clip_000003")}
        manifest = new_manifest(input_fingerprint="a" * 64, provider_versions={"candidate": "test"})
        artifacts = []
        for clip_id, state in states.items():
            path = self.root / "clips" / clip_id / "edit_project.json"
            artifacts.append({"clip_id": clip_id, "path": str(path), "schema_version": "edit_project.v1", "state": state})
        record_stage(manifest, "contract_generation", state="failed" if "failed" in states.values() else "completed", artifacts=artifacts, schema_version="edit_project.v1")
        write_manifest(self.root / "job_manifest.json", manifest)

    def _popen(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        output = Path(argv[argv.index("-o") + 1])
        clip_id = Path(argv[argv.index("render") + 1]).parent.name
        behavior = self.behavior.get(clip_id, {"exit": 0, "output": b"new-output", "stdout": {"clip_id": clip_id, "ok": True}, "stderr": "progress"})
        kwargs["stdout"].write(json.dumps(behavior.get("stdout"), ensure_ascii=False).encode("utf-8"))
        kwargs["stderr"].write(str(behavior.get("stderr", "")).encode("utf-8"))
        if behavior.get("output") is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(behavior["output"])
        return FakeProcess(behavior.get("exit", 0))

    def _runner(self):
        config = VideoEditorConfig(command=("node", "fake renderer (日本語).mjs"), source="test", require_built_artifact=False)
        return VideoEditorRunner(config, popen_factory=self._popen)

    def test_three_independent_clips_are_rendered_without_concat(self):
        self._write_planning_manifest()
        result = render_planning_outputs(self.root, runner=self._runner())
        self.assertFalse(result.failed)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([call["argv"][call["argv"].index("render") + 1] for call in self.calls], [
            str(self.root / "clips" / "clip_000001" / "edit_project.json"),
            str(self.root / "clips" / "clip_000002" / "edit_project.json"),
            str(self.root / "clips" / "clip_000003" / "edit_project.json"),
        ])
        self.assertEqual([call["argv"][call["argv"].index("-o") + 1].replace("\\", "/").split("/clips/")[1].split("/")[0] for call in self.calls], ["clip_000001", "clip_000002", "clip_000003"])
        self.assertTrue(all(call["kwargs"]["shell"] is False for call in self.calls))
        manifest = load_manifest(self.root / "render_manifest.v1.json")
        self.assertEqual(manifest["summary"], {"total": 3, "completed": 3, "failed": 0, "skipped": 0})

    def test_middle_failure_does_not_stop_third_clip(self):
        self._write_planning_manifest()
        self.behavior["clip_000002"] = {"exit": 1, "output": None, "stdout": {"clip_id": "clip_000002"}, "stderr": "renderer failed"}
        result = render_planning_outputs(self.root, runner=self._runner())
        self.assertTrue(result.failed)
        self.assertEqual(len(self.calls), 3)
        manifest = load_manifest(self.root / "render_manifest.v1.json")
        self.assertEqual(manifest["summary"], {"total": 3, "completed": 2, "failed": 1, "skipped": 0})
        self.assertEqual(manifest["clips"][1]["state"], "failed")
        self.assertTrue((self.root / "clips" / "clip_000001" / "automation" / "rendered.mp4").is_file())
        self.assertTrue((self.root / "clips" / "clip_000003" / "automation" / "rendered.mp4").is_file())

    def test_exit_zero_missing_or_zero_output_and_invalid_stdout_fail(self):
        for behavior in (
            {"exit": 0, "output": None, "stdout": {"ok": True}},
            {"exit": 0, "output": b"", "stdout": {"ok": True}},
            {"exit": 0, "output": b"output", "stdout": "not-object"},
        ):
            self.setUp()
            try:
                self._write_planning_manifest({"clip_000001": "completed"})
                self.behavior["clip_000001"] = behavior
                result = render_planning_outputs(self.root, runner=self._runner())
                self.assertEqual(result.manifest["summary"]["failed"], 1)
            finally:
                self.tearDown()

    def test_missing_renderer_is_actionable(self):
        config = VideoEditorConfig(command=("missing-node-executable", "video-editor.mjs"), source="test", require_built_artifact=False)
        invocation_root = self.root / "clips" / "clip_000001" / "automation"
        invocation = RenderInvocation("clip_000001", self.root / "clips" / "clip_000001" / "edit_project.json", invocation_root / "rendered.mp4", invocation_root / ".render.mp4", invocation_root / "stdout.log", invocation_root / "stderr.log")
        with self.assertRaises(VideoEditorNotFoundError):
            VideoEditorRunner(config).render(invocation)

    def test_command_resolution_uses_explicit_then_env_json_then_root_then_sibling(self):
        explicit = resolve_video_editor_config(
            explicit_command=["node with spaces", "editor (日本語).mjs"],
            video_editor_root=self.root,
            repo_root=self.root,
            env={"VIDEO_EDITOR_COMMAND_JSON": '["env-node", "env-editor.mjs"]'},
            require_built_artifact=False,
        )
        self.assertEqual(explicit.source, "explicit")
        self.assertEqual(explicit.command, ("node with spaces", "editor (日本語).mjs"))

        env_config = resolve_video_editor_config(
            video_editor_root=self.root,
            repo_root=self.root,
            env={"VIDEO_EDITOR_COMMAND_JSON": '["env-node", "env-editor.mjs"]'},
            require_built_artifact=False,
        )
        self.assertEqual(env_config.source, "env")
        self.assertEqual(env_config.command, ("env-node", "env-editor.mjs"))

        root_config = resolve_video_editor_config(
            video_editor_root=self.root,
            repo_root=self.root,
            env={},
            node_binary="node with spaces",
            require_built_artifact=False,
        )
        self.assertEqual(root_config.source, "root")
        self.assertEqual(root_config.command, ("node with spaces", str(self.root / "scripts" / "video-editor.mjs")))

    def test_missing_build_artifact_has_actionable_preflight_error(self):
        root = self.root / "video_editor root (日本語)"
        launcher = root / "scripts" / "video-editor.mjs"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("", encoding="utf-8")
        config = resolve_video_editor_config(
            explicit_command=["node", str(launcher)],
            require_built_artifact=True,
        )
        with self.assertRaises(VideoEditorNotBuiltError) as context:
            VideoEditorRunner(config).preflight()
        self.assertIn("Build video_editor before rendering", str(context.exception))

    def test_explicit_single_element_command_is_generic(self):
        config = VideoEditorConfig(command=("video-editor",), source="explicit")
        with patch("video_editor_runner.shutil.which", return_value="C:/Tools/video-editor.exe"):
            VideoEditorRunner(config).preflight()

    def test_env_single_element_command_is_generic(self):
        config = resolve_video_editor_config(
            env={"VIDEO_EDITOR_COMMAND_JSON": '["video-editor"]'},
        )
        self.assertEqual(config.source, "env")
        with patch("video_editor_runner.shutil.which", return_value="C:/Tools/video-editor.exe"):
            VideoEditorRunner(config).preflight()

    def test_single_element_missing_command_is_rejected(self):
        config = VideoEditorConfig(command=("missing-video-editor",), source="explicit")
        with patch("video_editor_runner.shutil.which", return_value=None):
            with self.assertRaises(VideoEditorNotFoundError):
                VideoEditorRunner(config).preflight()

    def test_known_node_wrapper_keeps_launcher_and_build_validation(self):
        root = self.root / "known video_editor root"
        launcher = root / "scripts" / "video-editor.mjs"
        artifact = root / "out" / "cli" / "video-editor.js"
        launcher.parent.mkdir(parents=True)
        artifact.parent.mkdir(parents=True)
        launcher.write_text("", encoding="utf-8")
        artifact.write_text("", encoding="utf-8")
        config = resolve_video_editor_config(
            explicit_command=["node", str(launcher)],
            require_built_artifact=True,
        )
        VideoEditorRunner(config).preflight()

    def test_custom_multi_argument_prefix_does_not_treat_command_one_as_script(self):
        config = VideoEditorConfig(
            command=("custom-editor", "--profile", "spectra"),
            source="explicit",
        )
        with patch("video_editor_runner.shutil.which", return_value="C:/Tools/custom-editor.exe"):
            VideoEditorRunner(config).preflight()

    def test_rerun_updates_automation_only_and_preserves_spectra(self):
        self._write_planning_manifest({"clip_000001": "completed"})
        result = render_planning_outputs(self.root, runner=self._runner())
        self.assertFalse(result.failed)
        clip_dir = self.root / "clips" / "clip_000001"
        project_before = (clip_dir / "edit_project.json").read_bytes()
        plan_before = (clip_dir / "clip_plan.v1.json").read_bytes()
        spectra_before = (clip_dir / "manual.spectra").read_bytes()
        self.behavior["clip_000001"] = {"exit": 0, "output": b"rerun-output", "stdout": {"clip_id": "clip_000001", "rerun": True}}
        render_planning_outputs(self.root, runner=self._runner())
        self.assertEqual((clip_dir / "automation" / "rendered.mp4").read_bytes(), b"rerun-output")
        self.assertEqual((clip_dir / "edit_project.json").read_bytes(), project_before)
        self.assertEqual((clip_dir / "clip_plan.v1.json").read_bytes(), plan_before)
        self.assertEqual((clip_dir / "manual.spectra").read_bytes(), spectra_before)

    def test_failed_rerun_preserves_previous_valid_output(self):
        self._write_planning_manifest({"clip_000001": "completed"})
        render_planning_outputs(self.root, runner=self._runner())
        output = self.root / "clips" / "clip_000001" / "automation" / "rendered.mp4"
        previous = output.read_bytes()
        self.behavior["clip_000001"] = {"exit": 1, "output": None, "stdout": {"clip_id": "clip_000001"}, "stderr": "new failure"}
        result = render_planning_outputs(self.root, runner=self._runner())
        self.assertEqual(result.manifest["summary"]["failed"], 1)
        self.assertEqual(output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
