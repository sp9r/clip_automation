import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from job_control import PipelineCancelled, raise_if_cancelled


def _load_edl(edl_json: str) -> list[dict]:
    with open(edl_json, "r", encoding="utf-8") as file:
        raw_edl = json.load(file)
    edl = []
    for index, item in enumerate(raw_edl, 1):
        start = float(item["start"])
        end = float(item["end"])
        if start < 0 or end <= start:
            raise ValueError(f"EDLの{index}区間目が不正です。")
        edl.append({"start": start, "end": end})
    if not edl:
        raise ValueError("EDLが空です。")
    return edl


def edl_duration(edl_json: str) -> float:
    return sum(item["end"] - item["start"] for item in _load_edl(edl_json))


def _select_expression(edl: list[dict]) -> str:
    # 1,000区間超でも入力を一度だけデコードする。巨大なtrim/concat分岐を作らない。
    return "+".join(
        f"between(t,{item['start']:.6f},{item['end']:.6f})" for item in edl
    )


@lru_cache(maxsize=1)
def nvenc_diagnostic() -> dict:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=size=640x360:rate=1",
        "-frames:v",
        "1",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "NUL" if os.name == "nt" else "/dev/null",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "ffmpeg_unavailable"}
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "nvenc_probe_timeout"}
    except OSError:
        return {"available": False, "reason": "nvenc_probe_os_error"}
    if result.returncode == 0:
        return {"available": True, "reason": "none"}
    stderr = (result.stderr or "").lower()
    if "nvcuda" in stderr or "cuda" in stderr and "driver" in stderr:
        reason = "cuda_runtime_unavailable"
    elif "nvenc" in stderr and ("not found" in stderr or "no such" in stderr):
        reason = "nvenc_encoder_unavailable"
    elif "driver" in stderr:
        reason = "nvenc_driver_unsupported"
    else:
        reason = f"nvenc_probe_exit_{result.returncode}"
    return {"available": False, "reason": reason}


def nvenc_available() -> bool:
    return bool(nvenc_diagnostic()["available"])


def hardware_decode_diagnostic(video_path: str) -> dict:
    """Probe CUDA/NVDEC for one frame without changing the render filter graph."""

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "cuda",
        "-i",
        str(Path(video_path).resolve()),
        "-frames:v",
        "1",
        "-an",
        "-f",
        "null",
        "NUL" if os.name == "nt" else "/dev/null",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return {"mode": "software", "fallback": True, "fallbackReason": "ffmpeg_unavailable"}
    except subprocess.TimeoutExpired:
        return {"mode": "software", "fallback": True, "fallbackReason": "cuda_decode_probe_timeout"}
    except OSError:
        return {"mode": "software", "fallback": True, "fallbackReason": "cuda_decode_probe_os_error"}
    if result.returncode == 0:
        return {"mode": "cuda", "fallback": False, "fallbackReason": "none"}
    stderr = (result.stderr or "").lower()
    if "nvdec" in stderr or "cuda" in stderr:
        reason = "cuda_decode_unavailable"
    else:
        reason = f"cuda_decode_probe_exit_{result.returncode}"
    return {"mode": "software", "fallback": True, "fallbackReason": reason}


def resolve_video_encoder(requested: str = "auto") -> str:
    requested = str(requested or "auto").lower()
    if requested not in {"auto", "h264_nvenc", "libx264"}:
        raise ValueError("動画エンコーダーはauto / h264_nvenc / libx264から選んでください。")
    if requested == "h264_nvenc" and not nvenc_available():
        raise RuntimeError("この環境ではh264_nvencを初期化できません。")
    if requested == "auto":
        return "h264_nvenc" if nvenc_available() else "libx264"
    return requested


