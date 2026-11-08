"""Read-only adapter from existing clip_automation artifacts to edit_project/v1."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from edit_project import (
    SCHEMA_VERSION,
    EditProjectValidationError,
    SequenceClipMapping,
    TIME_EPSILON,
    build_sequence_clip_mappings,
    map_timeline_interval_to_sequence,
    require_valid_edit_project,
)
from caption_text import normalize_caption_display_text, resolve_display_mode
from timeline_project import _effective_word_timing, validate_timeline_project, validate_transcript_words


WRITER_VERSION = "clip_automation.edit_project_v1/3"
DiagnosticSeverity = Literal["warning", "unsupported", "error"]


@dataclass(frozen=True)
class Diagnostic:
    severity: DiagnosticSeverity
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.details:
            result["details"] = self.details
        return result


class EditProjectBuildError(ValueError):
    def __init__(self, message: str, diagnostics: list[Diagnostic] | tuple[Diagnostic, ...]):
        self.diagnostics = tuple(diagnostics)
        super().__init__(message)


@dataclass(frozen=True)
class EditProjectBuildResult:
    project: dict
    diagnostics: tuple[Diagnostic, ...]


def _round_time(value: float) -> float:
    rounded = round(float(value), 6)
    return 0.0 if abs(rounded) < 1e-9 else rounded


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json(path: Path, diagnostics: list[Diagnostic], artifact_name: str) -> Any:
    if not path.is_file():
        diagnostics.append(Diagnostic("error", "missing_input", f"Missing required artifact: {artifact_name}", artifact_name))
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        diagnostics.append(Diagnostic("error", "invalid_json", f"Cannot read {artifact_name}: {error}", artifact_name))
        return None


def _probe(source_media_probe: dict[str, Any], diagnostics: list[Diagnostic]) -> dict[str, Any] | None:
    if not isinstance(source_media_probe, dict):
        diagnostics.append(Diagnostic("error", "missing_probe", "source media probe must be an object", "source_media_probe"))
        return None
    required = ("duration", "width", "height", "fps")
    result = dict(source_media_probe)
    for field in required:
        try:
            value = float(result[field])
            if not math.isfinite(value) or value <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            diagnostics.append(Diagnostic("error", "invalid_probe", f"source media probe requires positive finite {field}", f"source_media_probe.{field}"))
    return result


def _source_path(run_dir: Path, caption_project: dict, override: str | Path | None) -> Path | None:
    raw = override if override is not None else caption_project.get("source_path")
    if not raw:
        return None
    candidate = Path(str(raw))
    return candidate.resolve() if candidate.is_absolute() else (run_dir / candidate).resolve()


def _contract_media_path(contract_dir: Path, source_path: Path) -> str:
    """Return a portable path relative to the edit_project directory."""
    try:
        relative = Path(os.path.relpath(source_path, contract_dir))
    except ValueError as error:
        raise ValueError(
            "source media and edit_project are on different drives; a portable relative path cannot be generated"
        ) from error
    normalized = relative.as_posix()
    if Path(normalized).is_absolute() or normalized.startswith("//"):
        raise ValueError("source media path cannot be represented as a portable relative path")
    return normalized


def _contract_directory(
    run_dir: Path,
    *,
    contract_path: str | Path | None,
    contract_dir: str | Path | None,
) -> Path:
    if contract_path is not None and contract_dir is not None:
        raise ValueError("contract_path and contract_dir are mutually exclusive")
    if contract_path is not None:
        return Path(contract_path).resolve().parent
    if contract_dir is not None:
        return Path(contract_dir).resolve()
    # Phase 1 compatibility: callers that only build a dict historically used
    # the run directory as the implicit output base. New planning callers must
    # pass contract_path or contract_dir.
    return run_dir


def _clip_time(mapping: SequenceClipMapping) -> tuple[dict[str, float], dict[str, float]]:
    duration = _round_time(mapping.duration)
    return (
        {"start": _round_time(mapping.sequence_start), "duration": duration},
        {"start": _round_time(mapping.source_start), "duration": duration},
    )


def _video_clip(mapping: SequenceClipMapping, index: int, media_id: str) -> dict[str, Any]:
    timeline, source = _clip_time(mapping)
    return {
        "id": f"vclip_{index:06d}",
        "type": "video_clip",
        "source_media_id": media_id,
        "timeline": timeline,
        "source": source,
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


def _audio_clip(mapping: SequenceClipMapping, index: int, media_id: str) -> dict[str, Any]:
    timeline, source = _clip_time(mapping)
    return {
        "id": f"aclip_{index:06d}",
        "type": "audio_clip",
        "source_media_id": media_id,
        "timeline": timeline,
        "source": source,
        "volume": 1.0,
        "enabled": True,
    }


def _word_timing(word: dict, caption: dict, words: list[dict]) -> tuple[float, float]:
    start, end = _effective_word_timing(caption, int(word["word_id"]), words)
    offset = float(caption.get("position_offset", 0.0) or 0.0)
    return start + offset, end + offset


def _pieces_duration(pieces: tuple[dict[str, float], ...]) -> float:
    return sum(piece["timeline_end"] - piece["timeline_start"] for piece in pieces)


def _point_region_mode(point: float, regions: list[dict[str, Any]]) -> str | None:
    """Resolve a zero-duration word against half-open keep/delete regions."""
    for region in regions:
        start = float(region["start"])
        end = float(region["end"])
        if start - TIME_EPSILON <= point < end:
            return str(region["mode"])
    if regions and abs(point - float(regions[-1]["end"])) <= TIME_EPSILON:
        return str(regions[-1]["mode"])
    return None


def _caption_word_entries(
    run_words: list[dict],
    all_words: list[dict],
    caption: dict,
    caption_start: float,
    caption_duration: float,
    mappings: tuple[SequenceClipMapping, ...],
    caption_text: str,
    diagnostics: list[Diagnostic],
    diagnostic_path: str,
) -> list[dict[str, Any]] | None:
    """Build optional editor word metadata from already validated sequence mappings."""

    entries: list[dict[str, Any]] = []
    zero_word_ids = [
        int(word["word_id"])
        for word in run_words
        if abs(_word_timing(word, caption, all_words)[1] - _word_timing(word, caption, all_words)[0]) <= TIME_EPSILON
    ]
    if zero_word_ids:
        diagnostics.append(
            Diagnostic(
                "warning",
                "caption_word_metadata_omitted_zero_duration_word",
                "Caption editor word metadata was omitted because one or more source words have zero duration.",
                diagnostic_path,
                {"source_word_ids": zero_word_ids, "count": len(zero_word_ids)},
            )
        )
        return None
    text_status = caption.get("text_status")
    # 句読点の表示設定(旧仕様/非表示)は、字幕の表示文字列がsourceからどの規則で導出できるかで決まる。
    display_mode = resolve_display_mode(
        "".join(str(word.get("text", word.get("word", ""))) for word in run_words),
        caption.get("display_text", caption.get("text", "")),
    )
    word_ids: set[str] = set()
    previous_end: float | None = None
    for word in run_words:
        word_start, word_end = _word_timing(word, caption, all_words)
        pieces = map_timeline_interval_to_sequence(word_start, word_end, mappings)
        kept_duration = _pieces_duration(pieces)
        if not pieces or abs(kept_duration - (word_end - word_start)) > 1e-6:
            return None
        word_id = f"source-word:{word['word_id']}"
        sequence_start = _round_time(pieces[0]["sequence_start"])
        sequence_end = _round_time(pieces[-1]["sequence_end"])
        if (
            not word_id.strip()
            or word_id in word_ids
            or not math.isfinite(sequence_start)
            or not math.isfinite(sequence_end)
            or sequence_start < caption_start
            or sequence_end <= sequence_start
            or sequence_end > caption_start + caption_duration
            or (previous_end is not None and sequence_start < previous_end)
        ):
            return None
        word_ids.add(word_id)
        previous_end = sequence_end
        raw_text = str(word.get("text", word.get("word", "")))
        if text_status == "display_normalized":
            display_word_text = normalize_caption_display_text(raw_text, display_mode)
        elif text_status == "as_transcribed":
            display_word_text = raw_text
        else:
            # manually_correctedはtranscript word列から安全に再構成できない。
            display_word_text = raw_text
        if not display_word_text:
            diagnostics.append(
                Diagnostic(
                    "warning",
                    "caption_word_metadata_omitted_empty_display_word",
                    "A source punctuation-only word has no display token; editor word metadata was omitted.",
                    diagnostic_path,
                    {"word_id": int(word["word_id"])},
                )
            )
            return None
        entries.append({
            "id": word_id,
            "text": display_word_text,
            "timeline": {
                "start": sequence_start,
                "duration": _round_time(sequence_end - sequence_start),
            },
        })
    if not entries or "".join(entry["text"] for entry in entries) != caption_text:
        diagnostics.append(
            Diagnostic(
                "warning",
                "caption_word_metadata_omitted_text_mismatch",
                "Caption display text does not exactly reconstruct from transcript words; editor word metadata was omitted.",
                diagnostic_path,
                {"word_count": len(entries), "caption_text_length": len(caption_text)},
            )
        )
        return None
    return entries


def _caption_runs(
    caption: dict,
    words: list[dict],
    mappings: tuple[SequenceClipMapping, ...],
    regions: list[dict[str, Any]],
    diagnostics: list[Diagnostic],
) -> list[tuple[list[dict], tuple[dict[str, float], ...]]]:
    start_id = int(caption["start_word_id"])
    end_id = int(caption["end_word_id"])
    caption_words = words[start_id : end_id + 1]
    states: list[tuple[dict, tuple[dict[str, float], ...], bool]] = []
    for index, word in enumerate(caption_words):
        word_start, word_end = _word_timing(word, caption, words)
        is_zero_duration = abs(word_end - word_start) <= TIME_EPSILON
        if is_zero_duration:
            point_mode = _point_region_mode(word_start, regions)
            if point_mode is None:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "zero_word_region_unresolved",
                        "A zero-duration source word is outside the covered keep/delete regions.",
                        f"captions[{caption.get('id')}].words[{word['word_id']}]",
                    )
                )
                kept = False
            else:
                kept = point_mode == "keep"
            states.append((word, (), kept))
            continue
        pieces = map_timeline_interval_to_sequence(word_start, word_end, mappings)
        kept_duration = _pieces_duration(pieces)
        word_duration = word_end - word_start
        if pieces and abs(kept_duration - word_duration) > 1e-6:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "word_crosses_delete_boundary",
                    "A transcript word intersects a keep/delete boundary and cannot be split safely.",
                    f"captions[{caption.get('id')}].words[{start_id + index}]",
                )
            )
        states.append((word, pieces, bool(pieces) and abs(kept_duration - word_duration) <= 1e-6))

    runs: list[tuple[list[dict], tuple[dict[str, float], ...]]] = []
    current_words: list[dict] = []
    current_pieces: list[dict[str, float]] = []
    previous_word_end: float | None = None
    previous_kept = False
    for word, pieces, kept in states:
        word_start, word_end = _word_timing(word, caption, words)
        is_zero_duration = abs(word_end - word_start) <= TIME_EPSILON
        joins_previous = False
        if is_zero_duration and kept:
            joins_previous = bool(
                current_words
                and previous_word_end is not None
                and word_start <= previous_word_end + TIME_EPSILON
            )
        elif current_words and previous_word_end is not None and kept and previous_kept:
            between_start = previous_word_end
            between_end = word_start
            if between_end <= between_start + 1e-6:
                joins_previous = True
            else:
                between_pieces = map_timeline_interval_to_sequence(between_start, between_end, mappings)
                joins_previous = abs(_pieces_duration(between_pieces) - (between_end - between_start)) <= 1e-6
        if kept and (not current_words or joins_previous):
            current_words.append(word)
            current_pieces.extend(pieces)
        elif kept:
            runs.append((current_words, tuple(current_pieces)))
            current_words = [word]
            current_pieces = list(pieces)
        elif current_words:
            runs.append((current_words, tuple(current_pieces)))
            current_words = []
            current_pieces = []
        previous_word_end = word_end
        previous_kept = kept
    if current_words:
        runs.append((current_words, tuple(current_pieces)))
    return runs


def _caption_entries(
    caption_project: dict,
    words: list[dict],
    mappings: tuple[SequenceClipMapping, ...],
    regions: list[dict[str, Any]],
    media_id: str,
    diagnostics: list[Diagnostic],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for caption_index, caption in enumerate(caption_project["captions"]):
        if caption.get("deleted"):
            continue
        runs = _caption_runs(caption, words, mappings, regions, diagnostics)
        if not runs:
            continue
        caption_word_count = int(caption["end_word_id"]) - int(caption["start_word_id"]) + 1
        kept_word_count = sum(len(run_words) for run_words, _ in runs)
        text_status = caption.get("text_status")
        if (kept_word_count < caption_word_count or len(runs) > 1) and text_status not in {"as_transcribed", "display_normalized", None}:
            diagnostics.append(
                Diagnostic(
                    "unsupported",
                    "caption_split_unsafe",
                    "A manually corrected caption crosses a deleted interval and cannot be split without character-proportional guessing.",
                    f"captions[{caption_index}]",
                )
            )
            continue
        for part_index, (run_words, pieces) in enumerate(runs, 1):
            if not pieces:
                if any(
                    abs(_word_timing(word, caption, words)[1] - _word_timing(word, caption, words)[0]) <= TIME_EPSILON
                    for word in run_words
                ):
                    diagnostics.append(
                        Diagnostic(
                            "unsupported",
                            "caption_zero_duration_only_run",
                            "A caption run contains only zero-duration words and cannot be rendered safely.",
                            f"captions[{caption_index}]",
                            {"source_word_ids": [int(word["word_id"]) for word in run_words]},
                        )
                    )
                continue
            source_text = "".join(str(word.get("text", word.get("word", ""))) for word in run_words)
            text_status = caption.get("text_status")
            if text_status == "as_transcribed":
                display_text = source_text
            elif text_status == "display_normalized":
                display_text = normalize_caption_display_text(
                    source_text,
                    resolve_display_mode(source_text, caption.get("display_text", caption.get("text", ""))),
                )
            elif kept_word_count < caption_word_count or len(runs) > 1:
                display_text = source_text
            else:
                display_text = str(caption.get("display_text", source_text))
            sequence_start = pieces[0]["sequence_start"]
            sequence_end = pieces[-1]["sequence_end"]
            caption_id = str(caption["id"])
            if len(runs) > 1:
                caption_id = f"{caption_id}__part_{part_index:02d}"
            sequence_start = _round_time(sequence_start)
            sequence_end = _round_time(sequence_end)
            timeline_duration = _round_time(sequence_end - sequence_start)
            entry: dict[str, Any] = {
                "id": caption_id,
                "track_id": "C1",
                "text": display_text,
                "timeline": {
                    "start": sequence_start,
                    "duration": timeline_duration,
                },
                "style": {"template_id": str(caption.get("template_id") or "")},
                "enabled": True,
                "source_caption_id": str(caption["id"]),
                "source_word_ids": [int(run_words[0]["word_id"]), int(run_words[-1]["word_id"])],
                "source_text": source_text,
            }
            word_entries = _caption_word_entries(
                run_words,
                words,
                caption,
                sequence_start,
                timeline_duration,
                mappings,
                display_text,
                diagnostics,
                f"captions[{caption_index}].words",
            )
            if word_entries is not None:
                entry["words"] = word_entries
            source_contiguous = all(
                abs(pieces[index]["source_end"] - pieces[index + 1]["source_start"]) <= 1e-6
                for index in range(len(pieces) - 1)
            )
            if source_contiguous:
                entry["source"] = {
                    "media_id": media_id,
                    "start": _round_time(pieces[0]["source_start"]),
                    "duration": _round_time(pieces[-1]["source_end"] - pieces[0]["source_start"]),
                }
            elif len(pieces) > 1:
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "caption_source_discontinuous",
                        "Caption source timing crosses multiple source clips; source field omitted while sequence timing is preserved.",
                        f"captions[{caption_index}]",
                    )
                )
            entries.append(entry)
    return entries


def build_edit_project_from_artifacts(
    run_dir: str | Path,
    *,
    source_media_probe: dict[str, Any],
    source_media_path: str | Path | None = None,
    project_name: str | None = None,
    writer_version: str = WRITER_VERSION,
    regions: list[dict[str, Any]] | None = None,
    source_run_id: str | None = None,
    project_id: str | None = None,
    additional_input_hashes: dict[str, str] | None = None,
    sequence_output: dict[str, Any] | None = None,
    contract_path: str | Path | None = None,
    contract_dir: str | Path | None = None,
) -> EditProjectBuildResult:
    """Build an edit_project from a run without writing to that run."""

    directory = Path(run_dir).resolve()
    diagnostics: list[Diagnostic] = []
    if not directory.is_dir():
        raise EditProjectBuildError(f"run directory does not exist: {directory}", [Diagnostic("error", "missing_run", "run directory does not exist", str(directory))])

    artifact_paths = {
        "caption_project.v2.json": directory / "caption_project.v2.json",
        "transcript_words.json": directory / "transcript_words.json",
        "edl.json": directory / "edl.json",
    }
    if regions is None:
        artifact_paths["edit_regions.json"] = directory / "edit_regions.json"
    loaded = {name: _load_json(path, diagnostics, name) for name, path in artifact_paths.items()}
    if any(item is None for item in loaded.values()):
        raise EditProjectBuildError("required edit_project input is missing or invalid", diagnostics)

    probe = _probe(source_media_probe, diagnostics)
    caption_project = loaded["caption_project.v2.json"]
    words = loaded["transcript_words.json"]
    if not isinstance(caption_project, dict) or not isinstance(words, list):
        diagnostics.append(Diagnostic("error", "invalid_input_shape", "caption project or transcript words has an invalid shape"))
        raise EditProjectBuildError("invalid edit_project input shape", diagnostics)
    word_issues = validate_transcript_words(words)
    project_issues = validate_timeline_project(caption_project, words)
    for issue in word_issues:
        diagnostics.append(Diagnostic("error", "invalid_transcript_words", issue, "transcript_words.json"))
    for issue in project_issues:
        diagnostics.append(Diagnostic("error", "invalid_caption_project", issue, "caption_project.v2.json"))

    source_path = _source_path(directory, caption_project, source_media_path)
    if source_path is None or not source_path.is_file():
        diagnostics.append(Diagnostic("error", "missing_source_media", "source media file does not exist", str(source_path or "source_path")))
    if probe is None:
        pass
    if any(item.severity == "error" for item in diagnostics):
        raise EditProjectBuildError("invalid edit_project input", diagnostics)

    assert source_path is not None
    assert probe is not None
    media_duration = float(probe["duration"])
    regions_input = loaded["edit_regions.json"] if regions is None else regions
    try:
        mappings = build_sequence_clip_mappings(loaded["edl.json"], regions_input, media_duration=media_duration)
    except (EditProjectValidationError, TypeError, KeyError, ValueError) as error:
        diagnostics.append(Diagnostic("error", "invalid_time_mapping", str(error), "edl.json/edit_regions.json"))
        raise EditProjectBuildError("cannot build source/timeline mapping", diagnostics) from error

    input_hashes: dict[str, str] = {}
    for name, path in artifact_paths.items():
        input_hashes[name] = _sha256(path)
    input_hashes[f"source_media:{source_path.name}"] = _sha256(source_path)
    if regions is not None:
        input_hashes["derived:planning_regions.json"] = _sha256_json(regions)
    for name, digest in (additional_input_hashes or {}).items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"additional input hash is invalid: {name}")
        input_hashes[str(name)] = digest

    media_id = "media_main"
    try:
        contract_base = _contract_directory(
            directory,
            contract_path=contract_path,
            contract_dir=contract_dir,
        )
        contract_media_path = _contract_media_path(contract_base, source_path)
    except ValueError as error:
        diagnostics.append(
            Diagnostic(
                "error",
                "source_media_path_not_portable",
                str(error),
                "source_media[0].path",
                {"contract_directory": str(contract_base) if "contract_base" in locals() else None, "source_path": str(source_path)},
            )
        )
        raise EditProjectBuildError("cannot generate portable source media path", diagnostics) from error
    output = dict(caption_project["output"])
    if sequence_output is not None:
        output.update(sequence_output)
    width = int(float(output["width"]))
    height = int(float(output["height"]))
    fps = float(output["fps"])
    video_clips = [_video_clip(mapping, index, media_id) for index, mapping in enumerate(mappings, 1)]
    audio_clips = [_audio_clip(mapping, index, media_id) for index, mapping in enumerate(mappings, 1)]
    captions = _caption_entries(caption_project, words, mappings, regions_input, media_id, diagnostics)
    if any(item.severity in {"error", "unsupported"} for item in diagnostics):
        raise EditProjectBuildError("caption mapping is not safe", diagnostics)

    project: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": {
            "id": project_id or directory.name,
            "name": project_name or project_id or directory.name,
        },
        "source_media": [
            {
                "id": media_id,
                "path": contract_media_path,
                "kind": "video",
                "sha256": input_hashes[f"source_media:{source_path.name}"],
                **dict(sorted(probe.items())),
            }
        ],
        "sequence": {
            "id": "sequence_main",
            "width": width,
            "height": height,
            "fps": fps,
            "timebase": {"numerator": 1, "denominator": 1000},
            "duration": _round_time(mappings[-1].sequence_end),
        },
        "tracks": [
            {"id": "V1", "kind": "video", "name": "Video 1", "enabled": True, "locked": False, "clips": video_clips},
            {"id": "A1", "kind": "audio", "name": "Audio 1", "enabled": True, "locked": False, "clips": audio_clips},
            {"id": "C1", "kind": "caption", "name": "Captions", "enabled": True, "locked": False, "clips": []},
        ],
        "captions": captions,
        "metadata": {
            "source_run_id": source_run_id or directory.name,
            "writer_version": writer_version,
            "schema_version": SCHEMA_VERSION,
            "input_hashes": dict(sorted(input_hashes.items())),
            "source_media_probe": dict(sorted(probe.items())),
            "diagnostics": [diagnostic.as_dict() for diagnostic in diagnostics],
        },
    }
    try:
        require_valid_edit_project(project)
    except EditProjectValidationError as error:
        diagnostics.append(Diagnostic("error", "generated_project_invalid", str(error), "edit_project.json", {"issues": list(error.issues)}))
        raise EditProjectBuildError("generated edit_project failed validation", diagnostics) from error
    return EditProjectBuildResult(project=project, diagnostics=tuple(diagnostics))
