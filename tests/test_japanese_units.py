import unittest
from unittest.mock import patch

import japanese_units
from japanese_units import merge_bunsetsu_units


def segment_from_chars(chunks: list[str]) -> list[dict]:
    words = [
        {"word": text, "start": index * 0.1, "end": index * 0.1 + 0.08, "probability": 0.5 + index * 0.01}
        for index, text in enumerate(chunks)
    ]
    return [{"start": 0.0, "end": words[-1]["end"], "text": "".join(chunks), "words": words}]


def units(chunks: list[str]) -> list[str]:
    return [word["word"] for word in merge_bunsetsu_units(segment_from_chars(chunks))[0]["words"]]


class JapaneseUnitsTests(unittest.TestCase):
    def test_real_examples_become_phrases(self):
        chars = list("結構会員証を見せびらかして終わりなんですけど")
        self.assertEqual(units(chars), ["結構", "会員証を", "見せびらかして", "終わりなんですけど"])
        result = units(list("オフ会みたいなのが開催されててそこに行って友達を作るのが"))
        self.assertIn("開催されてて", result)
        self.assertIn("オフ会みたいなのが", result)
        self.assertIn("行って", result)
        self.assertIn("友達を", result)

    def test_text_is_preserved_and_tokens_never_split(self):
        chunks = ["結", "構", "会", "員", "証", "を", "見", "せ", "び", "ら", "か", "して", "終", "わ", "り", "なんです", "けど"]
        merged = merge_bunsetsu_units(segment_from_chars(chunks))[0]["words"]
        self.assertEqual("".join(w["word"] for w in merged), "".join(chunks))
        # 各単位は元トークンの連続した連結になっている
        position = 0
        for unit in merged:
            consumed = ""
            while len(consumed) < len(unit["word"]):
                consumed += chunks[position]
                position += 1
            self.assertEqual(consumed, unit["word"])

    def test_timing_is_monotonic_and_probability_is_min(self):
        original = segment_from_chars(list("毎日のように会いましょう"))
        merged = merge_bunsetsu_units(original)[0]["words"]
        for previous, current in zip(merged, merged[1:]):
            self.assertLessEqual(previous["start"], current["start"])
            self.assertLessEqual(previous["start"], previous["end"])
        self.assertEqual(merged[0]["start"], original[0]["words"][0]["start"])
        self.assertEqual(merged[-1]["end"], original[0]["words"][-1]["end"])
        self.assertEqual(merged[0]["probability"], min(w["probability"] for w in original[0]["words"][: len(merged[0]["word"])]))

    def test_input_is_not_mutated(self):
        original = segment_from_chars(list("毎日のように"))
        snapshot = [dict(w) for w in original[0]["words"]]
        merge_bunsetsu_units(original)
        self.assertEqual(original[0]["words"], snapshot)

    def test_whitespace_and_english_are_kept_in_text(self):
        chunks = [" What", " are", " you", " good", " at", "?", "です"]
        merged = merge_bunsetsu_units(segment_from_chars(chunks))[0]["words"]
        self.assertEqual("".join(w["word"] for w in merged), "".join(chunks))

    def test_fallback_without_analyzer(self):
        original = segment_from_chars(list("毎日のように"))
        with patch.object(japanese_units, "_get_tagger", return_value=None):
            self.assertEqual(merge_bunsetsu_units(original), original)

    def test_long_phrase_is_split_at_morpheme_boundary(self):
        text = "ビッグサイトとかでやってるんですけどまあやっぱり面白いんですよねこれ"
        merged = units(list(text))
        self.assertEqual("".join(merged), text)
        self.assertTrue(all(len(unit) <= japanese_units.MAX_UNIT_CHARS + 6 for unit in merged))

    def test_segments_without_words_pass_through(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "あ", "words": []}]
        self.assertEqual(merge_bunsetsu_units(segments), segments)


if __name__ == "__main__":
    unittest.main()
