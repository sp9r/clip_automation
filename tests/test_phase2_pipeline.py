import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edit_project_io import load_edit_project
from edit_project_adapter import EditProjectBuildError, build_edit_project_from_artifacts
from planning_core import ChunkingConfig, DecisionSettings
from planning_manifest import load_manifest
from planning_pipeline import PlanningRunError, run_planning
from planning_providers import DeterministicCandidateProvider, DeterministicEvaluationProvider, FailingCandidateProvider
import planning_pipeline
from unittest.mock import patch


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "edit_project_v1"


class Phase2PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.run = root / "source_run"
        self.planning = root / "planning"
        self.run.mkdir()
        for path in FIXTURE.iterdir():
            shutil.copy2(path, self.run / path.name)
        (self.run / "source.mp4").write_bytes(b"phase2 deterministic source")
        clean = [
            {"start": 0.0, "end": 1.0, "text": "あ"},
            {"start": 1.0, "end": 2.0, "text": "い"},
            {"start": 2.0, "end": 3.0, "text": "う"},
            {"start": 3.0, "end": 4.0, "text": "え"},
        ]
        (self.run / "clean_transcript.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        self.probe = {"duration": 30.0, "width": 1920, "height": 1080, "fps": 30.0, "codec": "h264"}

    def tearDown(self):
        self.temp.cleanup()

    def _hashes(self):
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.run.iterdir()
            if path.is_file()
        }

    def test_three_candidates_create_three_independent_plans_and_projects(self):
        before = self._hashes()
        provider = DeterministicCandidateProvider(
            selector=lambda chunk: [
                {"start": 0.0, "end": 1.0, "reason": "first"},
                {"start": 1.0, "end": 3.0, "reason": "cross edl"},
                {"start": 3.0, "end": 4.0, "reason": "third"},
            ]
        )
        result = run_planning(
            self.run,
            planning_dir=self.planning,
            source_media_probe=self.probe,
            candidate_provider=provider,
            evaluator_provider=DeterministicEvaluationProvider(),
            chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
            decision_settings=DecisionSettings(max_selected_clips=3, min_duration=0.5),
            target_output={"width": 720, "height": 1280, "fps": 30},
        )
        self.assertEqual(len(result.plans), 3)
        self.assertEqual(len(result.edit_projects), 3)
        self.assertEqual([plan["clip_id"] for plan in result.plans], ["clip_000001", "clip_000002", "clip_000003"])
        for plan in result.plans:
            clip_dir = self.planning / "clips" / plan["clip_id"]
            self.assertTrue((clip_dir / "clip_plan.v1.json").is_file())
            self.assertTrue((clip_dir / "edit_project.json").is_file())
            project = load_edit_project(clip_dir / "edit_project.json")
            self.assertEqual(project["project"]["id"], plan["clip_id"])
            self.assertEqual(project["sequence"]["width"], 720)
            self.assertEqual(project["sequence"]["height"], 1280)
        cross_project = load_edit_project(self.planning / "clips" / "clip_000002" / "edit_project.json")
        self.assertEqual(len(cross_project["tracks"][0]["clips"]), 2)
        self.assertEqual(cross_project["tracks"][0]["clips"][0]["source"]["start"], 11.0)
        self.assertEqual(cross_project["tracks"][0]["clips"][1]["source"]["start"], 20.0)
        manifest = load_manifest(self.planning / "job_manifest.json")
        plan_artifacts = manifest["stages"]["clip_planning"]["artifacts"]
        project_artifacts = manifest["stages"]["contract_generation"]["artifacts"]
        self.assertEqual([item["clip_id"] for item in plan_artifacts], ["clip_000001", "clip_000002", "clip_000003"])
        self.assertEqual([item["clip_id"] for item in project_artifacts], ["clip_000001", "clip_000002", "clip_000003"])
        self.assertTrue(all(len(item["sha256"]) == 64 for item in plan_artifacts + project_artifacts))
        self.assertEqual(before, self._hashes())

    def test_manifest_tracks_all_phase2_stages_and_hashes(self):
        result = run_planning(
            self.run,
            planning_dir=self.planning,
            source_media_probe=self.probe,
            candidate_provider=DeterministicCandidateProvider(),
            evaluator_provider=DeterministicEvaluationProvider(),
            chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
            decision_settings=DecisionSettings(max_selected_clips=1, min_duration=0.5),
        )
        manifest = load_manifest(self.planning / "job_manifest.json")
        self.assertEqual(set(manifest["stages"]), {"candidate_generation", "candidate_evaluation", "clip_decision", "clip_planning", "contract_generation"})
        self.assertTrue(all(item["artifact_sha256"] for item in manifest["stages"].values()))
        self.assertEqual(manifest["stages"]["contract_generation"]["schema_version"], "edit_project.v1")
        self.assertEqual(len(manifest["stages"]["clip_planning"]["artifacts"]), 1)
        self.assertEqual(len(manifest["stages"]["contract_generation"]["artifacts"]), 1)
        self.assertEqual(result.manifest, manifest)

    def test_regions_argument_does_not_require_legacy_edit_regions(self):
        (self.run / "edit_regions.json").unlink()
        result = run_planning(
            self.run,
            planning_dir=self.planning,
            source_media_probe=self.probe,
            candidate_provider=DeterministicCandidateProvider(),
            evaluator_provider=DeterministicEvaluationProvider(),
            chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
            decision_settings=DecisionSettings(max_selected_clips=1, min_duration=0.5),
        )
        self.assertEqual(len(result.edit_projects), 1)

    def test_none_regions_requires_legacy_edit_regions_explicitly(self):
        (self.run / "edit_regions.json").unlink()
        with self.assertRaises(EditProjectBuildError) as context:
            build_edit_project_from_artifacts(self.run, source_media_probe=self.probe, regions=None)
        self.assertTrue(any(item.code == "missing_input" for item in context.exception.diagnostics))

    def test_unused_legacy_edit_regions_does_not_change_per_clip_project_or_hashes(self):
        regions = json.loads((self.run / "edit_regions.json").read_text(encoding="utf-8"))
        first = build_edit_project_from_artifacts(
            self.run,
            source_media_probe=self.probe,
            regions=regions,
            contract_path=self.planning / "clips" / "clip_000001" / "edit_project.json",
        )
        (self.run / "edit_regions.json").write_text(json.dumps([{"legacy": "unused"}]), encoding="utf-8")
        second = build_edit_project_from_artifacts(
            self.run,
            source_media_probe=self.probe,
            regions=regions,
            contract_path=self.planning / "clips" / "clip_000001" / "edit_project.json",
        )
        self.assertEqual(first.project, second.project)
        self.assertFalse(any("edit_regions.json" in key for key in first.project["metadata"]["input_hashes"]))

    def test_manifest_round_trip_validates_multi_artifact_entries(self):
        result = run_planning(
            self.run,
            planning_dir=self.planning,
            source_media_probe=self.probe,
            candidate_provider=DeterministicCandidateProvider(),
            evaluator_provider=DeterministicEvaluationProvider(),
            chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
            decision_settings=DecisionSettings(max_selected_clips=3, min_duration=0.5),
        )
        loaded = load_manifest(self.planning / "job_manifest.json")
        self.assertEqual(loaded, result.manifest)
        for stage_name in ("clip_planning", "contract_generation"):
            for artifact in loaded["stages"][stage_name]["artifacts"]:
                self.assertEqual(len(artifact["sha256"]), 64)
                self.assertTrue(artifact["schema_version"])
                self.assertIn(artifact["state"], {"completed", "failed"})

        legacy = json.loads(json.dumps(loaded))
        for item in legacy["stages"].values():
            item.pop("artifacts", None)
        legacy_path = self.planning / "legacy_job_manifest.json"
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
        migrated = load_manifest(legacy_path)
        self.assertEqual(len(migrated["stages"]["clip_planning"]["artifacts"]), 1)
        self.assertEqual(len(migrated["stages"]["contract_generation"]["artifacts"]), 1)

    def test_manifest_keeps_partial_contract_generation_success_and_failure(self):
        provider = DeterministicCandidateProvider(
            selector=lambda chunk: [
                {"start": 0.0, "end": 1.0, "reason": "first"},
                {"start": 1.0, "end": 2.0, "reason": "second"},
                {"start": 2.0, "end": 3.0, "reason": "third"},
            ]
        )
        real_builder = planning_pipeline.build_edit_project_from_artifacts
        calls = 0

        def fail_third(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ValueError("forced third clip failure")
            return real_builder(*args, **kwargs)

        with patch("planning_pipeline.build_edit_project_from_artifacts", side_effect=fail_third):
            with self.assertRaises(PlanningRunError) as context:
                run_planning(
                    self.run,
                    planning_dir=self.planning,
                    source_media_probe=self.probe,
                    candidate_provider=provider,
                    evaluator_provider=DeterministicEvaluationProvider(),
                    chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
                    decision_settings=DecisionSettings(max_selected_clips=3, min_duration=0.5),
                )
        manifest = load_manifest(self.planning / "job_manifest.json")
        entries = manifest["stages"]["contract_generation"]["artifacts"]
        self.assertEqual([entry["clip_id"] for entry in entries], ["clip_000001", "clip_000002", "clip_000003"])
        self.assertEqual([entry["state"] for entry in entries], ["completed", "completed", "failed"])
        self.assertEqual(manifest["stages"]["contract_generation"]["state"], "failed")
        self.assertEqual(context.exception.manifest, manifest)

    def test_provider_failure_is_recorded_and_not_completed(self):
        with self.assertRaises(PlanningRunError):
            run_planning(
                self.run,
                planning_dir=self.planning,
                source_media_probe=self.probe,
                candidate_provider=FailingCandidateProvider(),
                evaluator_provider=DeterministicEvaluationProvider(),
                chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
            )
        manifest = load_manifest(self.planning / "job_manifest.json")
        self.assertEqual(manifest["stages"]["candidate_generation"]["state"], "failed")
        self.assertEqual(manifest["stages"]["candidate_evaluation"]["state"], "failed")
        self.assertNotEqual(manifest["stages"]["candidate_generation"]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
