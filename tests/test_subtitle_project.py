import json
import tempfile
import unittest
from pathlib import Path

from subtitle_project import (
    create_caption_project,
    load_templates,
    save_user_template,
    write_ass_subtitles,
)


class SubtitleProjectTests(unittest.TestCase):
    def test_project_defaults_to_1080p60(self):
        project = create_caption_project(
            captions=[{"start": 0, "end": 2, "text": "テスト字幕"}],
            source_path="input.mp4",
            edl_path="edl.json",
            run_dir="run",
        )
        self.assertEqual(project["output"], {"width": 1920, "height": 1080, "fps": 60})
        self.assertEqual(project["default_template_id"], "standard")

    def test_user_template_and_per_caption_style_are_written_to_ass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "templates.json"
            saved = save_user_template(
                {
                    "name": "左上テスト",
                    "font_name": "Meiryo",
                    "font_size": 72,
                    "primary_color": "#00FF00",
                    "outline_color": "#000000",
                    "bold": True,
                    "italic": False,
                    "outline": 4,
                    "shadow": 1,
                    "alignment": 7,
                    "margin_v": 50,
                    "position_x": 200,
                    "position_y": 150,
                },
                template_path,
            )
            project = create_caption_project(
                captions=[
                    {
                        "start": 0,
                        "end": 2,
                        "text": "個別テンプレート",
                        "template_id": saved["id"],
                    }
                ],
                source_path="input.mp4",
                edl_path="edl.json",
                run_dir="run",
            )
            ass_path = Path(temp_dir) / "captions.ass"
            write_ass_subtitles(project, ass_path, template_path=template_path)
            output = ass_path.read_text(encoding="utf-8-sig")

            self.assertIn(f"Style: {saved['id']}", output)
            self.assertIn(",72,", output)

            self.assertIn(f",{saved['id']},", output)
            self.assertIn(r"{\pos(200,150)}個別テンプレート", output)
            self.assertIn(saved["id"], load_templates(template_path))


if __name__ == "__main__":
    unittest.main()
