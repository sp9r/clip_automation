import unittest

from caption_bridge import build_transcript_output


class CaptionBridgeTests(unittest.TestCase):
    def test_preserves_caption_boundaries_and_source_word_metadata(self):
        transcript = [
            {
                "words": [
                    {"word": "字幕", "start": 0.1, "end": 0.4},
                    {"word": "テスト", "start": 0.4, "end": 0.9},
                ]
            }
        ]
        result = build_transcript_output(
            transcript,
            {"captions": [{"start": 0.1, "end": 0.9, "text": "字幕テスト", "start_word_id": 0, "end_word_id": 1}]},
        )
        self.assertEqual(result["schema_version"], "spectra.caption_bridge.v1")
        self.assertEqual(result["segments"][0]["text"], "字幕テスト")
        self.assertEqual([word["id"] for word in result["segments"][0]["words"]], ["source-word:0", "source-word:1"])

    def test_omits_words_when_normalized_words_do_not_reconstruct_display_text(self):
        result = build_transcript_output(
            [{"words": [{"word": "字幕。", "start": 0.0, "end": 0.5}]}],
            {"captions": [{"start": 0.0, "end": 0.5, "text": "別の字幕", "start_word_id": 0, "end_word_id": 0}]},
        )
        self.assertNotIn("words", result["segments"][0])


if __name__ == "__main__":
    unittest.main()
