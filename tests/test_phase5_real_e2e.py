import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from planning_core import ChunkingConfig, DecisionSettings
from planning_pipeline import run_planning
from planning_providers import DeterministicCandidateProvider, DeterministicEvaluationProvider
from render_pipeline import render_planning_outputs


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VIDEO_EDITOR_ROOT = Path(os.environ.get("VIDEO_EDITOR_ROOT", str(REPOSITORY_ROOT.parent / "video_editor"))).resolve()
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
REAL_E2E_AVAILABLE = bool(
    FFMPEG
    and FFPROBE
    and shutil.which("node")
    and (VIDEO_EDITOR_ROOT / "scripts" / "video-editor.mjs").is_file()
    and (VIDEO_EDITOR_ROOT / "out" / "cli" / "video-editor.js").is_file()
)


@unittest.skipUnless(REAL_E2E_AVAILABLE, "sibling video_editor build or ffmpeg/ffprobe is unavailable")
class Phase5GeneratedMediaE2ETests(unittest.TestCase):
    def test_planning_to_external_video_editor_produces_independent_mp4s(self):
        with tempfile.TemporaryDirectory(prefix="phase5-real-e2e-") as temporary:
            root = Path(temporary)
            run_dir = root / "source run (日本語)"
            planning_dir = root / "planning output (space)"
            run_dir.mkdir(parents=True)
            fixture = REPOSITORY_ROOT / "tests" / "fixtures" / "edit_project_v1"
            for name in ("caption_project.v2.json", "edit_regions.json", "edl.json", "transcript_words.json"):
                shutil.copy2(fixture / name, run_dir / name)
            source_path = run_dir / "source media (generated).mp4"
            completed = subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=22",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=22",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-t", "22", str(source_path),
                ],
                check=False,
                capture_output=True,
                shell=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            (run_dir / "caption_project.v2.json").write_text(
                (run_dir / "caption_project.v2.json").read_text(encoding="utf-8").replace('"source_path": "source.mp4"', f'"source_path": "{source_path.name}"'),
                encoding="utf-8",
            )
            clean_transcript = [
                {"start": 0.0, "end": 1.0, "text": "あ"},
                {"start": 1.0, "end": 2.0, "text": "い"},
                {"start": 2.0, "end": 3.0, "text": "う"},
                {"start": 3.0, "end": 4.0, "text": "え"},
            ]
            (run_dir / "clean_transcript.json").write_text(json.dumps(clean_transcript, ensure_ascii=False), encoding="utf-8")
            planning = run_planning(
                run_dir,
                planning_dir=planning_dir,
                source_media_probe={"duration": 22.0, "width": 320, "height": 180, "fps": 30.0, "codec": "h264", "sha256": source_hash},
                candidate_provider=DeterministicCandidateProvider(selector=lambda chunk: [
                    {"start": 0.0, "end": 1.0, "reason": "A"},
                    {"start": 2.0, "end": 3.0, "reason": "B"},
                ]),
                evaluator_provider=DeterministicEvaluationProvider(),
                chunking=ChunkingConfig(max_duration=10.0, overlap_duration=0.0),
                decision_settings=DecisionSettings(max_selected_clips=2, min_duration=0.5),
                target_output={"width": 320, "height": 180, "fps": 30},
            )
            self.assertEqual([plan["clip_id"] for plan in planning.plans], ["clip_000001", "clip_000002"])
            rendered = render_planning_outputs(planning_dir, video_editor_root=VIDEO_EDITOR_ROOT, repo_root=REPOSITORY_ROOT)
            self.assertFalse(rendered.failed)
            self.assertEqual(rendered.manifest["summary"], {"total": 2, "completed": 2, "failed": 0, "skipped": 0})
            outputs = []
            probe_results = []
            for clip_id in ("clip_000001", "clip_000002"):
                output = planning_dir / "clips" / clip_id / "automation" / "rendered.mp4"
                self.assertTrue(output.is_file())
                probe = subprocess.run(
                    [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                    check=True,
                    capture_output=True,
                    text=True,
                    shell=False,
                )
                data = json.loads(probe.stdout)
                video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
                audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
                self.assertEqual((int(video["width"]), int(video["height"])), (320, 180))
                self.assertGreater(float(data["format"]["duration"]), 0)
                self.assertEqual(audio["codec_type"], "audio")
                probe_results.append({
                    "clip_id": clip_id,
                    "path": output.as_posix(),
                    "width": int(video["width"]),
                    "height": int(video["height"]),
                    "duration": float(data["format"]["duration"]),
                    "audio_codec": audio.get("codec_name"),
                })
                outputs.append(output)
            self.assertNotEqual(outputs[0], outputs[1])
            self.assertFalse(list(planning_dir.rglob("concat*")))
            manifest = json.loads((planning_dir / "render_manifest.v1.json").read_text(encoding="utf-8"))
            self.assertEqual([item["output"]["path"] for item in manifest["clips"]], [
                "clips/clip_000001/automation/rendered.mp4",
                "clips/clip_000002/automation/rendered.mp4",
            ])
            if os.environ.get("PHASE5_E2E_VERBOSE") == "1":
                print("PHASE5_REAL_E2E " + json.dumps(probe_results, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
