import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from caption import _flatten_words, generate_subtitles
from caption_text import align_caption_word_ownership, normalize_caption_display_text
from timeline_project import build_native_v2_project, build_transcript_words, validate_timeline_project


def _words(*texts):
    return [
        {"word": text, "start": index, "end": index + 1}
        for index, text in enumerate(texts)
    ]


class CaptionWordOwnershipTests(unittest.TestCase):
    def test_raw_and_punctuation_normalized_ownership_are_explicit(self):
        words = _words("私、", "の", "本。", "。")
        result = align_caption_word_ownership(
            [{"text": "私の"}, {"text": "本"}],
            words,
        )
        self.assertEqual([(item["start_word_id"], item["end_word_id"]) for item in result], [(0, 1), (2, 3)])
        self.assertEqual([item["source_text"] for item in result], ["私、の", "本。。"])
        self.assertEqual([item["text_status"] for item in result], ["display_normalized", "display_normalized"])
        self.assertEqual(normalize_caption_display_text("私、の本。。"), "私の本")

    def test_boundary_between_words_passes_and_inside_word_fails(self):
        words = _words("字幕", "品質")
        result = align_caption_word_ownership([{"text": "字幕"}, {"text": "品質"}], words)
        self.assertEqual([item["end_word_id"] for item in result], [0, 1])
        with self.assertRaisesRegex(ValueError, "caption_boundary_inside_word"):
            align_caption_word_ownership([{"text": "字"}, {"text": "幕品質"}], words)

    def test_flatten_and_transcript_word_builder_have_same_identity(self):
        segments = [{
            "start": 0,
            "end": 2,
            "text": "字幕品質。",
            "words": [
                {"word": "字幕", "start": 0, "end": 1},
                {"word": "品質", "start": 1, "end": 2},
            ],
        }]
        flattened = _flatten_words(segments)
        built = build_transcript_words(segments)
        self.assertEqual([item["word"] for item in flattened], [item["text"] for item in built])
        self.assertEqual([item["word_id"] for item in flattened], [item["word_id"] for item in built])

    def test_native_v2_uses_explicit_range_and_validates_status(self):
        words = build_transcript_words([{
            "start": 0,
            "end": 2,
            "words": [
                {"word": "字幕、", "start": 0, "end": 1},
                {"word": "品質。", "start": 1, "end": 2},
            ],
        }])
        native = {
            "schema_version": 1,
            "captions": [{
                "text": "字幕品質",
                "start_word_id": 0,
                "end_word_id": 1,
                "source_text": "字幕、品質。",
                "text_status": "display_normalized",
                "template_id": "standard",
            }],
        }
        project = build_native_v2_project(native, words)
        self.assertEqual(project["captions"][0]["display_text"], "字幕品質")
        self.assertEqual(validate_timeline_project(project, words), [])
        native["captions"][0]["source_text"] = "字幕品質。"
        with self.assertRaisesRegex(ValueError, "source_text"):
            build_native_v2_project(native, words)

    def test_generate_subtitles_publishes_explicit_ownership(self):
        segments = [{
            "start": 0,
            "end": 2,
            "text": "私、の。",
            "words": [
                {"word": "私、", "start": 0, "end": 1},
                {"word": "の。", "start": 1, "end": 2},
            ],
        }]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "clean.json"
            srt = Path(directory) / "captions.srt"
            transcript.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
            with patch("caption._heuristic_break_indices", return_value=[1]):
                result = generate_subtitles(str(transcript), str(srt), use_llm=False)
        caption = result["captions"][0]
        self.assertEqual(caption["start_word_id"], 0)
        self.assertEqual(caption["end_word_id"], 1)
        self.assertEqual(caption["source_text"], "私、の。")
        self.assertEqual(caption["text"], "私の")
        self.assertEqual(caption["text_status"], "display_normalized")


if __name__ == "__main__":
    unittest.main()
