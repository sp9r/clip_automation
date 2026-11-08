import unittest
from subtitle_project import realign_caption_project_texts

class TestRealignCaptionProject(unittest.TestCase):
    def test_realign_texts_basic(self):
        project = {
            "captions": [
                {"id": 1, "start": 0.0, "end": 2.0, "text": "こんにちは世界"},
            ]
        }
        raw_segments = [
            {
                "text": "こんにちは世界",
                "start": 0.0,
                "end": 2.0,
                "words": [
                    {"word": "こんにちは", "start": 0.0, "end": 1.0},
                    {"word": "世界", "start": 1.0, "end": 2.0},
                ]
            }
        ]

        # ユーザーが「こんにちは」と「世界」の2行に改行区切りした場合
        new_texts = ["こんにちは", "世界"]
        realigned = realign_caption_project_texts(project, new_texts, raw_segments)
        
        self.assertEqual(len(realigned["captions"]), 2)
        self.assertEqual(realigned["captions"][0]["text"], "こんにちは")
        self.assertAlmostEqual(realigned["captions"][0]["start"], 0.0)
        self.assertAlmostEqual(realigned["captions"][0]["end"], 1.0)
        
        self.assertEqual(realigned["captions"][1]["text"], "世界")
        self.assertAlmostEqual(realigned["captions"][1]["start"], 1.0)
        self.assertAlmostEqual(realigned["captions"][1]["end"], 2.0)

if __name__ == "__main__":
    unittest.main()