def resolve_video_encoder_diagnostic(requested: str = "auto") -> dict:
    requested = str(requested or "auto").lower()
    if requested not in {"auto", "h264_nvenc", "libx264"}:
        raise ValueError("動画エンコーダーはauto / h264_nvenc / libx264から選んでください。")
    if requested == "libx264":
        return {
            "selected": "libx264",
            "class": "cpu",
            "fallback": False,
            "fallbackReason": "explicit_cpu_encoder",
        }
    probe = nvenc_diagnostic()
    if probe["available"]:
        return {
            "selected": "h264_nvenc",
            "class": "gpu",
            "fallback": False,
            "fallbackReason": "none",
        }
    if requested == "h264_nvenc":
        raise RuntimeError(f"この環境ではh264_nvencを初期化できません。理由: {probe['reason']}")
    return {
        "selected": "libx264",
        "class": "cpu",
        "fallback": True,
        "fallbackReason": probe["reason"],
    }


def _encoder_arguments(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "21",
            "-b:v",
            "0",
        ]
    return ["-c:v", "libx264", "-preset", "fast", "-crf", "23"]


def _terminate_process(process: subprocess.Popen):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def create_clip(
    video_path: str,
    edl_json: str,
    subtitle_path: str | None,
    output_mp4: str,
    *,
    caption_project_path: str | None = None,
    output_width: int = 1920,
    output_height: int = 1080,
    output_fps: int = 60,
    encoder: str = "auto",
    hardware_decode: str = "auto",
    progress_callback=None,
    cancel_event=None,
):
    """EDLを1回のデコードで選択し、HTML5透過字幕またはASS字幕をオーバーレイしてMP4を生成する。"""
    raise_if_cancelled(cancel_event)
    edl = _load_edl(edl_json)
    expected_duration = sum(item["end"] - item["start"] for item in edl)

    output_width = int(output_width)
    output_height = int(output_height)
    output_fps = int(output_fps)
    if output_width < 320 or output_height < 240:
        raise ValueError("出力解像度が小さすぎます。")
    if not 1 <= output_fps <= 240:
        raise ValueError("FPSは1〜240で指定してください。")

    output_path = Path(output_mp4).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
    filter_script_path = output_path.with_suffix(".filter.txt")
    if partial_path.exists():
        partial_path.unlink()

    select_expression = _select_expression(edl)
    video_filters = [
        f"select='{select_expression}'",
        "setpts=N/FRAME_RATE/TB",
        f"scale=w={output_width}:h={output_height}:force_original_aspect_ratio=decrease",
        f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={output_fps}",
    ]

    html_inputs = []
    html_filter_str = ""
    use_html_overlay = False

    # HTML5 透過字幕オーバーレイの準備
    if caption_project_path and os.path.exists(caption_project_path):
        try:
            from subtitle_project import load_caption_project
            from renderer_html import prepare_html_captions, build_ffmpeg_html_overlay_filter
            
            proj = load_caption_project(caption_project_path)
            rendered_items = prepare_html_captions(proj, edl)

            if rendered_items:
                html_inputs, html_filter_str = build_ffmpeg_html_overlay_filter(rendered_items)
                use_html_overlay = True
        except Exception as e:
            print(f"[Warning] HTML5 subtitle overlay rendering failed, falling back to ASS: {e}")
            use_html_overlay = False

    if not use_html_overlay and subtitle_path:
        abs_subtitle = os.path.abspath(subtitle_path).replace("\\", "/")
        subtitle_escaped = abs_subtitle.replace(":", "\\:")
        video_filters.append(f"subtitles='{subtitle_escaped}'")

    filter_lines = [
        f"[0:v]{','.join(video_filters)}[cut_v];"
    ]

    if use_html_overlay and html_filter_str:
        # overlay フィルターの最初の入力を [cut_v] に、最後の出力を [final_v] に置き換え
        modified_filter = html_filter_str.replace("[0:v]", "[cut_v]", 1)
        if "[v_out]" in modified_filter:
            modified_filter = modified_filter.replace("[v_out]", "[final_v]")
        else:
            # 1件のみの場合
            modified_filter = modified_filter.rsplit("]", 1)[0] + "][final_v]"
        filter_lines.append(f"{modified_filter};")
    else:
        filter_lines.append("[cut_v]null[final_v];")


    filter_lines.append(f"[0:a]aselect='{select_expression}',asetpts=N/SR/TB[final_a]")
    filter_script_path.write_text("\n".join(filter_lines), encoding="utf-8")

    hardware_decode = str(hardware_decode or "auto").lower()
    if hardware_decode not in {"auto", "cuda", "software"}:
        raise ValueError("hardware_decodeはauto / cuda / softwareから選んでください。")
    if hardware_decode == "cuda":
        decode_info = hardware_decode_diagnostic(str(Path(video_path).resolve()))
        if decode_info["mode"] != "cuda":
            raise RuntimeError(f"CUDA decodeを初期化できません。理由: {decode_info['fallbackReason']}")
    elif hardware_decode == "software":
        decode_info = {"mode": "software", "fallback": False, "fallbackReason": "explicit_software_decode"}
    else:
        decode_info = hardware_decode_diagnostic(str(Path(video_path).resolve()))

    encoder_info = resolve_video_encoder_diagnostic(encoder)
    selected_encoder = encoder_info["selected"]
    decode_arguments = ["-hwaccel", "cuda"] if decode_info["mode"] == "cuda" else []
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        *decode_arguments,
        "-i",
        str(Path(video_path).resolve()),
        *html_inputs,
        "-filter_complex_script",
        str(filter_script_path),


        "-map",
        "[final_v]",
        "-map",
        "[final_a]",
        *_encoder_arguments(selected_encoder),
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-progress",


        "pipe:1",
        "-nostats",
        str(partial_path),
    ]


    if progress_callback:
        encoder_label = (
            f"GPU encoder selected: {selected_encoder}"
            if encoder_info["class"] == "gpu"
            else f"CPU fallback: {selected_encoder} ({encoder_info['fallbackReason']})"
        )
        progress_callback(0.0, f"{encoder_label} / decode={decode_info['mode']}")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    log_tail = []
    cancelled = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                log_tail.append(line)
                log_tail = log_tail[-40:]
            if line.startswith("out_time_us="):
                try:
                    current_seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                fraction = min(1.0, current_seconds / max(expected_duration, 0.001))
                if progress_callback:
                    progress_callback(
                        fraction,
                        f"動画レンダリング {fraction * 100:.1f}% / {selected_encoder}",
                    )
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate_process(process)
                break
        return_code = process.wait()
    finally:
        if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
            cancelled = True
            _terminate_process(process)
        if process.stdout is not None:
            process.stdout.close()

    if cancelled or (cancel_event is not None and cancel_event.is_set()):
        raise PipelineCancelled("動画レンダリングを停止しました。再開できます。")
    if return_code != 0:
        details = "\n".join(log_tail[-12:])
        raise RuntimeError(f"FFmpegが終了コード{return_code}で失敗しました。\n{details}")
    if not partial_path.exists() or partial_path.stat().st_size == 0:
        raise RuntimeError("FFmpegは成功終了しましたが、動画ファイルが生成されませんでした。")

    os.replace(partial_path, output_path)
    if progress_callback:
        progress_callback(1.0, f"動画生成完了 / {selected_encoder}")
    return {
        "output_path": str(output_path),
        "encoder": selected_encoder,
        "encoder_class": encoder_info["class"],
        "encoder_fallback": encoder_info["fallback"],
        "encoder_fallback_reason": encoder_info["fallbackReason"],
        "decode_mode": decode_info["mode"],
        "decode_fallback": decode_info["fallback"],
        "decode_fallback_reason": decode_info["fallbackReason"],
        "filters": "software:select/scale/pad/fps/overlay/subtitles",
        "duration": expected_duration,
        "intervals": len(edl),
        "filter_script_path": str(filter_script_path),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 4:
        create_clip(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print("Usage: python clip.py <input_video.mp4> <edl.json> <captions.ass> <output_video.mp4>")
