import copy
import math
import unittest
from decimal import Decimal

from edit_project import (
    EditProjectValidationError,
    _decimal_seconds,
    build_sequence_clip_mappings,
    build_source_timeline_mapping,
    map_timeline_interval_to_sequence,
    require_valid_edit_project,
    validate_edit_project,
)


def _project(*, clips, captions=None, duration=None, source_duration=30.0):
    sequence_duration = duration if duration is not None else max(
        [clip["timeline"]["start"] + clip["timeline"]["duration"] for clip in clips] + [0.0]
    )
    return {
        "schema_version": "edit_project.v1",
        "project": {"id": "fixture_project", "name": "Fixture"},
        "source_media": [
            {
                "id": "media_main",
                "path": "source.mp4",
                "kind": "video",
                "duration": source_duration,
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
            }
        ],
        "sequence": {
            "id": "sequence_main",
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
            "timebase": {"numerator": 1, "denominator": 1000},
            "duration": sequence_duration,
        },
        "tracks": [
            {"id": "V1", "kind": "video", "name": "Video", "enabled": True, "locked": False, "clips": clips},
            {"id": "A1", "kind": "audio", "name": "Audio", "enabled": True, "locked": False, "clips": []},
            {"id": "C1", "kind": "caption", "name": "Captions", "enabled": True, "locked": False, "clips": []},
        ],
        "captions": captions or [],
        "metadata": {
            "source_run_id": "fixture_run",
            "writer_version": "test",
            "schema_version": "edit_project.v1",
            "input_hashes": {"fixture": "0" * 64},
            "source_media_probe": {"duration": source_duration, "width": 1920, "height": 1080, "fps": 30.0},
        },
    }


class MappingTests(unittest.TestCase):
    def test_single_edl_and_keep_region_maps_to_one_clip(self):
        mappings = build_sequence_clip_mappings(
            [{"start": 10.0, "end": 15.0}],
            [
                {"id": "delete_head", "start": 0.0, "end": 1.0, "mode": "delete"},
                {"id": "keep_1", "start": 1.0, "end": 3.0, "mode": "keep"},
                {"id": "delete_tail", "start": 3.0, "end": 5.0, "mode": "delete"},
            ],
            media_duration=30.0,
        )
        self.assertEqual(len(mappings), 1)
        self.assertEqual((mappings[0].source_start, mappings[0].source_end), (11.0, 13.0))
        self.assertEqual((mappings[0].sequence_start, mappings[0].sequence_end), (0.0, 2.0))

    def test_keep_region_crossing_edl_boundary_splits_and_compacts_sequence(self):
        mappings = build_sequence_clip_mappings(
            [{"start": 10.0, "end": 15.0}, {"start": 20.0, "end": 25.0}],
            [
                {"id": "delete_head", "start": 0.0, "end": 3.0, "mode": "delete"},
                {"id": "keep_1", "start": 3.0, "end": 7.0, "mode": "keep"},
                {"id": "delete_tail", "start": 7.0, "end": 10.0, "mode": "delete"},
            ],
            media_duration=30.0,
        )
        self.assertEqual(len(mappings), 2)
        self.assertEqual(
            [(item.source_start, item.source_end) for item in mappings],
            [(13.0, 15.0), (20.0, 22.0)],
        )
        self.assertEqual(
            [(item.sequence_start, item.sequence_end) for item in mappings],
            [(0.0, 2.0), (2.0, 4.0)],
        )

    def test_multiple_keep_regions_and_head_tail_delete(self):
        mappings = build_sequence_clip_mappings(
            [{"start": 0.0, "end": 10.0}],
            [
                {"id": "delete_head", "start": 0.0, "end": 2.0, "mode": "delete"},
                {"id": "keep_a", "start": 2.0, "end": 4.0, "mode": "keep"},
                {"id": "delete_middle", "start": 4.0, "end": 6.0, "mode": "delete"},
                {"id": "keep_b", "start": 6.0, "end": 8.0, "mode": "keep"},
                {"id": "delete_tail", "start": 8.0, "end": 10.0, "mode": "delete"},
            ],
            media_duration=10.0,
        )
        self.assertEqual([(m.source_start, m.source_end) for m in mappings], [(2.0, 4.0), (6.0, 8.0)])
        self.assertEqual([(m.sequence_start, m.sequence_end) for m in mappings], [(0.0, 2.0), (2.0, 4.0)])

    def test_region_outside_edl_is_rejected(self):
        with self.assertRaises(EditProjectValidationError):
            build_sequence_clip_mappings(
                [{"start": 0.0, "end": 5.0}],
                [{"id": "keep", "start": 0.0, "end": 6.0, "mode": "keep"}],
                media_duration=10.0,
            )

    def test_invalid_edl_order_overlap_and_reverse_are_rejected(self):
        for edl in (
            [{"start": 5.0, "end": 10.0}, {"start": 4.0, "end": 6.0}],
            [{"start": 0.0, "end": 5.0}, {"start": 4.0, "end": 8.0}],
            [{"start": 5.0, "end": 4.0}],
        ):
            with self.subTest(edl=edl), self.assertRaises(EditProjectValidationError):
                build_source_timeline_mapping(edl, media_duration=20.0)

    def test_interval_mapping_returns_only_kept_pieces(self):
        mappings = build_sequence_clip_mappings(
            [{"start": 0.0, "end": 10.0}],
            [
                {"id": "keep_a", "start": 0.0, "end": 3.0, "mode": "keep"},
                {"id": "delete", "start": 3.0, "end": 5.0, "mode": "delete"},
                {"id": "keep_b", "start": 5.0, "end": 10.0, "mode": "keep"},
            ],
            media_duration=10.0,
        )
        pieces = map_timeline_interval_to_sequence(1.0, 7.0, mappings)
        self.assertEqual(
            [(piece["sequence_start"], piece["sequence_end"]) for piece in pieces],
            [(1.0, 3.0), (3.0, 5.0)],
        )


