import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from silence_detection import (
    SilenceDetectionError,
    analyze_silences,
    build_clean_transcript,
    parse_silencedetect_output,
)


class SilenceDetectionTests(unittest.TestCase):
    def test_parser_returns_integer_microseconds_and_filters_short_silence(self):
        output = """
[silencedetect] silence_start: 0.100
[silencedetect] silence_end: 0.500 | silence_duration: 0.400
[silencedetect] silence_start: 1.000
[silencedetect] silence_end: 1.800 | silence_duration: 0.800
"""
        result = parse_silencedetect_output(output, duration_us=2_000_000, min_silence_us=600_000)
        self.assertEqual(result, [{"startUs": 1_000_000, "endUs": 1_800_000, "durationUs": 800_000}])
        self.assertTrue(all(isinstance(value, int) for item in result for value in item.values()))

    def test_clean_transcript_keeps_filler_words(self):
        transcript = [{"words": [
            {"word": "えっと", "start": 0.1, "end": 0.3},
            {"word": "今日は", "start": 1.0, "end": 1.4},
        ]}]
        clean = build_clean_transcript(transcript, [{"startUs": 0, "endUs": 2_000_000}])
        self.assertEqual("".join(word["word"] for word in clean[0]["words"]), "えっと今日は")

    def test_generated_audio_uses_signal_and_protects_raw_word(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "tone-silence-tone.wav"
            generated = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.8",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000:duration=1.0",
                    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=16000:duration=0.8",
                    "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(media),
                ],
                capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            transcript = root / "transcript.json"
            transcript.write_text(json.dumps([{"words": [
                {"word": "開始", "start": 0.2, "end": 0.4},
                # Deliberately place a raw word inside a signal silence. It must
                # be protected even though its text is a filler-like token.
                {"word": "えっと", "start": 1.2, "end": 1.3},
            ]}], ensure_ascii=False), encoding="utf-8")
            result = analyze_silences(media, transcript, profile="natural")
            self.assertTrue(result["silences"])
            self.assertGreater(result["removedDurationUs"], 0)
            self.assertEqual(result["settings"]["fillerRemoval"], False)
            self.assertEqual(result["diagnostics"][0]["code"], "AUDIO_SIGNAL_AUTHORITY")
            self.assertEqual(result["diagnostics"][2]["code"], "FILLER_REMOVAL_DISABLED")
            self.assertTrue(all(set(item) == {"level", "code", "message"} for item in result["diagnostics"]))
            self.assertGreaterEqual(len(result["silences"][0]["removedRanges"]), 2)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(str(media), serialized)
            self.assertTrue(all(isinstance(result["removedDurationUs"], int) for _ in [0]))

    def test_strong_profile_accepts_shorter_signal_silence(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "short-silence.wav"
            generated = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.3",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000:duration=0.5",
                    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=16000:duration=0.3",
                    "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(media),
                ],
                capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            result = analyze_silences(media, profile="strong")
            self.assertTrue(result["silences"])
            self.assertEqual(result["settings"]["minSilenceUs"], 400_000)
            self.assertEqual(result["settings"]["retainPauseUs"], 100_000)

    def test_youtube_profile_uses_a_short_editable_gap_without_changing_authority(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "youtube-silence.wav"
            generated = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.2",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000:duration=0.4",
                    "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=16000:duration=0.2",
                    "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(media),
                ], capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            result = analyze_silences(media, profile="youtube")
            self.assertTrue(result["silences"])
            self.assertEqual(result["settings"]["profile"], "youtube")
            self.assertEqual(result["settings"]["minSilenceUs"], 150_000)
            self.assertEqual(result["settings"]["retainPauseUs"], 0)
            self.assertFalse(result["settings"]["fillerRemoval"])

    def test_custom_minimum_silence_threshold_is_bounded_and_reported(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "custom-threshold.wav"
            generated = subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.2",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000:duration=0.2",
                "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=16000:duration=0.2",
                "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(media),
            ], capture_output=True, text=True, check=False, shell=False)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            result = analyze_silences(media, profile="youtube", min_silence_us=50_000)
            self.assertEqual(result["settings"]["minSilenceUs"], 50_000)
            self.assertEqual(result["settings"]["retainPauseUs"], 0)

    def test_zero_minimum_is_valid_without_passing_zero_duration_to_ffmpeg(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "zero-threshold.wav"
            generated = subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000:duration=0.2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.2",
                "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]", "-c:a", "pcm_s16le", str(media),
            ], capture_output=True, text=True, check=False, shell=False)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            result = analyze_silences(media, profile="youtube", min_silence_us=0)
            self.assertEqual(result["settings"]["minSilenceUs"], 0)
            self.assertTrue(result["silences"])

    def test_no_silence_is_a_normal_noop(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "continuous-tone.wav"
            generated = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1.0",
                    "-c:a", "pcm_s16le", str(media),
                ],
                capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            result = analyze_silences(media)
            self.assertEqual(result["silences"], [])
            self.assertEqual(result["removedDurationUs"], 0)
            self.assertEqual(result["settings"]["fillerRemoval"], False)

    def test_no_audio_and_ffmpeg_failure_have_explicit_codes(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg/ffprobe is required")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_only = root / "video-only.mp4"
            generated_video = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.5",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video_only),
                ],
                capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated_video.returncode, 0, generated_video.stderr)
            with self.assertRaises(SilenceDetectionError) as no_audio:
                analyze_silences(video_only)
            self.assertEqual(no_audio.exception.code, "NO_AUDIO_STREAM")

            audio = root / "audio.wav"
            generated_audio = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=0.5",
                    "-c:a", "pcm_s16le", str(audio),
                ],
                capture_output=True, text=True, check=False, shell=False,
            )
            self.assertEqual(generated_audio.returncode, 0, generated_audio.stderr)

            def nonzero_runner(command, **_kwargs):
                return subprocess.CompletedProcess(command, 1, "", "Stream map '0:a:0' matches no streams.")

            with self.assertRaises(SilenceDetectionError) as nonzero:
                analyze_silences(audio, runner=nonzero_runner)
            self.assertEqual(nonzero.exception.code, "NO_AUDIO_STREAM")

            def malformed_runner(command, **_kwargs):
                return subprocess.CompletedProcess(command, 0, "", "silence_start: not-a-number")

            with self.assertRaises(SilenceDetectionError) as malformed:
                analyze_silences(audio, runner=malformed_runner)
            self.assertEqual(malformed.exception.code, "FFMPEG_OUTPUT_MALFORMED")


if __name__ == "__main__":
    unittest.main()
