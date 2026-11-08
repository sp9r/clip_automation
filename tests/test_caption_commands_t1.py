import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caption_commands import CaptionCommandService, apply_command
from timeline_project import build_transcript_words, migrate_v1_project


class CaptionCommandServiceTests(unittest.TestCase):
    def setUp(self):
        raw = [{"start": 0, "words": [
            {"word": "A", "start": 0, "end": 1}, {"word": "B", "start": 1, "end": 2},
            {"word": "C", "start": 2, "end": 3}, {"word": "D", "start": 3, "end": 4},
        ]}]
        self.words = build_transcript_words(raw)
        v1 = {"schema_version": 1, "captions": [{"text": "AB"}, {"text": "CD"}]}
        self.project = migrate_v1_project(v1, self.words)

    def command(self, command_type, **payload):
        return {"command_id": "cmd", "type": command_type, "base_revision": self.project["revision"], **payload}

    def test_revision_conflict_is_rejected(self):
        command = self.command("UpdateCaptionText", caption_id="cap_000001", display_text="修正")
        command["base_revision"] = 99
        with self.assertRaisesRegex(ValueError, "revision競合"):
            apply_command(self.project, self.words, command)

    def test_text_edit_changes_only_display_fields_and_revision(self):
        original = dict(self.project["captions"][0])
        result = apply_command(self.project, self.words, self.command(
            "UpdateCaptionText", caption_id="cap_000001", display_text="AX"
        ))
        edited = result["captions"][0]
        for field in ("source_text", "start_word_id", "end_word_id", "start", "end"):
            self.assertEqual(edited[field], original[field])
        self.assertEqual(edited["display_text"], "AX")
        self.assertEqual(edited["text_status"], "manually_corrected")

    def test_boundary_move_updates_only_adjacent_captions_without_word_loss(self):
        result = apply_command(self.project, self.words, self.command(
            "MoveBoundary", left_id="cap_000001", right_id="cap_000002", boundary_word_id=2
        ))
        self.assertEqual((result["captions"][0]["start_word_id"], result["captions"][0]["end_word_id"]), (0, 2))
        self.assertEqual((result["captions"][1]["start_word_id"], result["captions"][1]["end_word_id"]), (3, 3))
        bad = dict(self.command("MoveBoundary", left_id="cap_000001", right_id="cap_000002", boundary_word_id=3))
        with self.assertRaisesRegex(ValueError, "空字幕"):
            apply_command(self.project, self.words, bad)

    def test_boundary_move_keeps_unedited_display_text_aligned_to_words(self):
        result = apply_command(self.project, self.words, self.command(
            "MoveBoundary", left_id="cap_000001", right_id="cap_000002", boundary_word_id=2
        ))
        self.assertEqual(result["captions"][0]["display_text"], "ABC")
        self.assertEqual(result["captions"][1]["display_text"], "D")

    def test_boundary_move_rejects_gap_and_non_whisper_word_id(self):
        gap_project = json.loads(json.dumps(self.project))
        gap_project["captions"][1]["start_word_id"] = 3
        with self.assertRaisesRegex(ValueError, "contiguous"):
            apply_command(gap_project, self.words, {
                **self.command("MoveBoundary", left_id="cap_000001", right_id="cap_000002", boundary_word_id=2),
                "base_revision": gap_project["revision"],
            })
        with self.assertRaisesRegex(ValueError, "存在しません"):
            apply_command(self.project, self.words, self.command(
                "MoveBoundary", left_id="cap_000001", right_id="cap_000002", boundary_word_id=99
            ))

    def test_split_edited_caption_preserves_text_and_partitions_overrides(self):
        edited = apply_command(self.project, self.words, self.command(
            "UpdateCaptionText", caption_id="cap_000001", display_text="AB"
        ))
        edited["captions"][0]["word_timing_overrides"] = [
            {"word_id": 0, "start": 0.1, "end": 0.9},
            {"word_id": 1, "start": 1.1, "end": 1.9},
        ]
        edited["captions"][0]["start"] = 0.1
        edited["captions"][0]["end"] = 1.9
        split = apply_command(edited, self.words, {
            "command_id": "split-edited", "type": "SplitCaption", "base_revision": edited["revision"],
            "caption_id": "cap_000001", "after_word_id": 0,
            "left_display_text": "A", "right_display_text": "B",
        })
        self.assertEqual("".join(item["display_text"] for item in split["captions"][:2]), "AB")
        self.assertEqual([item["word_id"] for item in split["captions"][0]["word_timing_overrides"]], [0])
        self.assertEqual([item["word_id"] for item in split["captions"][1]["word_timing_overrides"]], [1])

    def test_split_edited_caption_requires_exact_display_parts(self):
        edited = apply_command(self.project, self.words, self.command(
            "UpdateCaptionText", caption_id="cap_000001", display_text="修正AB"
        ))
        with self.assertRaisesRegex(ValueError, "display text must be preserved"):
            apply_command(edited, self.words, {
                "command_id": "split-edited", "type": "SplitCaption", "base_revision": edited["revision"],
                "caption_id": "cap_000001", "after_word_id": 0,
                "left_display_text": "修正", "right_display_text": "A",
            })

    def test_merge_requires_adjacent_contiguous_captions_and_keeps_all_words(self):
        with self.assertRaisesRegex(ValueError, "adjacent"):
            apply_command(self.project, self.words, self.command(
                "MergeCaptions", left_id="cap_000002", right_id="cap_000001"
            ))
        gap_project = json.loads(json.dumps(self.project))
        gap_project["captions"][1]["start_word_id"] = 3
        with self.assertRaisesRegex(ValueError, "adjacent word ranges"):
            apply_command(gap_project, self.words, {
                **self.command("MergeCaptions", left_id="cap_000001", right_id="cap_000002"),
                "base_revision": gap_project["revision"],
            })

        merged = apply_command(self.project, self.words, self.command(
            "MergeCaptions", left_id="cap_000001", right_id="cap_000002"
        ))
        self.assertEqual((merged["captions"][0]["start_word_id"], merged["captions"][0]["end_word_id"]), (0, 3))
        self.assertEqual("".join(word["text"] for word in self.words), merged["captions"][0]["source_text"])

    def test_timing_override_rejects_overlap_with_neighbor_word(self):
        with self.assertRaisesRegex(ValueError, "invalid command result"):
            apply_command(self.project, self.words, self.command(
                "SetTimingOverride", caption_id="cap_000001", word_id=1, start=0.5, end=2.5
            ))

    def test_caption_move_keeps_word_mapping_and_shifts_timeline(self):
        moved = apply_command(self.project, self.words, self.command(
            "MoveCaption", caption_id="cap_000001", delta=0.5
        ))
        caption = moved["captions"][0]
        self.assertEqual(caption["start_word_id"], 0)
        self.assertEqual(caption["end_word_id"], 1)
        self.assertEqual(caption["position_offset"], 0.5)
        self.assertEqual((caption["start"], caption["end"]), (0.5, 2.5))
        with self.assertRaisesRegex(ValueError, "先頭より前"):
            apply_command(self.project, self.words, self.command(
                "MoveCaption", caption_id="cap_000001", delta=-0.1
            ))

    def test_caption_delete_is_soft_delete_and_can_be_undone(self):
        deleted = apply_command(self.project, self.words, self.command(
            "DeleteCaption", caption_id="cap_000001"
        ))
        self.assertTrue(deleted["captions"][0]["deleted"])
        self.assertEqual(deleted["captions"][1]["display_text"], "CD")
        with self.assertRaisesRegex(ValueError, "すでに削除"):
            apply_command(deleted, self.words, {
                **self.command("DeleteCaption", caption_id="cap_000001"),
                "base_revision": deleted["revision"],
            })

    def test_command_envelope_rejects_non_string_caption_ids(self):
        with self.assertRaisesRegex(ValueError, "caption_id"):
            apply_command(self.project, self.words, self.command(
                "UpdateCaptionText", caption_id=1, display_text="AX"
            ))

    def test_split_then_merge_restores_word_range_and_text(self):
        split = apply_command(self.project, self.words, self.command(
            "SplitCaption", caption_id="cap_000001", after_word_id=0
        ))
        merged = apply_command(split, self.words, {
            "command_id": "merge", "type": "MergeCaptions", "base_revision": 1,
            "left_id": split["captions"][0]["id"], "right_id": split["captions"][1]["id"],
        })
        self.assertEqual(merged["captions"][0]["source_text"], "AB")
        self.assertEqual(merged["captions"][0]["display_text"], "AB")
        self.assertEqual((merged["captions"][0]["start_word_id"], merged["captions"][0]["end_word_id"]), (0, 1))

    def test_history_undo_redo_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            path.write_text(json.dumps(self.project), encoding="utf-8")
            service = CaptionCommandService(path, self.words)
            service.execute(self.command("UpdateCaptionText", caption_id="cap_000001", display_text="AX"))
            undone = service.undo()
            self.assertEqual(undone["captions"][0]["display_text"], "AB")
            self.assertEqual(undone["revision"], 2)
            restarted = CaptionCommandService(path, self.words)
            redone = restarted.redo()
            self.assertEqual(redone["captions"][0]["display_text"], "AX")
            self.assertEqual(redone["revision"], 3)
            history_lines = (Path(temp) / "caption_history.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(history_lines[-1]), {"record_type": "state", "cursor": 1})

    def test_history_keeps_at_least_one_hundred_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            path.write_text(json.dumps(self.project), encoding="utf-8")
            service = CaptionCommandService(path, self.words)
            for revision in range(105):
                service.execute({
                    "command_id": f"cmd_{revision}", "type": "UpdateCaptionText", "base_revision": revision,
                    "caption_id": "cap_000001", "display_text": f"edit-{revision}",
                })
            restarted = CaptionCommandService(path, self.words)
            self.assertEqual(len(restarted._history["entries"]), 100)
            self.assertEqual(restarted._history["cursor"], 100)

    def test_atomic_replace_failure_keeps_last_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            path.write_text(json.dumps(self.project), encoding="utf-8")
            service = CaptionCommandService(path, self.words)
            with patch("timeline_project.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    service.execute(self.command("UpdateCaptionText", caption_id="cap_000001", display_text="AX"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["revision"], 0)


if __name__ == "__main__":
    unittest.main()