class ValidatorTests(unittest.TestCase):
    def test_valid_video_clip_project_passes(self):
        clip = {
            "id": "vclip_1",
            "type": "video_clip",
            "source_media_id": "media_main",
            "timeline": {"start": 0.0, "duration": 2.0},
            "source": {"start": 1.0, "duration": 2.0},
            "transform": {
                "x": 0.0,
                "y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
                "rotation": 0.0,
                "opacity": 1.0,
                "anchor_x": 0.5,
                "anchor_y": 0.5,
            },
            "enabled": True,
        }
        project = _project(clips=[clip])
        self.assertEqual(validate_edit_project(project), [])
        self.assertIs(require_valid_edit_project(project), project)

    def test_duplicate_ids_missing_reference_and_source_overflow_are_rejected(self):
        clip = {
            "id": "duplicate",
            "type": "video_clip",
            "source_media_id": "missing",
            "timeline": {"start": 0.0, "duration": 2.0},
            "source": {"start": 29.0, "duration": 2.0},
            "transform": {"x": 0, "y": 0, "scale_x": 1, "scale_y": 1, "rotation": 0, "opacity": 1, "anchor_x": 0.5, "anchor_y": 0.5},
            "enabled": True,
        }
        project = _project(clips=[clip, copy.deepcopy(clip)], duration=4.0)
        issues = validate_edit_project(project)
        self.assertTrue(any("duplicate clip id" in issue for issue in issues))
        self.assertTrue(any("missing source media" in issue for issue in issues))
        with self.assertRaises(EditProjectValidationError):
            require_valid_edit_project(project)

    def test_nan_and_infinity_are_rejected(self):
        clip = {
            "id": "vclip_1",
            "type": "video_clip",
            "source_media_id": "media_main",
            "timeline": {"start": 0.0, "duration": 2.0},
            "source": {"start": 1.0, "duration": 2.0},
            "transform": {"x": math.nan, "y": 0, "scale_x": 1, "scale_y": 1, "rotation": 0, "opacity": 1, "anchor_x": 0.5, "anchor_y": 0.5},
            "enabled": True,
        }
        project = _project(clips=[clip])
        self.assertTrue(any("finite" in issue for issue in validate_edit_project(project)))

    def test_optional_caption_words_validate_identity_timing_and_text(self):
        caption = {
            "id": "caption_1", "track_id": "C1", "text": "AB", "timeline": {"start": 1.0, "duration": 1.0},
            "enabled": True, "words": [
                {"id": "source-word:1", "text": "A", "timeline": {"start": 1.0, "duration": 0.4}},
                {"id": "source-word:2", "text": "B", "timeline": {"start": 1.4, "duration": 0.4}},
            ], "source_word_ids": [1, 2]
        }
        project = _project(clips=[], captions=[caption], duration=2.0)
        self.assertEqual(validate_edit_project(project), [])
        for mutation in (
            lambda item: item["words"][1].update({"id": "source-word:1"}),
            lambda item: item["words"][1]["timeline"].update({"start": 1.2}),
            lambda item: item["words"][1]["timeline"].update({"start": 1.9}),
            lambda item: item["words"][0].update({"text": "X"}),
        ):
            invalid = copy.deepcopy(project)
            mutation(invalid["captions"][0])
            self.assertTrue(validate_edit_project(invalid), invalid)

    def test_caption_word_decimal_adjacency_does_not_create_float_overlap(self):
        caption = {
            "id": "caption_1", "track_id": "C1", "text": "AB", "timeline": {"start": 13.88, "duration": 0.52},
            "enabled": True, "words": [
                {"id": "source-word:1", "text": "A", "timeline": {"start": 13.88, "duration": 0.32}},
                {"id": "source-word:2", "text": "B", "timeline": {"start": 14.2, "duration": 0.2}},
            ], "source_word_ids": [1, 2]
        }
        self.assertEqual(validate_edit_project(_project(clips=[], captions=[caption], duration=14.4)), [])

    def test_caption_word_true_submicrosecond_overlap_is_rejected(self):
        caption = {
            "id": "caption_1", "track_id": "C1", "text": "AB", "timeline": {"start": 13.88, "duration": 0.52},
            "enabled": True, "words": [
                {"id": "source-word:1", "text": "A", "timeline": {"start": 13.88, "duration": 0.320000000000001}},
                {"id": "source-word:2", "text": "B", "timeline": {"start": 14.2, "duration": 0.2}},
            ], "source_word_ids": [1, 2]
        }
        issues = validate_edit_project(_project(clips=[], captions=[caption], duration=14.4))
        self.assertIn("captions[0].words[1] overlaps or reverses the previous word", issues)

    def test_caption_word_true_microsecond_overlap_is_rejected(self):
        caption = {
            "id": "caption_1", "track_id": "C1", "text": "AB", "timeline": {"start": 13.88, "duration": 0.52},
            "enabled": True, "words": [
                {"id": "source-word:1", "text": "A", "timeline": {"start": 13.88, "duration": 0.320001}},
                {"id": "source-word:2", "text": "B", "timeline": {"start": 14.2, "duration": 0.2}},
            ], "source_word_ids": [1, 2]
        }
        issues = validate_edit_project(_project(clips=[], captions=[caption], duration=14.4))
        self.assertIn("captions[0].words[1] overlaps or reverses the previous word", issues)

    def test_caption_word_decimal_exact_end_and_overrun(self):
        exact = {
            "id": "caption_1", "track_id": "C1", "text": "AB", "timeline": {"start": 13.88, "duration": 0.52},
            "enabled": True, "words": [
                {"id": "source-word:1", "text": "A", "timeline": {"start": 13.88, "duration": 0.32}},
                {"id": "source-word:2", "text": "B", "timeline": {"start": 14.2, "duration": 0.2}},
            ], "source_word_ids": [1, 2]
        }
        self.assertEqual(validate_edit_project(_project(clips=[], captions=[exact], duration=14.4)), [])
        overrun = copy.deepcopy(exact)
        overrun["words"][1]["timeline"]["duration"] = 0.200001
        issues = validate_edit_project(_project(clips=[], captions=[overrun], duration=14.4))
        self.assertIn("captions[0].words[1] ends after the caption", issues)

    def test_decimal_seconds_uses_string_semantics_without_quantization(self):
        self.assertEqual(_decimal_seconds(13.88, "time"), Decimal("13.88"))
        self.assertEqual(_decimal_seconds("14.200001", "time"), Decimal("14.200001"))
        with self.assertRaises(EditProjectValidationError):
            _decimal_seconds(float("nan"), "time")
        with self.assertRaises(EditProjectValidationError):
            _decimal_seconds(float("inf"), "time")


if __name__ == "__main__":
    unittest.main()
