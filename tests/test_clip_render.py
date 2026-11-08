import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip import create_clip, hardware_decode_diagnostic, resolve_video_encoder_diagnostic
from pipeline import video_is_valid


class ClipRenderTests(unittest.TestCase):
    def test_single_decode_select_render(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=size=320x240:rate=10:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source),
                ],
                check=True,
            )
            edl = temp / "edl.json"
            edl.write_text(
                json.dumps([{"start": 0, "end": 1}, {"start": 2, "end": 3}]),
                encoding="utf-8",
            )
            output = temp / "output.mp4"
            progress = []
            result = create_clip(
                str(source),
                str(edl),
                None,
                str(output),
                output_width=320,
                output_height=240,
                output_fps=10,
                encoder="libx264",
                progress_callback=lambda value, message: progress.append(value),
            )

            self.assertTrue(video_is_valid(output))
            self.assertEqual(result["intervals"], 2)
            self.assertEqual(result["encoder"], "libx264")
            self.assertIn(result["decode_mode"], {"cuda", "software"})
            self.assertEqual(result["filters"], "software:select/scale/pad/fps/overlay/subtitles")
            self.assertEqual(progress[-1], 1.0)
            filter_text = Path(result["filter_script_path"]).read_text(encoding="utf-8")
            self.assertGreaterEqual(len(filter_text.splitlines()), 2)
            self.assertIn("aselect", filter_text)

    def test_auto_encoder_reports_gpu_selection(self):
        with patch("clip.nvenc_diagnostic", return_value={"available": True, "reason": "none"}):
            self.assertEqual(
                resolve_video_encoder_diagnostic("auto"),
                {"selected": "h264_nvenc", "class": "gpu", "fallback": False, "fallbackReason": "none"},
            )

    def test_auto_encoder_reports_cpu_fallback_reason(self):
        with patch("clip.nvenc_diagnostic", return_value={"available": False, "reason": "cuda_runtime_unavailable"}):
            self.assertEqual(
                resolve_video_encoder_diagnostic("auto"),
                {"selected": "libx264", "class": "cpu", "fallback": True, "fallbackReason": "cuda_runtime_unavailable"},
            )

    def test_explicit_gpu_encoder_does_not_silently_fallback(self):
        with patch("clip.nvenc_diagnostic", return_value={"available": False, "reason": "nvenc_probe_timeout"}):
            with self.assertRaisesRegex(RuntimeError, "nvenc_probe_timeout"):
                resolve_video_encoder_diagnostic("h264_nvenc")

    def test_hardware_decode_probe_reports_cuda_success(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("clip.subprocess.run", return_value=completed) as run:
            result = hardware_decode_diagnostic("C:/media/source.mp4")
        self.assertEqual(result, {"mode": "cuda", "fallback": False, "fallbackReason": "none"})
        self.assertIn("-hwaccel", run.call_args.args[0])

    def test_hardware_decode_probe_reports_software_fallback(self):
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="No NVDEC device")
        with patch("clip.subprocess.run", return_value=completed):
            result = hardware_decode_diagnostic("C:/media/source.mp4")
        self.assertEqual(result["mode"], "software")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallbackReason"], "cuda_decode_unavailable")



if __name__ == "__main__":
    unittest.main()
