"""Pure edit_project/v1 model, validation, and source/timeline mapping.

This module deliberately has no dependency on the editor API, frontend, FFmpeg,
or a run directory.  It only validates contract-shaped data and converts EDL
source intervals plus analysis-timeline keep regions into deterministic
sequence clips.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


SCHEMA_VERSION = "edit_project.v1"
TIME_EPSILON = 1e-6


class EditProjectValidationError(ValueError):
    """Raised when input data cannot satisfy the edit_project contract."""

    def __init__(self, message: str, issues: Iterable[str] | None = None):
        self.issues = tuple(issues or (message,))
        super().__init__(message)


@dataclass(frozen=True)
class SourceTimelineSegment:
    """One continuous EDL source interval and its jumpcut-timeline interval."""

    source_start: float
    source_end: float
    timeline_start: float
    timeline_end: float

    @property
    def duration(self) -> float:
        return self.source_end - self.source_start


@dataclass(frozen=True)
class SequenceClipMapping:
    """A kept intersection mapped to final edit sequence time."""

    region_id: str
    source_start: float
    source_end: float
    timeline_start: float
    timeline_end: float
    sequence_start: float
    sequence_end: float

    @property
    def duration(self) -> float:
        return self.sequence_end - self.sequence_start


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise EditProjectValidationError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise EditProjectValidationError(f"{label} must be a finite number")
    if minimum is not None and number < minimum - TIME_EPSILON:
        raise EditProjectValidationError(f"{label} must be >= {minimum}")
    return number


def _decimal_seconds(value: Any, label: str) -> Decimal:
    """Parse serialized seconds without reintroducing binary float artifacts."""

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise EditProjectValidationError(f"{label} must be a finite number") from error
    if not number.is_finite():
        raise EditProjectValidationError(f"{label} must be a finite number")
    return number


def _interval(item: dict, label: str) -> tuple[float, float]:
    if not isinstance(item, dict):
        raise EditProjectValidationError(f"{label} must be an object")
    try:
        start = _finite_number(item["start"], f"{label}.start", minimum=0.0)
        end = _finite_number(item["end"], f"{label}.end", minimum=0.0)
    except KeyError as error:
        raise EditProjectValidationError(f"{label} requires {error.args[0]}") from error
    if end <= start + TIME_EPSILON:
        raise EditProjectValidationError(f"{label} must have end > start")
    return start, end


def validate_edl(edl: list[dict], *, media_duration: float | None = None) -> list[str]:
    """Return validation issues for source-time EDL intervals."""

    issues: list[str] = []
    if not isinstance(edl, list) or not edl:
        return ["edl must be a non-empty array"]
    previous_end = -math.inf
    for index, entry in enumerate(edl):
        try:
            start, end = _interval(entry, f"edl[{index}]")
        except EditProjectValidationError as error:
            issues.append(str(error))
            continue
        if start < previous_end - TIME_EPSILON:
            issues.append(f"edl[{index}] overlaps or reverses the preceding source interval")
        if media_duration is not None:
            try:
                duration = _finite_number(media_duration, "media_duration", minimum=0.0)
                if end > duration + TIME_EPSILON:
                    issues.append(f"edl[{index}] exceeds source media duration")
            except EditProjectValidationError as error:
                issues.append(str(error))
        previous_end = max(previous_end, end)
    return issues


def build_source_timeline_mapping(
    edl: list[dict], *, media_duration: float | None = None
) -> tuple[SourceTimelineSegment, ...]:
    """Map each source EDL interval to monotonic jumpcut timeline time."""

    issues = validate_edl(edl, media_duration=media_duration)
    if issues:
        raise EditProjectValidationError("invalid EDL", issues)

    timeline_cursor = 0.0
    segments: list[SourceTimelineSegment] = []
    for entry in edl:
        source_start, source_end = _interval(entry, "edl interval")
        duration = source_end - source_start
        segment = SourceTimelineSegment(
            source_start=source_start,
            source_end=source_end,
            timeline_start=timeline_cursor,
            timeline_end=timeline_cursor + duration,
        )
        segments.append(segment)
        timeline_cursor = segment.timeline_end
    return tuple(segments)


def validate_edit_regions(
    regions: list[dict], timeline_duration: float, *, require_full_coverage: bool = True
) -> list[str]:
    """Validate keep/delete regions on jumpcut timeline time."""

    issues: list[str] = []
    if not isinstance(regions, list) or not regions:
        return ["edit_regions must be a non-empty array"]
    try:
        duration = _finite_number(timeline_duration, "timeline_duration", minimum=0.0)
    except EditProjectValidationError as error:
        return [str(error)]

    seen_ids: set[str] = set()
    previous_end = 0.0
    for index, region in enumerate(regions):
        label = f"edit_regions[{index}]"
        if not isinstance(region, dict):
            issues.append(f"{label} must be an object")
            continue
        region_id = region.get("id")
        if not isinstance(region_id, str) or not region_id.strip():
            issues.append(f"{label}.id must be a non-empty string")
        elif region_id in seen_ids:
            issues.append(f"duplicate edit region id: {region_id}")
        else:
            seen_ids.add(region_id)
        if region.get("mode") not in {"keep", "delete"}:
            issues.append(f"{label}.mode must be keep or delete")
        try:
            start, end = _interval(region, label)
        except EditProjectValidationError as error:
            issues.append(str(error))
            continue
        if start < previous_end - TIME_EPSILON:
            issues.append(f"{label} overlaps or reverses the preceding region")
        if start > previous_end + TIME_EPSILON:
            issues.append(f"{label} leaves an uncovered analysis-timeline gap")
        if end > duration + TIME_EPSILON:
            issues.append(f"{label} is outside the EDL-derived timeline")
        previous_end = max(previous_end, end)
    if require_full_coverage:
        if regions and float(regions[0].get("start", -1)) > TIME_EPSILON:
            issues.append("edit_regions do not cover the timeline from zero")
        if previous_end < duration - TIME_EPSILON:
            issues.append("edit_regions do not cover the timeline to its end")
    return issues


def build_sequence_clip_mappings(
    edl: list[dict], regions: list[dict], *, media_duration: float | None = None
) -> tuple[SequenceClipMapping, ...]:
    """Intersect keep regions with EDL intervals and assign compact sequence time.

    The returned order is deterministic: input region order, then EDL order.
    A keep region crossing an EDL boundary produces multiple mappings.  The
    same tuple must be used to create both video and audio clips.
    """

    source_segments = build_source_timeline_mapping(edl, media_duration=media_duration)
    timeline_duration = source_segments[-1].timeline_end
    region_issues = validate_edit_regions(regions, timeline_duration)
    if region_issues:
        raise EditProjectValidationError("invalid edit regions", region_issues)

    sequence_cursor = 0.0
    mappings: list[SequenceClipMapping] = []
    for region in regions:
        if region["mode"] != "keep":
            continue
        region_start, region_end = _interval(region, "keep region")
        for segment in source_segments:
            overlap_start = max(region_start, segment.timeline_start)
            overlap_end = min(region_end, segment.timeline_end)
            if overlap_end <= overlap_start + TIME_EPSILON:
                continue
            source_start = segment.source_start + (overlap_start - segment.timeline_start)
            source_end = segment.source_start + (overlap_end - segment.timeline_start)
            duration = overlap_end - overlap_start
            mapping = SequenceClipMapping(
                region_id=str(region["id"]),
                source_start=source_start,
                source_end=source_end,
                timeline_start=overlap_start,
                timeline_end=overlap_end,
                sequence_start=sequence_cursor,
                sequence_end=sequence_cursor + duration,
            )
            mappings.append(mapping)
            sequence_cursor = mapping.sequence_end

    if not mappings:
        raise EditProjectValidationError("edit_regions contain no keep interval")
    _validate_mapping_invariants(mappings)
    return tuple(mappings)


def _validate_mapping_invariants(mappings: tuple[SequenceClipMapping, ...] | list[SequenceClipMapping]) -> None:
    previous_sequence_end = 0.0
    previous_timeline_end = -math.inf
    for index, mapping in enumerate(mappings):
        values = (
            mapping.source_start,
            mapping.source_end,
            mapping.timeline_start,
            mapping.timeline_end,
            mapping.sequence_start,
            mapping.sequence_end,
        )
        if not all(math.isfinite(value) for value in values):
            raise EditProjectValidationError(f"mapping[{index}] contains NaN or Infinity")
        if mapping.source_end <= mapping.source_start + TIME_EPSILON:
            raise EditProjectValidationError(f"mapping[{index}] has invalid source duration")
        if mapping.timeline_end <= mapping.timeline_start + TIME_EPSILON:
            raise EditProjectValidationError(f"mapping[{index}] has invalid timeline duration")
        if mapping.sequence_end <= mapping.sequence_start + TIME_EPSILON:
            raise EditProjectValidationError(f"mapping[{index}] has invalid sequence duration")
        if abs((mapping.source_end - mapping.source_start) - (mapping.timeline_end - mapping.timeline_start)) > TIME_EPSILON:
            raise EditProjectValidationError(f"mapping[{index}] source/timeline duration mismatch")
        if abs(mapping.sequence_start - previous_sequence_end) > TIME_EPSILON:
            raise EditProjectValidationError(f"mapping[{index}] leaves an unintended sequence gap/overlap")
        if mapping.timeline_start < previous_timeline_end - TIME_EPSILON:
            raise EditProjectValidationError(f"mapping[{index}] reverses analysis timeline order")
        previous_sequence_end = mapping.sequence_end
        previous_timeline_end = mapping.timeline_end


def map_timeline_interval_to_sequence(
    start: float, end: float, mappings: tuple[SequenceClipMapping, ...] | list[SequenceClipMapping]
) -> tuple[dict[str, float], ...]:
    """Return kept pieces of an analysis-timeline interval in sequence time."""

    start = _finite_number(start, "timeline interval start", minimum=0.0)
    end = _finite_number(end, "timeline interval end", minimum=0.0)
    if end <= start + TIME_EPSILON:
        raise EditProjectValidationError("timeline interval must have end > start")
    _validate_mapping_invariants(mappings)
    pieces: list[dict[str, float]] = []
    for mapping in mappings:
        overlap_start = max(start, mapping.timeline_start)
        overlap_end = min(end, mapping.timeline_end)
        if overlap_end <= overlap_start + TIME_EPSILON:
            continue
        offset_start = overlap_start - mapping.timeline_start
        offset_end = overlap_end - mapping.timeline_start
        pieces.append(
            {
                "timeline_start": overlap_start,
                "timeline_end": overlap_end,
                "sequence_start": mapping.sequence_start + offset_start,
                "sequence_end": mapping.sequence_start + offset_end,
                "source_start": mapping.source_start + offset_start,
                "source_end": mapping.source_start + offset_end,
            }
        )
    return tuple(pieces)


def validate_edit_project(project: dict) -> list[str]:
    """Return all contract validation issues without mutating the project."""

    issues: list[str] = []
    if not isinstance(project, dict):
        return ["project must be an object"]
    if project.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version must be {SCHEMA_VERSION}")

    project_metadata = project.get("project")
    if not isinstance(project_metadata, dict):
        issues.append("project metadata must be an object")
    else:
        for field in ("id", "name"):
            if not isinstance(project_metadata.get(field), str) or not project_metadata[field].strip():
                issues.append(f"project.{field} must be a non-empty string")

    source_media = project.get("source_media")
    media_by_id: dict[str, dict] = {}
    if not isinstance(source_media, list) or not source_media:
        issues.append("source_media must be a non-empty array")
    else:
        for index, media in enumerate(source_media):
            if not isinstance(media, dict):
                issues.append(f"source_media[{index}] must be an object")
                continue
            media_id = media.get("id")
            if not isinstance(media_id, str) or not media_id.strip():
                issues.append(f"source_media[{index}].id must be a non-empty string")
                continue
            if media_id in media_by_id:
                issues.append(f"duplicate source_media id: {media_id}")
            media_by_id[media_id] = media
            if not isinstance(media.get("path"), str) or not media["path"].strip():
                issues.append(f"source_media[{index}].path must be a non-empty string")
            if media.get("kind") not in {"video", "audio"}:
                issues.append(f"source_media[{index}].kind must be video or audio")
            if "duration" in media:
                try:
                    _finite_number(media["duration"], f"source_media[{index}].duration", minimum=0.0)
                except EditProjectValidationError as error:
                    issues.append(str(error))

    sequence = project.get("sequence")
    sequence_duration = None
    if not isinstance(sequence, dict):
        issues.append("sequence must be an object")
    else:
        for field in ("width", "height", "fps"):
            try:
                _finite_number(sequence[field], f"sequence.{field}", minimum=TIME_EPSILON)
            except (KeyError, EditProjectValidationError) as error:
                issues.append(f"sequence.{field} must be a positive finite number")
        timebase = sequence.get("timebase")
        if not isinstance(timebase, dict):
            issues.append("sequence.timebase must be an object")
        else:
            for field in ("numerator", "denominator"):
                try:
                    _finite_number(timebase[field], f"sequence.timebase.{field}", minimum=1.0)
                except (KeyError, EditProjectValidationError):
                    issues.append(f"sequence.timebase.{field} must be a positive finite number")
        try:
            sequence_duration = _finite_number(sequence["duration"], "sequence.duration", minimum=0.0)
        except (KeyError, EditProjectValidationError) as error:
            issues.append(str(error) if isinstance(error, EditProjectValidationError) else "sequence.duration is required")

    tracks = project.get("tracks")
    tracks_by_id: dict[str, dict] = {}
    clip_ids: set[str] = set()
    max_timeline_end = 0.0
    if not isinstance(tracks, list) or not tracks:
        issues.append("tracks must be a non-empty array")
    else:
        for index, track in enumerate(tracks):
            if not isinstance(track, dict):
                issues.append(f"tracks[{index}] must be an object")
                continue
            track_id = track.get("id")
            if not isinstance(track_id, str) or not track_id.strip():
                issues.append(f"tracks[{index}].id must be a non-empty string")
                continue
            if track_id in tracks_by_id:
                issues.append(f"duplicate track id: {track_id}")
            tracks_by_id[track_id] = track
            if track.get("kind") not in {"video", "audio", "caption", "text"}:
                issues.append(f"tracks[{index}].kind is unsupported")
            if not isinstance(track.get("enabled"), bool):
                issues.append(f"tracks[{index}].enabled must be boolean")
            clips = track.get("clips", [])
            if not isinstance(clips, list):
                issues.append(f"tracks[{index}].clips must be an array")
                continue
            for clip_index, clip in enumerate(clips):
                _validate_clip(
                    clip,
                    f"tracks[{index}].clips[{clip_index}]",
                    track,
                    media_by_id,
                    clip_ids,
                    issues,
                    sequence_duration,
                )
                if isinstance(clip, dict):
                    try:
                        timeline = clip.get("timeline", {})
                        max_timeline_end = max(
                            max_timeline_end,
                            float(timeline.get("start", 0)) + float(timeline.get("duration", 0)),
                        )
                    except (TypeError, ValueError):
                        pass

    captions = project.get("captions")
    caption_ids: set[str] = set()
    if not isinstance(captions, list):
        issues.append("captions must be an array")
    else:
        for index, caption in enumerate(captions):
            label = f"captions[{index}]"
            if not isinstance(caption, dict):
                issues.append(f"{label} must be an object")
                continue
            caption_id = caption.get("id")
            if not isinstance(caption_id, str) or not caption_id.strip():
                issues.append(f"{label}.id must be a non-empty string")
            elif caption_id in caption_ids or caption_id in clip_ids:
                issues.append(f"duplicate clip/caption id: {caption_id}")
            else:
                caption_ids.add(caption_id)
            track_id = caption.get("track_id")
            if track_id not in tracks_by_id or tracks_by_id.get(track_id, {}).get("kind") != "caption":
                issues.append(f"{label}.track_id must reference a caption track")
            if not isinstance(caption.get("text"), str):
                issues.append(f"{label}.text must be a string")
            _validate_time_object(caption.get("timeline"), f"{label}.timeline", issues)
            if isinstance(caption.get("timeline"), dict):
                try:
                    max_timeline_end = max(
                        max_timeline_end,
                        float(caption["timeline"]["start"]) + float(caption["timeline"]["duration"]),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
            if "source" in caption:
                _validate_source_time(caption.get("source"), f"{label}.source", issues, media_by_id)
            words = caption.get("words")
            if words is not None:
                _validate_caption_words(words, label, caption.get("text"), caption.get("timeline"), issues)
            source_word_ids = caption.get("source_word_ids")
            if source_word_ids is not None:
                if (
                    not isinstance(source_word_ids, list)
                    or len(source_word_ids) != 2
                    or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in source_word_ids)
                ):
                    issues.append(f"{label}.source_word_ids must contain the first and last non-negative integer word id")
                elif isinstance(words, list) and words and all(isinstance(word, dict) for word in words):
                    if words[0].get("id") != f"source-word:{source_word_ids[0]}" or words[-1].get("id") != f"source-word:{source_word_ids[1]}":
                        issues.append(f"{label}.source_word_ids does not match the first and last words")
            if not isinstance(caption.get("enabled"), bool):
                issues.append(f"{label}.enabled must be boolean")
            if "style" in caption and not isinstance(caption["style"], dict):
                issues.append(f"{label}.style must be an object")

    metadata = project.get("metadata")
    if not isinstance(metadata, dict):
        issues.append("metadata must be an object")
    else:
        for field in ("source_run_id", "writer_version"):
            if not isinstance(metadata.get(field), str) or not metadata[field].strip():
                issues.append(f"metadata.{field} must be a non-empty string")
        if metadata.get("schema_version") != SCHEMA_VERSION:
            issues.append(f"metadata.schema_version must be {SCHEMA_VERSION}")
        input_hashes = metadata.get("input_hashes")
        if not isinstance(input_hashes, dict) or not input_hashes:
            issues.append("metadata.input_hashes must be a non-empty object")
        elif any(not isinstance(value, str) or len(value) != 64 for value in input_hashes.values()):
            issues.append("metadata.input_hashes values must be SHA-256 hex strings")
        probe = metadata.get("source_media_probe")
        if not isinstance(probe, dict):
            issues.append("metadata.source_media_probe must be an object")
        else:
            for field in ("duration", "width", "height", "fps"):
                try:
                    _finite_number(probe[field], f"metadata.source_media_probe.{field}", minimum=TIME_EPSILON)
                except (KeyError, EditProjectValidationError):
                    issues.append(f"metadata.source_media_probe.{field} must be a positive finite number")

    if sequence_duration is not None:
        if max_timeline_end > sequence_duration + TIME_EPSILON:
            issues.append("sequence.duration is shorter than a timeline item")
        elif max_timeline_end > TIME_EPSILON and abs(sequence_duration - max_timeline_end) > TIME_EPSILON:
            issues.append("sequence.duration does not equal the final timeline end")
    return issues


def _validate_time_object(value: Any, label: str, issues: list[str]) -> None:
    if not isinstance(value, dict):
        issues.append(f"{label} must be an object")
        return
    for field in ("start", "duration"):
        try:
            _finite_number(value[field], f"{label}.{field}", minimum=0.0)
        except (KeyError, EditProjectValidationError):
            issues.append(f"{label}.{field} must be a finite non-negative number")
    try:
        if float(value["duration"]) <= TIME_EPSILON:
            issues.append(f"{label}.duration must be > 0")
    except (KeyError, TypeError, ValueError):
        pass


def _validate_source_time(value: Any, label: str, issues: list[str], media_by_id: dict[str, dict]) -> None:
    _validate_time_object(value, label, issues)
    if isinstance(value, dict) and "media_id" in value:
        media_id = value.get("media_id")
        if media_id not in media_by_id:
            issues.append(f"{label}.media_id references missing source media")


def _validate_caption_words(words: Any, label: str, caption_text: Any, timeline: Any, issues: list[str]) -> None:
    if not isinstance(words, list) or not words:
        issues.append(f"{label}.words must be a non-empty array when present")
        return
    word_ids: set[str] = set()
    texts: list[str] = []
    previous_end: Decimal | None = None
    caption_start: Decimal | None = None
    caption_end: Decimal | None = None
    if isinstance(timeline, dict):
        try:
            caption_start = _decimal_seconds(timeline["start"], f"{label}.timeline.start")
            caption_duration = _decimal_seconds(timeline["duration"], f"{label}.timeline.duration")
            caption_end = caption_start + caption_duration
        except (EditProjectValidationError, KeyError, TypeError, ValueError):
            pass
    for index, word in enumerate(words):
        word_label = f"{label}.words[{index}]"
        if not isinstance(word, dict):
            issues.append(f"{word_label} must be an object")
            continue
        word_id = word.get("id")
        if not isinstance(word_id, str) or not word_id.strip():
            issues.append(f"{word_label}.id must be a non-empty string")
        elif word_id in word_ids:
            issues.append(f"duplicate word id: {word_id}")
        else:
            word_ids.add(word_id)
        word_text = word.get("text")
        if not isinstance(word_text, str):
            issues.append(f"{word_label}.text must be a string")
        else:
            texts.append(word_text)
        _validate_time_object(word.get("timeline"), f"{word_label}.timeline", issues)
        word_timeline = word.get("timeline")
        if isinstance(word_timeline, dict):
            try:
                start = _decimal_seconds(word_timeline["start"], f"{word_label}.timeline.start")
                duration = _decimal_seconds(word_timeline["duration"], f"{word_label}.timeline.duration")
                end = start + duration
                if caption_start is not None and start < caption_start:
                    issues.append(f"{word_label} starts before the caption")
                if caption_end is not None and end > caption_end:
                    issues.append(f"{word_label} ends after the caption")
                if previous_end is not None and start < previous_end:
                    issues.append(f"{word_label} overlaps or reverses the previous word")
                previous_end = end
            except (EditProjectValidationError, KeyError, TypeError, ValueError):
                pass
    if isinstance(caption_text, str) and "".join(texts) != caption_text:
        issues.append(f"{label}.words text does not reconstruct caption.text")


def _validate_clip(
    clip: Any,
    label: str,
    track: dict,
    media_by_id: dict[str, dict],
    clip_ids: set[str],
    issues: list[str],
    sequence_duration: float | None,
) -> None:
    if not isinstance(clip, dict):
        issues.append(f"{label} must be an object")
        return
    clip_id = clip.get("id")
    if not isinstance(clip_id, str) or not clip_id.strip():
        issues.append(f"{label}.id must be a non-empty string")
    elif clip_id in clip_ids:
        issues.append(f"duplicate clip id: {clip_id}")
    else:
        clip_ids.add(clip_id)
    expected_type = {"video": "video_clip", "audio": "audio_clip"}.get(track.get("kind"))
    if expected_type and clip.get("type") != expected_type:
        issues.append(f"{label}.type must be {expected_type}")
    if clip.get("source_media_id") not in media_by_id:
        issues.append(f"{label}.source_media_id references missing source media")
    _validate_time_object(clip.get("timeline"), f"{label}.timeline", issues)
    _validate_source_time(clip.get("source"), f"{label}.source", issues, media_by_id)
    if isinstance(clip.get("timeline"), dict) and isinstance(clip.get("source"), dict):
        try:
            timeline_duration = float(clip["timeline"]["duration"])
            source_duration = float(clip["source"]["duration"])
            if abs(timeline_duration - source_duration) > TIME_EPSILON:
                issues.append(f"{label} timeline/source duration mismatch")
        except (TypeError, ValueError, KeyError):
            pass
    if isinstance(clip.get("source"), dict):
        media = media_by_id.get(clip.get("source_media_id"), {})
        if "duration" in media:
            try:
                source_end = float(clip["source"]["start"]) + float(clip["source"]["duration"])
                if source_end > float(media["duration"]) + TIME_EPSILON:
                    issues.append(f"{label}.source ends beyond source media duration")
            except (TypeError, ValueError, KeyError):
                pass
    if isinstance(clip.get("timeline"), dict) and sequence_duration is not None:
        try:
            timeline_end = float(clip["timeline"]["start"]) + float(clip["timeline"]["duration"])
            if timeline_end > sequence_duration + TIME_EPSILON:
                issues.append(f"{label}.timeline ends beyond sequence duration")
        except (TypeError, ValueError, KeyError):
            pass
    if not isinstance(clip.get("enabled"), bool):
        issues.append(f"{label}.enabled must be boolean")
    if track.get("kind") == "video":
        transform = clip.get("transform")
        if not isinstance(transform, dict):
            issues.append(f"{label}.transform must be an object")
        else:
            for field in ("x", "y", "scale_x", "scale_y", "rotation", "opacity", "anchor_x", "anchor_y"):
                try:
                    value = _finite_number(transform[field], f"{label}.transform.{field}")
                    if field in {"scale_x", "scale_y"} and value <= 0:
                        issues.append(f"{label}.transform.{field} must be > 0")
                    if field == "opacity" and not 0.0 <= value <= 1.0:
                        issues.append(f"{label}.transform.opacity must be between 0 and 1")
                except (KeyError, EditProjectValidationError):
                    issues.append(f"{label}.transform.{field} must be finite")
    if track.get("kind") == "audio":
        try:
            _finite_number(clip["volume"], f"{label}.volume", minimum=0.0)
        except (KeyError, EditProjectValidationError):
            issues.append(f"{label}.volume must be a finite non-negative number")


def require_valid_edit_project(project: dict) -> dict:
    """Validate and return the same object for convenient writer use."""

    issues = validate_edit_project(project)
    if issues:
        raise EditProjectValidationError("invalid edit_project", issues)
    return project
