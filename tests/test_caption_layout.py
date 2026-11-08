import unittest

from caption import _enforce_line_layout
from caption_layout import count_lines, fits, split_to_fit, text_width
from auto_caption_contract import AutoCaptionContractError, parse_request


class CaptionLayoutTests(unittest.TestCase):
    def test_width_model_matches_editor(self):
        self.assertAlmostEqual(text_width("あA b"), 1 + 0.58 + 0.45 + 0.58)

    def test_count_lines_keeps_units_whole(self):
        units = ["今日は", "天気が", "良いので", "散歩に", "行きます"]
        self.assertEqual(count_lines(units, 12), 2)
        self.assertEqual(count_lines(["あ" * 5, "い" * 5, "う" * 5], 10), 2)
        # 1単位が1行より広い場合はその単位内で折る
        self.assertEqual(count_lines(["あ" * 25], 10), 3)

    def test_split_to_fit_minimizes_blocks_and_respects_limit(self):
        units = ["今日は", "天気が", "良いので", "散歩に", "行きます", "そして", "帰りに", "買い物を", "します"]
        ends = split_to_fit(units, 8, 2)
        cursor = 0
        for end in ends:
            self.assertTrue(fits(units[cursor : end + 1], 8, 2))
            cursor = end + 1
        self.assertEqual(cursor, len(units))
        self.assertEqual(len(ends), 3)

    def test_single_long_unit_is_not_split(self):
        self.assertEqual(split_to_fit(["あ" * 40], 10, 2), [0])

    def test_enforce_line_layout_splits_by_word_ids(self):
        words = [
            {"word": text, "start": index * 0.5, "end": index * 0.5 + 0.5}
            for index, text in enumerate(["今日は", "天気が", "良いので", "散歩に", "行きます", "そして", "帰りに", "買い物を", "します"])
        ]
        subtitle = {
            "start": 0.0, "end": 4.5, "text": "".join(w["word"] for w in words),
            "start_word_id": 0, "end_word_id": len(words) - 1,
        }
        result, count = _enforce_line_layout([subtitle], words, 8, 2)
        self.assertEqual(count, 2)
        self.assertEqual("".join(item["text"] for item in result), subtitle["text"])
        self.assertAlmostEqual(result[0]["start"], 0.0)
        self.assertAlmostEqual(result[-1]["end"], 4.5)

    def test_request_contract_accepts_line_capacity(self):
        base = {
            "version": "spectra.auto-caption.request.v1", "jobId": "j1", "mediaPath": "x.mp4",
            "sourceStartUs": 0, "sourceEndUs": 10, "language": "ja", "maxChars": 24, "segmentationMode": "rule",
        }
        parsed = parse_request({**base, "maxLines": 2, "lineCapacity": 12})
        self.assertEqual(parsed["lineCapacity"], 12)
        self.assertNotIn("lineCapacity", parse_request(base))
        with self.assertRaises(AutoCaptionContractError):
            parse_request({**base, "lineCapacity": 0})


if __name__ == "__main__":
    unittest.main()
