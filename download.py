import os
import yt_dlp

from job_control import raise_if_cancelled


def download_video(
    url: str,
    output_dir: str = "data/temp",
    *,
    progress_callback=None,
    cancel_event=None,
) -> str:
    """
    Downloads a YouTube video using yt_dlp.
    Returns the path to the downloaded video file (mp4).
    """
    os.makedirs(output_dir, exist_ok=True)
    
    def progress_hook(data):
        raise_if_cancelled(cancel_event)
        if not progress_callback:
            return
        if data.get("status") == "finished":
            progress_callback(1.0, "動画取得完了・結合処理中")
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        fraction = min(0.99, downloaded / total) if total else 0.0
        progress_callback(fraction, f"動画取得 {fraction * 100:.1f}%")

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(output_dir, 'video_%(id)s.%(ext)s'),
        'merge_output_format': 'mp4',
        'quiet': False,
        'progress_hooks': [progress_hook],
    }
    
    print(f"Downloading video from {url}...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info_dict = ydl.extract_info(url, download=True)
        # ydl.prepare_filename might return the original video extension before merge
        # but yt-dlp will automatically change the extension to .mp4 after merge
        filename = ydl.prepare_filename(info_dict)
        base, ext = os.path.splitext(filename)
        if ext != '.mp4':
            filename = base + '.mp4'
            
        print(f"Download completed: {filename}")
        return filename

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        url = sys.argv[1]
        print(f"Final video path: {download_video(url)}")
    else:
        print("Usage: python download.py <youtube_url>")
