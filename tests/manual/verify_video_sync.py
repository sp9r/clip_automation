import subprocess
from pathlib import Path
from clip import create_clip

ARTIFACT_DIR = Path(r"C:\Users\user\.gemini\antigravity\brain\c88e403d-d305-4614-a450-090d806c3b90")
RUN_DIR = Path(r"C:\Storage\30_Software\90_Self_Made\Apps\clip_automation\data\runs\20260807_081656")

def run_render_and_extract():
    video_path = RUN_DIR / "final_jumpcut_video.partial.mp4" # 元動画
    edl_path = RUN_DIR / "edl.json"
    caption_proj_path = RUN_DIR / "caption_project.json"
    out_mp4 = RUN_DIR / "rendered_fixed_test.mp4"
    
    print("Starting full render test...")
    create_clip(
        video_path=str(video_path),
        edl_json=str(edl_path),
        subtitle_path=None,
        output_mp4=str(out_mp4),
        caption_project_path=str(caption_proj_path),
        output_width=1920,
        output_height=1080,
        output_fps=60,
        encoder="libx264"
    )
    print(f"Render completed: {out_mp4}")
    
    # 複数タイムスタンプ（1秒後, 3秒後, 5秒後, 10秒後）のフレームを抽出
    timestamps = [1.0, 3.0, 5.0, 10.0]
    for ts in timestamps:
        out_png = ARTIFACT_DIR / f"sync_frame_{ts:.1f}s.png"
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(ts), "-i", str(out_mp4),
            "-vframes", "1", str(out_png)
        ], check=True)
        print(f"Extracted frame at {ts}s -> {out_png}")

if __name__ == "__main__":
    run_render_and_extract()
