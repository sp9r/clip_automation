"""既存runへ触れずTimeline Editorの短尺GUI受入fixtureを作る。"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from timeline_project import _atomic_json_write, build_transcript_words, migrate_v1_project


RUN = ROOT / "data" / "runs" / "timeline_editor_fixture"


def main():
    RUN.mkdir(parents=True, exist_ok=True)
    words = build_transcript_words([{"words": [
        {"word": "タイムライン", "start": 0.0, "end": 0.5},
        {"word": "編集の", "start": 0.5, "end": 1.0},
        {"word": "確認です。", "start": 1.0, "end": 1.5},
        {"word": "次の", "start": 1.5, "end": 2.0},
        {"word": "字幕を", "start": 2.0, "end": 2.5},
        {"word": "区切って", "start": 2.5, "end": 3.0},
        {"word": "直せ", "start": 3.0, "end": 3.5},
        {"word": "ます。", "start": 3.5, "end": 4.0},
    ]}])
    v1 = {
        "schema_version": 1,
        "output": {"width": 640, "height": 360, "fps": 30},
        "default_template_id": "standard",
        "captions": [
            {"text": "タイムライン編集の"},
            {"text": "確認です。次の"},
            {"text": "字幕を区切って"},
            {"text": "直せます。"},
        ],
    }
    project = migrate_v1_project(v1, words)
    source = RUN / "source.mp4"
    project["source_path"] = str(source)
    project["edl_path"] = str(RUN / "edl.json")
    _atomic_json_write(RUN / "transcript_words.json", words)
    _atomic_json_write(RUN / "caption_project.v2.json", project)
    _atomic_json_write(RUN / "edl.json", [{"start": 0.0, "end": 4.0}])
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1b2435:s=640x360:d=4",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-r", "30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True, capture_output=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1b2435:s=640x360:d=4",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-r", "30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(RUN / "preview_proxy.mp4"),
    ], check=True, capture_output=True)
    print(RUN)


if __name__ == "__main__":
    main()
