import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caption import generate_subtitles


def _sample_segments():
    texts = [
        "ちょっと",
        "今日は",
        "新聞",
        "の",
        "ニュース",
        "ネタ",
        "でも",
        "拾",
        "って",
        "みよう",
        "と思いました。",
    ]
    words = []
    for index, text in enumerate(texts):
        words.append(
            {
                "word": text,
                "start": index * 0.4,
                "end": (index + 1) * 0.4,
            }
        )
    return [{"start": 0.0, "end": 4.4, "text": "".join(texts), "words": words}]


class CaptionTests(unittest.TestCase):
    def _paths(self, temp_dir):
        transcript = Path(temp_dir) / "clean.json"
        srt = Path(temp_dir) / "captions.srt"
        transcript.write_text(
            json.dumps(_sample_segments(), ensure_ascii=False), encoding="utf-8"
        )
        return transcript, srt

    @patch("caption._ollama_generate")
    def test_llm_selects_boundaries_without_rewriting_text(self, ollama_generate):
        ollama_generate.return_value = (
            '{"captions":["ちょっと今日は新聞のニュースネタでも",'
            '"拾ってみようと思いました。"]}'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._paths(temp_dir)
            result = generate_subtitles(str(transcript), str(srt))
            output = srt.read_text(encoding="utf-8")

        self.assertEqual(result["blocks"], 2)
        self.assertEqual(result["method"], "llm")
        self.assertEqual(len(result["captions"]), 2)
        self.assertIn("ちょっと今日は新聞のニュースネタでも", output)
        self.assertIn("拾ってみようと思いました", output)
        ollama_generate.assert_called_once()

    @patch("caption._ollama_generate")
    def test_invalid_llm_boundaries_use_heuristic_fallback(self, ollama_generate):
        ollama_generate.return_value = '{"captions":["原文を書き換えた字幕"]}'
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._paths(temp_dir)
            result = generate_subtitles(str(transcript), str(srt))
            output_exists = srt.exists()

        self.assertEqual(result["method"], "llm+fallback")
        self.assertTrue(output_exists)
        self.assertEqual(ollama_generate.call_count, 2)

    @patch("caption._ollama_generate")
    def test_llm_word_indices_are_accepted_without_text_rewriting(self, ollama_generate):
        ollama_generate.return_value = '{"break_after":[6,10]}'
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._paths(temp_dir)
            result = generate_subtitles(str(transcript), str(srt))
            output = srt.read_text(encoding="utf-8")

        self.assertEqual(result["method"], "llm")
        self.assertEqual(result["blocks"], 2)
        self.assertIn("ちょっと今日は新聞のニュースネタでも", output)
        self.assertIn("拾ってみようと思いました", output)

    @patch("caption._split_obvious_topic_boundaries", return_value=["ちょっと"])
    @patch("caption._ollama_generate")
    def test_llm_postprocess_integrity_failure_uses_explicit_rule_fallback(
        self, ollama_generate, split_obvious_topic_boundaries
    ):
        ollama_generate.return_value = (
            '{"captions":["ちょっと今日は新聞のニュースネタでも",'
            '"拾ってみようと思いました。"]}'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._paths(temp_dir)
            result = generate_subtitles(str(transcript), str(srt))
            output_exists = srt.exists()

        self.assertEqual(result["method"], "llm+failsafe")
        self.assertTrue(output_exists)
        self.assertGreater(result["blocks"], 0)
        split_obvious_topic_boundaries.assert_called_once()

    @patch("caption._clean_unnatural_subtitles", side_effect=ValueError("fallback postprocess failure"))
    @patch("caption._request_llm_subtitles", side_effect=ValueError("invalid LLM boundaries"))
    def test_llm_fallback_postprocess_failure_does_not_block_job(
        self, request_llm_subtitles, clean_unnatural_subtitles
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._paths(temp_dir)
            result = generate_subtitles(str(transcript), str(srt))
            output_exists = srt.exists()

        self.assertEqual(result["method"], "llm+failsafe")
        self.assertGreater(result["blocks"], 0)
        self.assertTrue(output_exists)
        request_llm_subtitles.assert_called_once()
        clean_unnatural_subtitles.assert_called_once()
        self.assertIn("CAPTION_FAILSAFE_FALLBACK", {item["code"] for item in result["diagnostics"]})

    @patch("caption.align_caption_word_ownership")
    @patch("caption._ollama_generate")
    def test_ownership_failure_rebuilds_from_immutable_words(
        self, ollama_generate, align_ownership
    ):
        ollama_generate.return_value = '{"break_after":[6,10]}'
        real_align = __import__("caption_text").align_caption_word_ownership
        align_calls = {"count": 0}

        def fake_align(captions, words, mode="legacy"):
            align_calls["count"] += 1
            if align_calls["count"] == 1:
                raise ValueError("caption ownership mismatch")
            return real_align(captions, words, mode)

        align_ownership.side_effect = fake_align
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._paths(temp_dir)
            result = generate_subtitles(str(transcript), str(srt))

        self.assertEqual(result["method"], "llm+failsafe")
        self.assertEqual("".join(item["text"] for item in result["captions"]), "".join(
            word["word"] for word in _sample_segments()[0]["words"]
        ))
        self.assertEqual(align_ownership.call_count, 1)

    def test_heuristic_generation_does_not_publish_oversized_fallback(self):
        words = [
            {"word": "あ", "start": index * 0.001, "end": index * 0.001 + 0.001}
            for index in range(66)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "clean.json"
            srt = Path(temp_dir) / "captions.srt"
            transcript.write_text(
                json.dumps(
                    [{"start": 0.0, "end": 0.1, "text": "あ" * 66, "words": words}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = generate_subtitles(
                str(transcript), str(srt), use_llm=False, max_chars=32
            )

        self.assertGreater(result["blocks"], 1)
        self.assertTrue(all(len(item["text"]) <= 32 for item in result["captions"]))
        self.assertEqual("".join(item["text"] for item in result["captions"]), "あ" * 66)

    def _word_transcript(self, texts):
        words = [
            {"word": text, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, text in enumerate(texts)
        ]
        return [{"start": 0.0, "end": len(words) * 0.1, "text": "".join(texts), "words": words}]

    def _write_transcript(self, temp_dir, texts):
        transcript = Path(temp_dir) / "failsafe.json"
        srt = Path(temp_dir) / "failsafe.srt"
        transcript.write_text(json.dumps(self._word_transcript(texts), ensure_ascii=False), encoding="utf-8")
        return transcript, srt

    def test_malformed_llm_response_falls_back_with_explicit_diagnostics(self):
        with patch("caption._ollama_generate", return_value="not json"), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, ["字幕", "を", "生成", "します"])
            result = generate_subtitles(str(transcript), str(srt), max_chars=8)

        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn("CAPTION_LLM_FALLBACK", codes)
        self.assertIn("CAPTION_HEURISTIC_FALLBACK", codes)
        self.assertEqual("".join(item["text"] for item in result["captions"]), "字幕を生成します")

    def test_llm_timeout_is_not_a_caption_job_failure(self):
        with patch("caption._ollama_generate", side_effect=TimeoutError("timeout")), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, ["音声", "を", "字幕", "に", "します"])
            result = generate_subtitles(str(transcript), str(srt), max_chars=8)

        self.assertGreater(result["blocks"], 0)
        self.assertIn("CAPTION_LLM_FALLBACK", {item["code"] for item in result["diagnostics"]})

    def test_invalid_break_after_uses_safe_quality_fallback(self):
        with patch("caption._ollama_generate", return_value='{"break_after":[999]}'), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, ["これは", "安全", "な", "字幕"])
            result = generate_subtitles(str(transcript), str(srt), max_chars=8)

        self.assertGreater(result["blocks"], 0)
        self.assertIn("CAPTION_LLM_FALLBACK", {item["code"] for item in result["diagnostics"]})

    def test_heuristic_optimizer_failure_uses_deterministic_failsafe(self):
        texts = ["長い", "字幕", "でも", "失わ", "ない"]
        with patch("caption._heuristic_break_indices", side_effect=ValueError("optimizer failed")), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, texts)
            result = generate_subtitles(str(transcript), str(srt), use_llm=False, max_chars=8)

        self.assertEqual(result["method"], "failsafe")
        self.assertIn("CAPTION_HEURISTIC_FALLBACK", {item["code"] for item in result["diagnostics"]})
        self.assertIn("CAPTION_FAILSAFE_FALLBACK", {item["code"] for item in result["diagnostics"]})
        self.assertEqual("".join(item["text"] for item in result["captions"]), "".join(texts))

    def test_heuristic_validation_failure_uses_deterministic_failsafe(self):
        with patch("caption._validate_break_indices", side_effect=ValueError("validation failed")), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, ["検証", "失敗", "でも", "継続"])
            result = generate_subtitles(str(transcript), str(srt), use_llm=False, max_chars=8)

        self.assertEqual(result["method"], "failsafe")
        self.assertEqual("".join(item["text"] for item in result["captions"]), "検証失敗でも継続")

    def test_postprocess_failure_falls_back_to_pre_quality_failsafe(self):
        with patch("caption._clean_unnatural_subtitles", side_effect=RuntimeError("postprocess failed")), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, ["後処理", "が", "失敗", "しても"])
            result = generate_subtitles(str(transcript), str(srt), use_llm=False, max_chars=8)

        codes = {item["code"] for item in result["diagnostics"]}
        self.assertEqual(result["method"], "failsafe")
        self.assertIn("CAPTION_FAILSAFE_FALLBACK", codes)
        self.assertEqual("".join(item["text"] for item in result["captions"]), "後処理が失敗しても")

    def test_failsafe_keeps_a_single_very_long_word(self):
        text = "非常に長い単一のWhisperwordです"
        with patch("caption._heuristic_break_indices", side_effect=ValueError("heuristic failed")), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, [text])
            result = generate_subtitles(str(transcript), str(srt), use_llm=False, max_chars=8)

        self.assertEqual(result["captions"][0]["text"], text)
        self.assertEqual(result["blocks"], 1)

    def test_failsafe_preserves_punctuation_and_monotonic_timing(self):
        texts = ["こんにちは。", "本当？", "すごい！"]
        with patch("caption._heuristic_break_indices", side_effect=ValueError("heuristic failed")), tempfile.TemporaryDirectory() as temp_dir:
            transcript, srt = self._write_transcript(temp_dir, texts)
            result = generate_subtitles(str(transcript), str(srt), use_llm=False, max_chars=8)

        self.assertEqual("".join(item["text"] for item in result["captions"]), "".join(texts))
        self.assertAlmostEqual(result["captions"][-1]["end"], 0.3)
        for previous, current in zip(result["captions"], result["captions"][1:]):
            self.assertLessEqual(previous["end"], current["start"] + 1e-9)

    def test_failsafe_covers_more_than_500_words_at_small_and_normal_limits(self):
        texts = ["あ"] * 501
        for max_chars in (8, 32):
            with self.subTest(max_chars=max_chars), patch("caption._heuristic_break_indices", side_effect=ValueError("heuristic failed")), tempfile.TemporaryDirectory() as temp_dir:
                transcript, srt = self._write_transcript(temp_dir, texts)
                result = generate_subtitles(str(transcript), str(srt), use_llm=False, max_chars=max_chars)

            self.assertEqual("".join(item["text"] for item in result["captions"]), "あ" * 501)
            self.assertEqual(result["captions"][-1]["end"], 50.1)
            self.assertTrue(all(item["text"] for item in result["captions"]))


if __name__ == "__main__":
    unittest.main()
