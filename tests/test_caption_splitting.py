import unittest

from caption import _heuristic_break_indices


class CaptionSplittingTests(unittest.TestCase):
    def test_heuristic_break_indices_with_pause_and_topic_shift(self):
        words = [
            {"word": "こんにちは", "start": 0.0, "end": 1.0},
            {"word": "今日は", "start": 1.0, "end": 1.5},
            {"word": "晴れですね", "start": 1.5, "end": 2.0},
            {"word": "ところで", "start": 2.6, "end": 3.0},
            {"word": "明日の", "start": 3.0, "end": 3.5},
            {"word": "予定ですが", "start": 3.5, "end": 4.0},
            {"word": "雨が降るそうです", "start": 4.0, "end": 5.0},
        ]

        breaks = _heuristic_break_indices(words, max_chars=28)
        self.assertIn(2, breaks)

if __name__ == "__main__":
    unittest.main()
