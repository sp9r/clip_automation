import json
import tempfile
import unittest
from pathlib import Path

from caption import generate_subtitles
from caption_boundary_v2 import (
    TOKEN_CLASS_JAPANESE,
    TOKEN_CLASS_LATIN,
    TOKEN_CLASS_MIXED,
    TOKEN_CLASS_NUMERIC,
    boundary_diagnostic,
    _classify_token,
    optimize_boundaries,
)
from caption_quality import (
    evaluate_caption_quality,
    fixture_boundary_sets,
    fixture_segments,
    load_horizontal_fixture,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"
DEV_FIXTURE = FIXTURE_DIR / "caption_quality_horizontal_v1.json"
HOLDOUT_FIXTURE = FIXTURE_DIR / "caption_quality_horizontal_holdout_v1.json"
HOLDOUT_BASELINE = FIXTURE_DIR / "caption_quality_horizontal_holdout_baseline.v1.json"


def _generate_metrics(fixture_path: Path):
    fixture = load_horizontal_fixture(fixture_path)
    with tempfile.TemporaryDirectory() as temp_dir:
        run_dir = Path(temp_dir)
        transcript = run_dir / "clean_transcript.json"
        srt = run_dir / "captions.srt"
        transcript.write_text(
            json.dumps(fixture_segments(fixture), ensure_ascii=False), encoding="utf-8"
        )
        result = generate_subtitles(
            str(transcript),
            str(srt),
            use_llm=False,
            max_chars=fixture["settings"]["max_chars"],
            max_lines=fixture["settings"]["max_lines"],
        )
    words = [word for segment in fixture_segments(fixture) for word in segment["words"]]
    allowed, protected = fixture_boundary_sets(fixture)
    return evaluate_caption_quality(
        words,
        result["captions"],
        max_chars=fixture["settings"]["max_chars"],
        max_lines=fixture["settings"]["max_lines"],
        allowed_break_after=allowed,
        protected_break_after=protected,
    )


class CaptionQualityStep2Tests(unittest.TestCase):
    def test_token_classes_cover_japanese_latin_numeric_and_mixed(self):
        self.assertEqual(_classify_token("字幕"), TOKEN_CLASS_JAPANESE)
        self.assertEqual(_classify_token("timeline"), TOKEN_CLASS_LATIN)
        self.assertEqual(_classify_token("1920"), TOKEN_CLASS_NUMERIC)
        self.assertEqual(_classify_token("Version二"), TOKEN_CLASS_MIXED)

    def test_boundary_diagnostic_is_explainable_and_supports_categories(self):
        words = [
            {"word": "YouTubeの", "start": 0.0, "end": 0.5},
            {"word": "長尺動画を", "start": 0.5, "end": 1.0},
        ]
        diagnostic = boundary_diagnostic(words, 0)
        for key in ("boundary_index", "base_score", "pause_contribution", "syntax_penalty", "protected_penalty", "score"):
            self.assertIn(key, diagnostic)
        self.assertTrue(diagnostic["generic_protected"])
        self.assertEqual(diagnostic["category"], "hard_forbidden")

        neutral = boundary_diagnostic(
            [{"word": "字幕", "start": 0.0, "end": 0.5}, {"word": "品質", "start": 0.5, "end": 1.0}],
            0,
        )
        self.assertEqual(neutral["category"], "neutral")
        preferred = boundary_diagnostic(
            [{"word": "字幕", "start": 0.0, "end": 0.5}, {"word": "品質", "start": 0.5, "end": 1.0}],
            0,
            preferred_breaks={0},
        )
        self.assertEqual(preferred["category"], "preferred")

    def test_holdout_has_frozen_expected_boundaries_and_many_categories(self):
        fixture = load_horizontal_fixture(HOLDOUT_FIXTURE)
        self.assertGreaterEqual(len(fixture["utterances"]), 15)
        self.assertGreaterEqual(len({item["category"] for item in fixture["utterances"]}), 8)
        baseline = json.loads(HOLDOUT_BASELINE.read_text(encoding="utf-8"))
        self.assertIn("frozen before Step 2", baseline["note"])
        self.assertEqual(baseline["metrics"]["source_word_count"], 61)

    def test_development_and_holdout_quality_gates(self):
        development = _generate_metrics(DEV_FIXTURE)
        holdout = _generate_metrics(HOLDOUT_FIXTURE)
        self.assertGreaterEqual(development.boundary_precision, 0.85)
        self.assertLessEqual(development.bad_boundary_count, 5)
        self.assertLessEqual(development.protected_boundary_violation_count, 5)
        self.assertTrue(development.exact_source_fidelity)
        self.assertTrue(development.timestamp_preserved)
        self.assertLessEqual(holdout.bad_boundary_count, 1)
        self.assertTrue(holdout.exact_source_fidelity)
        self.assertTrue(holdout.timestamp_preserved)
        self.assertGreater(holdout.caption_count, 0)

    def test_max_chars_keeps_a_complete_path_when_protection_must_be_forced(self):
        words = [
            {"word": "Alphaの", "start": 0.0, "end": 0.5},
            {"word": "Beta", "start": 0.5, "end": 1.0},
        ]
        breaks = optimize_boundaries(words, max_chars=6, max_duration=5.0)
        self.assertEqual(breaks, [0, 1])
        self.assertEqual("".join(words[index]["word"] for index in range(len(words))), "AlphaのBeta")

    def test_generalized_rules_do_not_add_development_fixture_phrases(self):
        source = (Path(__file__).parents[1] / "caption_boundary_v2.py").read_text(encoding="utf-8")
        for phrase in ("YouTubeの", "次のStepでは", "このfixtureを", "timeと", "担当する"):
            self.assertNotIn(phrase, source)


if __name__ == "__main__":
    unittest.main()
