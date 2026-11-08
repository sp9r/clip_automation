import json
import unittest

from caption_reading import reading_similarity
from caption_text import writer_text_status
from caption_writer import is_filler_deletion, write_captions


def _words(texts):
    return [{"word": text, "start": index * 0.5, "end": index * 0.5 + 0.4} for index, text in enumerate(texts)]


def _rule(batch):
    return [(index, index) for index in range(len(batch))]


class CaptionWriterTests(unittest.TestCase):
    def test_filler_deletion_is_strict(self):
        self.assertTrue(is_filler_deletion(["え、", "今日は", "えーと、", "フォートナイトを"], "今日はフォートナイトを", allow_punctuation=True))
        self.assertTrue(is_filler_deletion(["え、", "なんだ", "これ。"], "えなんだこれ", allow_punctuation=True))
        self.assertFalse(is_filler_deletion(["ありがとう"], "りがとう", allow_punctuation=True))
        self.assertFalse(is_filler_deletion(["しましょう"], "ししょう", allow_punctuation=True))
        self.assertFalse(is_filler_deletion(["これは", "すごい"], "これはすごいね", allow_punctuation=True))
        self.assertFalse(is_filler_deletion(["今日は", "晴れ。"], "今日は晴れ", allow_punctuation=False))

    def test_valid_answer_is_used_as_written(self):
        words = _words(["え、", "今日は", "えーと、", "フォートナイトを", "やって", "いきます。"])
        answer = json.dumps({"c": [[0, 3, "今日はフォートナイトを"], [4, 5, "やっていきます"]]}, ensure_ascii=False)
        captions, fallback = write_captions(words, generate=lambda prompt, timeout: answer, max_chars=12, max_lines=1, line_capacity=12, hide_punctuation=True, fallback=_rule)
        self.assertEqual(fallback, 0)
        self.assertEqual([c["text"] for c in captions], ["今日はフォートナイトを", "やっていきます"])
        self.assertEqual([(c["start_word_id"], c["end_word_id"]) for c in captions], [(0, 3), (4, 5)])

    def test_a_rewritten_caption_keeps_its_source_words(self):
        words = _words(["今日は", "晴れ。"])
        prompts = []

        def generate(prompt, timeout):
            prompts.append(prompt)
            return json.dumps({"c": [[0, 1, "今日は雨"]]}, ensure_ascii=False)

        captions, fallback = write_captions(words, generate=generate, max_chars=12, max_lines=1, line_capacity=12, hide_punctuation=True, fallback=_rule, style="clean")
        self.assertEqual((fallback, len(prompts)), (0, 1))
        self.assertEqual([c["text"] for c in captions], ["今日は晴れ"])

    def test_many_rewrites_get_feedback_once_more(self):
        words = _words(["一", "二", "三", "四"])
        prompts = []

        def generate(prompt, timeout):
            prompts.append(prompt)
            return json.dumps({"c": [[0, 0, "壱"], [1, 1, "弐"], [2, 2, "参"], [3, 3, "四"]]}, ensure_ascii=False)

        captions, fallback = write_captions(words, generate=generate, max_chars=12, max_lines=1, line_capacity=12, hide_punctuation=True, fallback=_rule, style="clean")
        self.assertEqual(len(prompts), 2)
        self.assertIn("前回の答えの問題点", prompts[1])
        self.assertEqual([c["text"] for c in captions], ["一", "二", "三", "四"])

    def test_filler_only_range_is_owned_by_next_caption(self):
        words = _words(["あー、", "うーん、", "行こう。"])
        answer = json.dumps({"c": [[0, 1, ""], [2, 2, "行こう"]]}, ensure_ascii=False)
        captions, _ = write_captions(words, generate=lambda prompt, timeout: answer, max_chars=12, max_lines=1, line_capacity=12, hide_punctuation=True, fallback=_rule)
        self.assertEqual(len(captions), 1)
        self.assertEqual((captions[0]["start_word_id"], captions[0]["end_word_id"]), (0, 2))
        self.assertEqual(captions[0]["start"], words[0]["start"])

    def test_too_long_caption_is_split_keeping_the_written_text(self):
        words = _words(["これは", "えーと、", "とても", "長い", "字幕に", "なります。"])
        answer = json.dumps({"c": [[0, 5, "これはとても長い字幕になります"]]}, ensure_ascii=False)
        captions, fallback = write_captions(words, generate=lambda prompt, timeout: answer, max_chars=8, max_lines=1, line_capacity=8, hide_punctuation=True, fallback=_rule)
        self.assertEqual(fallback, 0)
        self.assertEqual("".join(c["text"] for c in captions), "これはとても長い字幕になります")
        self.assertTrue(all(len(c["text"]) <= 8 for c in captions))
        self.assertEqual(captions[0]["start_word_id"], 0)
        self.assertEqual(captions[-1]["end_word_id"], 5)

    def test_repetitions_and_question_marks_may_be_removed(self):
        self.assertTrue(is_filler_deletion(["これだ、", "これだ。"], "これだ", allow_punctuation=True))
        self.assertTrue(is_filler_deletion(["お、", "よし来た。", "よし来た。"], "よし来た", allow_punctuation=True))
        self.assertTrue(is_filler_deletion(["ちょ、", "肩車", "教えてくれん?"], "ちょ肩車教えてくれん", allow_punctuation=True))
        self.assertFalse(is_filler_deletion(["これだ、", "あれだ。"], "これだ", allow_punctuation=True))

    def test_correct_style_accepts_close_readings_only(self):
        words = _words(["監視", "は", "何で", "コーティング", "してんの?", "今日は", "晴れ。"])
        answer = json.dumps({"c": [[0, 1, "看守は"], [2, 4, "何でゴースティングしてんの？"], [5, 6, "明日も行こうぜ"]]}, ensure_ascii=False)
        captions, fallback = write_captions(words, generate=lambda prompt, timeout: answer, max_chars=14, max_lines=1, line_capacity=14, hide_punctuation=True, fallback=_rule, glossary=["ゴースティング"])
        self.assertEqual(fallback, 0)
        self.assertEqual([c["text"] for c in captions], ["看守は", "何でゴースティングしてんの？", "今日は晴れ"])

    def test_correct_style_prompt_has_glossary_topic_and_keeps_fillers(self):
        prompts = []
        words = _words(["まあ、", "薄利", "多売", "だよね。"])

        def generate(prompt, timeout):
            prompts.append(prompt)
            return json.dumps({"c": [[0, 3, "薄利多売だよね。"]]}, ensure_ascii=False)

        captions, _ = write_captions(words, generate=generate, max_chars=14, max_lines=1, line_capacity=14, hide_punctuation=True, fallback=_rule, glossary=["シャッポ"], topic="多分今のフォトナが")
        self.assertIn("シャッポ", prompts[0])
        self.assertIn("多分今のフォトナが", prompts[0])
        self.assertEqual([c["text"] for c in captions], ["薄利多売だよね"])

    def test_long_corrected_caption_is_split_at_word_boundaries(self):
        words = _words(["ブイ", "チューバ", "って", "さ", "結構", "見てる", "よね"])
        answer = json.dumps({"c": [[0, 6, "VTuberってさ結構見てるよね"]]}, ensure_ascii=False)
        captions, _ = write_captions(words, generate=lambda prompt, timeout: answer, max_chars=8, max_lines=1, line_capacity=8, hide_punctuation=True, fallback=_rule, glossary=[])
        self.assertEqual("".join(c["text"] for c in captions), "VTuberってさ結構見てるよね")
        self.assertGreater(len(captions), 1)
        self.assertEqual((captions[0]["start_word_id"], captions[-1]["end_word_id"]), (0, 6))

    def test_reading_similarity_and_status(self):
        self.assertGreaterEqual(reading_similarity("監視は", "看守は"), 0.5)
        self.assertGreaterEqual(reading_similarity("おんりアップがさ", "OnlyUpがさ"), 0.5)
        self.assertLess(reading_similarity("例えば", "来るんじゃないですか？"), 0.5)
        self.assertEqual(writer_text_status("えー、今日は", "今日は"), "ai_cleaned")
        self.assertEqual(writer_text_status("監視は", "看守は"), "ai_corrected")


if __name__ == "__main__":
    unittest.main()
