import unittest

from ui_helpers import (
    apply_resolution_preset,
    build_template_preview,
    detect_resolution_preset,
    installed_font_names,
)


class UiHelperTests(unittest.TestCase):
    def test_resolution_presets_and_custom_detection(self):
        self.assertEqual(apply_resolution_preset("fhd_landscape", 1, 1), (1920, 1080))
        self.assertEqual(apply_resolution_preset("fhd_portrait", 1, 1), (1080, 1920))
        self.assertEqual(apply_resolution_preset("custom", 1600, 900), (1600, 900))
        self.assertEqual(detect_resolution_preset(3840, 2160), "uhd_landscape")
        self.assertEqual(detect_resolution_preset(1600, 900), "custom")

    def test_font_choices_include_common_fonts(self):
        fonts = installed_font_names()
        self.assertIn("Meiryo", fonts)
        self.assertEqual(len(fonts), len(set(fonts)))

    def test_template_preview_escapes_text_and_uses_output_ratio(self):
        preview = build_template_preview(
            "<script>字幕</script>",
            "Meiryo",
            54,
            "#FFFFFF",
            "#000000",
            True,
            False,
            3,
            1,
            2,
            80,
            False,
            None,
            None,
            1920,
            1080,
        )
        self.assertIn("template-preview-stage", preview)
        self.assertIn("&lt;script&gt;字幕&lt;/script&gt;", preview)

        self.assertNotIn("<script>字幕</script>", preview)
        self.assertIn("font-family:'Meiryo'", preview)


if __name__ == "__main__":
    unittest.main()
