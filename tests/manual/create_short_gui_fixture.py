"""GUI受入専用の短尺runを新規作成する。既存runには触れない。"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from clip import create_clip
from job_manager import JobManager
from pipeline import PROJECT_ROOT, _create_run_dir
from subtitle_project import create_caption_project, save_caption_project, write_ass_subtitles


def main():
    run_dir = _create_run_dir(PROJECT_ROOT / "data")
    source = run_dir / "gui_fixture_source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x203050:size=640x360:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ],
        check=True,
        capture_output=True,
    )
    transcript = [{"start": 0.0, "end": 3.0, "text": "GUI受入テストです", "words": [
        {"start": 0.0, "end": 1.4, "word": "GUI受入"},
        {"start": 1.4, "end": 3.0, "word": "テストです"},
    ]}]
    captions = [{"id": 1, "start": 0.2, "end": 2.8, "text": "GUI受入テストです"}]
    (run_dir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    (run_dir / "clean_transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
    (run_dir / "edl.json").write_text(json.dumps([{"start": 0.0, "end": 3.0}]), encoding="utf-8")
    (run_dir / "captions.srt").write_text("1\n00:00:00,200 --> 00:00:02,800\nGUI受入テストです\n", encoding="utf-8")
    project = create_caption_project(
        captions=captions, source_path=str(source), edl_path=str(run_dir / "edl.json"),
        run_dir=str(run_dir), width=640, height=360, fps=30,
    )
    save_caption_project(project, run_dir / "caption_project.json")
    write_ass_subtitles(project, run_dir / "captions.ass")
    create_clip(
        str(source), str(run_dir / "edl.json"), str(run_dir / "captions.ass"),
        str(run_dir / "final_jumpcut_video.mp4"),
        caption_project_path=str(run_dir / "caption_project.json"),
        output_width=640, output_height=360, output_fps=30, encoder="libx264",
    )
    (run_dir / "highlight.json").write_text(
        json.dumps({"start": 0.0, "end": 3.0, "reason": "GUI受入"}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = JobManager(PROJECT_ROOT / "data" / "runs").adopt_run(run_dir)
    print(json.dumps({"job_id": manifest["job_id"], "run_dir": str(run_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
