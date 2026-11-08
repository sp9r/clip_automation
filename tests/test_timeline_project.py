import json
import tempfile
import unittest
from pathlib import Path

from caption_commands import validate_command_envelope
from timeline_project import (
    V2_SCHEMA_VERSION,
    build_transcript_words,
    load_timeline_project,
    migrate_v1_project_file,
    migrate_v1_project,
    validate_timeline_project,
)


class TimelineProjectTests(unittest.TestCase):
    def setUp(self):
        self.raw_segments = [
            {
                "start": 0.0,
                "end": 2.0,
                "words": [
                    {"word": "大阪", "start": 0.0, "end": 0.5, "probability": 0.9},
                    {"word": "大学", "start": 0.5, "end": 1.0, "probability": 0.8},
                    {"word": "です。", "start": 1.0, "end": 1.4, "probability": 0.95},
                    {"word": "よろしく", "start": 1.5, "end": 1.8, "probability": 0.9},
                    {"word": "お願いします。", "start": 1.8, "end": 2.0, "probability": 0.9},
                ],
            }
        ]
        self.v1 = {
            "schema_version": 1,
            "run_dir": "run",
            "source_path": "input.mp4",
            "edl_path": "edl.json",
            "output": {"width": 640, "height": 360, "fps": 30},
            "caption_settings": {"max_chars": 24},
            "default_template_id": "standard",
            "captions": [
                {"id": 1, "start": 0.0, "end": 1.4, "text": "大学です。", "template_id": ""},
                {"id": 2, "start": 1.5, "end": 2.0, "text": "よろしくお願いします。", "template_id": "emphasis"},
            ],
        }

    def test_word_schema_is_immutable_and_has_stable_timeline_times(self):
        words = build_transcript_words(self.raw_segments)

        self.assertEqual([word["word_id"] for word in words], [0, 1, 2, 3, 4])
        self.assertEqual(words[0]["timeline_start"], 0.0)
        self.assertEqual(words[-1]["timeline_end"], 2.0)
        self.assertEqual("".join(word["text"] for word in words), "大学です。よろしくお願いします。")

    def test_migration_creates_v2_without_mutating_v1(self):
        words = build_transcript_words(self.raw_segments)
        original = json.loads(json.dumps(self.v1))

        migrated = migrate_v1_project(self.v1, words)

        self.assertEqual(migrated["schema_version"], V2_SCHEMA_VERSION)
        self.assertEqual(migrated["revision"], 0)
        self.assertEqual(migrated["captions"][0]["id"], "cap_000001")
        self.assertEqual(migrated["captions"][0]["start_word_id"], 0)
        self.assertEqual(migrated["captions"][0]["end_word_id"], 2)
        self.assertEqual(migrated["captions"][0]["source_text"], "大学です。")
        self.assertEqual(migrated["captions"][0]["display_text"], "大学です。")
        self.assertEqual(migrated["captions"][1]["template_id"], "emphasis")
        self.assertEqual(self.v1, original)
        self.assertEqual(validate_timeline_project(migrated, words), [])

    def test_migration_rejects_text_that_cannot_be_aligned_to_words(self):
        words = build_transcript_words(self.raw_segments)
        broken = json.loads(json.dumps(self.v1))
        broken["captions"][0]["text"] = "大阪市です。"

        with self.assertRaisesRegex(ValueError, "対応付け"):
            migrate_v1_project(broken, words)

    def test_file_migration_keeps_v1_and_writes_separate_backup_and_v2(self):
        words = build_transcript_words(self.raw_segments)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            v1_path = root / "caption_project.json"
            v1_path.write_text(json.dumps(self.v1, ensure_ascii=False), encoding="utf-8")

            v2_path = migrate_v1_project_file(v1_path, words)

            self.assertTrue(v1_path.exists())
            self.assertTrue((root / "caption_project.v1.backup.json").exists())
            self.assertEqual(v2_path.name, "caption_project.v2.json")
            self.assertEqual(load_timeline_project(v2_path)["schema_version"], V2_SCHEMA_VERSION)

    def test_validation_rejects_missing_or_overlapping_words(self):
        words = build_transcript_words(self.raw_segments)
        project = migrate_v1_project(self.v1, words)
        project["captions"][1]["start_word_id"] = 2

        issues = validate_timeline_project(project, words)

        self.assertTrue(any("重複" in issue for issue in issues))

    def test_command_contract_requires_revision_and_minimal_payload(self):
        command = validate_command_envelope(
            {
                "command_id": "cmd_001",
                "type": "MoveBoundary",
                "base_revision": 3,
                "left_id": "cap_000001",
                "right_id": "cap_000002",
                "boundary_word_id": 12,
            }
        )

        self.assertEqual(command["base_revision"], 3)
        with self.assertRaisesRegex(ValueError, "base_revision"):
            validate_command_envelope(
                {"command_id": "cmd_002", "type": "UpdateCaptionText", "caption_id": "cap_000001", "display_text": "修正"}
            )


if __name__ == "__main__":
    unittest.main()
