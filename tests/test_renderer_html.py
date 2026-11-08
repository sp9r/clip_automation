import tempfile
import unittest
from pathlib import Path

from PIL import Image

from renderer_html import render_caption_html_png


class HtmlRendererTests(unittest.TestCase):
    def test_transparent_caption_png_is_rendered_in_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "caption.png"
            render_caption_html_png(
                "一時テスト字幕",
                {
                    "font_name": "Meiryo",
                    "font_size": 36,
                    "primary_color": "#FFFFFF",
                    "outline_color": "#000000",
                    "bold": True,
                    "outline": 3,
                    "shadow": 1,
                    "alignment": 2,
                    "margin_v": 30,
                },
                640,
                360,
                output,
            )
            with Image.open(output) as image:
                alpha = image.convert("RGBA").getchannel("A")
                self.assertEqual(image.size, (640, 360))
                self.assertEqual(alpha.getextrema()[0], 0)
                self.assertGreater(alpha.getextrema()[1], 0)


if __name__ == "__main__":
    unittest.main()
