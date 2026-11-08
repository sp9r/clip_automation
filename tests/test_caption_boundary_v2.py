import unittest

from caption_boundary_v2 import assign_word_ids, optimize_boundaries


class CaptionBoundaryV2Tests(unittest.TestCase):
    def test_assigns_stable_word_ids_and_preserves_source(self):
        words = [{"word": "大学", "start": 0, "end": 1}, {"word": "です", "start": 1, "end": 1.4}]
        tagged = assign_word_ids(words)
        self.assertEqual([item["word_id"] for item in tagged], [0, 1])
        self.assertEqual("".join(item["word"] for item in tagged), "大学です")

    def test_does_not_split_particle_or_auxiliary_pairs(self):
        words = [
            {"word": "これ", "start": 0, "end": .4},
            {"word": "を", "start": .4, "end": .5},
            {"word": "確認", "start": .9, "end": 1.3},
            {"word": "して", "start": 1.3, "end": 1.6},
            {"word": "いる", "start": 1.6, "end": 1.9},
            {"word": "ところです。", "start": 1.9, "end": 2.5},
        ]
        breaks = optimize_boundaries(words, max_chars=9)
        self.assertNotIn(1, breaks)
        self.assertNotIn(3, breaks)
        self.assertEqual(breaks[-1], len(words) - 1)

    def test_keeps_japanese_inflections_and_compound_words_together(self):
        words = [
            {"word": "俺", "start": 0, "end": .2},
            {"word": "もう", "start": .2, "end": .4},
            {"word": "さ", "start": .4, "end": .6},
            {"word": "全然", "start": .6, "end": .9},
            {"word": "100", "start": .9, "end": 1.1},
            {"word": "超", "start": 1.1, "end": 1.3},
            {"word": "えて", "start": 1.3, "end": 1.5},
            {"word": "る", "start": 1.5, "end": 1.6},
            {"word": "か", "start": 1.6, "end": 1.7},
            {"word": "と思", "start": 1.7, "end": 1.9},
            {"word": "って", "start": 1.9, "end": 2.1},
            {"word": "さ", "start": 2.1, "end": 2.3},
            {"word": "フォロ", "start": 2.3, "end": 2.6},
            {"word": "バ", "start": 2.6, "end": 2.8},
            {"word": "した", "start": 2.8, "end": 3.1},
        ]
        breaks = optimize_boundaries(words, max_chars=18)
        self.assertNotIn(5, breaks)  # 超｜えて
        self.assertNotIn(12, breaks)  # フォロ｜バ
        self.assertEqual(breaks[-1], len(words) - 1)

    def test_compressed_timestamps_keep_a_valid_complete_path(self):
        words = [
            {"word": "あ", "start": index * 0.001, "end": index * 0.001 + 0.001}
            for index in range(66)
        ]

        breaks = optimize_boundaries(words, max_chars=32, max_duration=5.0)

        self.assertGreater(len(breaks), 1)
        self.assertEqual(breaks[-1], len(words) - 1)
        start = 0
        for end in breaks:
            block = words[start : end + 1]
            self.assertLessEqual(len("".join(item["word"] for item in block)), 32)
            self.assertLessEqual(block[-1]["end"] - block[0]["start"], 5.0)
            start = end + 1

    def test_oversize_unit_becomes_its_own_caption(self):
        words = [
            {"word": "こんにちは", "start": 0.0, "end": 0.5},
            {"word": "あ" * 33, "start": 0.5, "end": 1.0},
            {"word": "さようなら", "start": 1.0, "end": 1.5},
        ]

        breaks = optimize_boundaries(words, max_chars=32, max_duration=5.0)

        self.assertEqual(breaks, [0, 1, 2])

    def test_oversize_duration_unit_becomes_its_own_caption(self):
        words = [
            {"word": "あ", "start": 0.0, "end": 0.5},
            {"word": "いい", "start": 0.5, "end": 9.0},
            {"word": "う", "start": 9.0, "end": 9.5},
        ]

        breaks = optimize_boundaries(words, max_chars=32, max_duration=5.0)

        self.assertEqual(breaks, [0, 1, 2])

    def test_length_of_excludes_hidden_characters(self):
        words = [
            {"word": "あいうえお、", "start": 0.0, "end": 1.0},
            {"word": "かきくけこ。", "start": 1.0, "end": 2.0},
        ]
        counted = lambda word: len(word["word"].rstrip("、。"))

        self.assertEqual(optimize_boundaries(words, max_chars=10, length_of=counted, target_chars=10)[-1], 1)
        self.assertEqual(len(optimize_boundaries(words, max_chars=10, length_of=counted, target_chars=10)), 1)


if __name__ == "__main__":
    unittest.main()
