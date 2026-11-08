"""1語(1単位)で上限を超える字幕と、句読点の表示設定のテスト。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import caption
from auto_caption_contract import AutoCaptionContractError, parse_request, parse_resegment_request
from caption import generate_subtitles
from caption_text import (
    classify_caption_text_status,
    is_display_normalization,
    normalize_caption_display_text,
)
from japanese_units import merge_bunsetsu_units
from spectra_caption_resegment import resegment

# 実際の実行(max_chars 12 / 1行 / 容量12)で13文字になり、全体をfailsafeへ落としていた単位。
UNITS = [
    "えっと、", "昨日アップデーあっただろ。", "今日は", "ありませんよ。", "1回みんな来ないでほしい。",
    "そうだね、", "でも", "専業YouTuberになる", "者や", "なんと", "なく、", "やってる?", "すごい!",
]


def _segments(texts=UNITS, step=0.9):
    words = []
    for index, text in enumerate(texts):
        words.append({"word": text, "start": round(index * step, 3), "end": round(index * step + step - 0.05, 3)})
    return [{"text": "".join(texts), "start": 0.0, "end": words[-1]["end"], "words": words}]


def _generate(segments, **kwargs):
    with tempfile.TemporaryDirectory() as directory:
        transcript = Path(directory) / "t.json"
        transcript.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        return generate_subtitles(str(transcript), str(Path(directory) / "o.srt"), **kwargs)


def _owned_text(result, segments):
    words = [word["word"] for segment in segments for word in segment["words"]]
    return ["".join(words[c["start_word_id"] : c["end_word_id"] + 1]) for c in result["captions"]]


class OversizeUnitTests(unittest.TestCase):
    def test_oversize_unit_does_not_force_failsafe_in_rule_mode(self):
        segments = _segments()
        result = _generate(segments, use_llm=False, max_chars=12, max_lines=1, line_capacity=12)

        self.assertEqual(result["method"], "heuristic")
        self.assertEqual([item["code"] for item in result["diagnostics"]], [])
        sources = _owned_text(result, segments)
        self.assertEqual("".join(sources), "".join(UNITS))
        for unit in ("昨日アップデーあっただろ。", "1回みんな来ないでほしい。", "専業YouTuberになる"):
            self.assertIn(unit, sources)  # 超える単位は単独の字幕になる
        for item, source in zip(result["captions"], sources):
            self.assertEqual(item["end_word_id"] >= item["start_word_id"], True)
            if source not in ("昨日アップデーあっただろ。", "1回みんな来ないでほしい。", "専業YouTuberになる"):
                self.assertLessEqual(len(item["text"]), 12, item)

    def test_oversize_unit_with_llm_break_proposal(self):
        segments = _segments()
        with patch("caption._ollama_generate", return_value=json.dumps({"break_after": list(range(len(UNITS)))})):
            result = _generate(segments, use_llm=True, max_chars=12, max_lines=1, line_capacity=12)

        self.assertEqual(result["method"], "llm")
        self.assertIn("昨日アップデーあっただろ。", _owned_text(result, segments))

    def test_oversize_unit_with_llm_caption_texts(self):
        segments = _segments()
        proposals = ["".join(UNITS[0:1]), UNITS[1], UNITS[2] + UNITS[3], UNITS[4], UNITS[5] + UNITS[6], UNITS[7], UNITS[8] + UNITS[9] + UNITS[10], UNITS[11] + UNITS[12]]
        with patch("caption._ollama_generate", return_value=json.dumps({"captions": proposals}, ensure_ascii=False)):
            result = _generate(segments, use_llm=True, max_chars=12, max_lines=1, line_capacity=12)

        self.assertEqual(result["method"], "llm")
        sources = _owned_text(result, segments)
        self.assertIn(UNITS[1], sources)
        self.assertIn(UNITS[7], sources)

    def test_llm_caption_splitting_inside_an_oversize_unit_never_splits_the_word(self):
        segments = _segments()
        proposals = [UNITS[0], "昨日アップデー", "あっただろ。", *UNITS[2:]]
        with patch("caption._ollama_generate", return_value=json.dumps({"captions": proposals}, ensure_ascii=False)):
            result = _generate(segments, use_llm=True, max_chars=12, max_lines=1, line_capacity=12)

        # 単語の途中の境界は採用されず、既存方針どおり安全方式で全文を保つ。
        self.assertEqual(result["method"], "llm+failsafe")
        self.assertEqual("".join(_owned_text(result, segments)), "".join(UNITS))

    def test_optimize_boundaries_never_raises_for_oversize_unit(self):
        words = [{"word": "あ" * 40, "start": 0.0, "end": 0.3}]
        self.assertEqual(caption.optimize_boundaries(words, max_chars=12), [0])

    def test_units_are_split_at_morpheme_boundaries_when_too_long(self):
        words = [{"word": char, "start": index * 0.1, "end": index * 0.1 + 0.1} for index, char in enumerate("昨日アップデートがありました。")]
        merged = merge_bunsetsu_units([{"words": words}], 8, True)[0]["words"]
        self.assertEqual("".join(word["word"] for word in merged), "昨日アップデートがありました。")
        for word in merged:
            self.assertLessEqual(len(word["word"].replace("。", "")), 8, word)


class PunctuationTests(unittest.TestCase):
    def test_hide_mode_removes_only_japanese_stops_and_ascii_marks(self):
        self.assertEqual(normalize_caption_display_text("そうだね、でも。", "hide"), "そうだねでも")
        self.assertEqual(normalize_caption_display_text("すごい!やってる?えっ…", "hide"), "すごい!やってる?えっ…")
        self.assertEqual(normalize_caption_display_text("ok, fine.", "hide"), "ok fine")
        self.assertEqual(normalize_caption_display_text("3.14と1,000", "hide"), "3.14と1,000")
        self.assertEqual(normalize_caption_display_text("そうだね、", "show"), "そうだね、")

    def test_status_treats_both_normalizations_as_non_manual(self):
        self.assertEqual(classify_caption_text_status("そうだね、でも。", "そうだねでも"), "display_normalized")
        self.assertEqual(classify_caption_text_status("すごい!?です。", "すごいです"), "display_normalized")
        self.assertEqual(classify_caption_text_status("そうだね、", "そうだね、"), "as_transcribed")
        self.assertEqual(classify_caption_text_status("そうだね、", "そうだよ"), "manually_corrected")
        self.assertTrue(is_display_normalization("あ、い。", "あい"))

    def test_generate_hides_punctuation_by_default_and_keeps_question_marks(self):
        segments = _segments()
        result = _generate(segments, use_llm=False, max_chars=32, max_lines=2)
        text = "\n".join(item["text"] for item in result["captions"])

        self.assertNotIn("、", text)
        self.assertNotIn("。", text)
        self.assertIn("?", text)
        self.assertIn("!", text)
        self.assertTrue(result["caption_settings"]["hide_punctuation"])
        for item, source in zip(result["captions"], _owned_text(result, segments)):
            expected = "display_normalized" if item["text"] != source else "as_transcribed"
            self.assertEqual(item["text_status"], expected)

    def test_generate_can_show_punctuation(self):
        segments = _segments()
        result = _generate(segments, use_llm=False, max_chars=32, max_lines=2, hide_punctuation=False)
        text = "".join(item["text"] for item in result["captions"])

        self.assertEqual(text, "".join(UNITS))
        self.assertTrue(all(item["text_status"] == "as_transcribed" for item in result["captions"]))

    def test_hidden_punctuation_does_not_count_toward_the_limit(self):
        texts = ["あいうえお、", "かきくけこ。"]
        hidden = _generate(_segments(texts), use_llm=False, max_chars=10, max_lines=2)
        shown = _generate(_segments(texts), use_llm=False, max_chars=10, max_lines=2, hide_punctuation=False)

        self.assertEqual([item["text"] for item in hidden["captions"]], ["あいうえおかきくけこ"])
        self.assertEqual(len(shown["captions"]), 2)

    def test_punctuation_still_acts_as_a_boundary_hint(self):
        texts = ["今日は", "晴れです。", "明日は", "雨です。"]
        result = _generate(_segments(texts, step=1.0), use_llm=False, max_chars=32, max_lines=2)

        self.assertEqual([item["text"] for item in result["captions"]], ["今日は晴れです", "明日は雨です"])


class ResegmentAndContractTests(unittest.TestCase):
    def _request(self, **extra):
        words = [
            {"id": f"w{index}", "text": text, "startUs": index * 700_000, "endUs": index * 700_000 + 650_000}
            for index, text in enumerate(UNITS)
        ]
        return {
            "version": "spectra.auto-caption.resegment.request.v1",
            "jobId": "rs-oversize",
            "language": "ja",
            "maxChars": 12,
            "maxLines": 1,
            "lineCapacity": 12,
            "segmentationMode": "rule",
            "words": words,
            **extra,
        }

    def test_resegment_keeps_working_with_oversize_unit_and_hidden_punctuation(self):
        result = resegment(self._request(hidePunctuation=True))

        self.assertEqual(result["method"], "heuristic")
        owned = [word_id for item in result["captions"] for word_id in item["sourceWordIds"]]
        self.assertEqual(owned, [f"w{index}" for index in range(len(UNITS))])
        text = "".join(item["text"] for item in result["captions"])
        self.assertNotIn("、", text)
        self.assertNotIn("。", text)
        self.assertIn("?", text)
        self.assertTrue(all(item["textStatus"] in {"display_normalized", "as_transcribed"} for item in result["captions"]))

    def test_resegment_can_show_punctuation_again(self):
        result = resegment(self._request(hidePunctuation=False))

        self.assertEqual("".join(item["text"] for item in result["captions"]), "".join(UNITS))
        self.assertTrue(all(item["textStatus"] == "as_transcribed" for item in result["captions"]))

    def test_contract_accepts_and_validates_hide_punctuation(self):
        self.assertIs(parse_resegment_request(self._request(hidePunctuation=False))["hidePunctuation"], False)
        with self.assertRaises(AutoCaptionContractError):
            parse_resegment_request(self._request(hidePunctuation="yes"))
        base = {
            "version": "spectra.auto-caption.request.v1", "jobId": "j1", "mediaPath": "x.mp4", "sourceStartUs": 0,
            "sourceEndUs": 1_000_000, "language": "ja", "maxChars": 12, "segmentationMode": "rule",
        }
        self.assertIs(parse_request({**base, "hidePunctuation": True})["hidePunctuation"], True)
        self.assertNotIn("hidePunctuation", parse_request(base))


if __name__ == "__main__":
    unittest.main()
