"""Apply and persist caption editing commands for the v2 timeline project."""

import json
import math
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from timeline_project import (
    _atomic_json_write,
    _effective_word_timing,
    atomic_replace,
    validate_timeline_project,
)


COMMAND_TYPES = {
    "UpdateCaptionText": ("caption_id", "display_text"),
    "MoveCaption": ("caption_id", "delta"),
    "DeleteCaption": ("caption_id",),
    "MoveBoundary": ("left_id", "right_id", "boundary_word_id"),
    "SplitCaption": ("caption_id", "after_word_id"),
    "MergeCaptions": ("left_id", "right_id"),
    "ApplyCaptionStyle": ("caption_ids", "template_id"),
    "SetTimingOverride": ("caption_id", "word_id", "start", "end"),
}


def validate_command_envelope(command: dict) -> dict:
    if not isinstance(command, dict):
        raise ValueError("command must be a JSON object")
    command_type = command.get("type")
    if command_type not in COMMAND_TYPES:
        raise ValueError(f"unsupported command: {command_type}")
    if not isinstance(command.get("command_id"), str) or not command["command_id"].strip():
        raise ValueError("command_id is required")
    if (
        isinstance(command.get("base_revision"), bool)
        or not isinstance(command.get("base_revision"), int)
        or command["base_revision"] < 0
    ):
        raise ValueError("base_revision must be a non-negative integer")
    for field in COMMAND_TYPES[command_type]:
        value = command.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{command_type} requires {field}")
    for field in ("caption_id", "left_id", "right_id", "template_id"):
        if field in command and (
            not isinstance(command[field], str) or not command[field].strip()
        ):
            raise ValueError(f"{field} must be a non-empty string")
    if "display_text" in command and (
        not isinstance(command["display_text"], str) or not command["display_text"].strip()
    ):
        raise ValueError("display_text must be a non-empty string")
    if command_type == "ApplyCaptionStyle" and (
        not isinstance(command["caption_ids"], list) or not command["caption_ids"]
    ):
        raise ValueError("ApplyCaptionStyle caption_ids must be a non-empty list")
    if command_type == "ApplyCaptionStyle" and (
        any(not isinstance(caption_id, str) or not caption_id.strip() for caption_id in command["caption_ids"])
        or len(set(command["caption_ids"])) != len(command["caption_ids"])
    ):
        raise ValueError("ApplyCaptionStyle caption_ids must contain unique non-empty strings")
    for field in ("boundary_word_id", "after_word_id", "word_id"):
        if field in command and (
            isinstance(command[field], bool)
            or not isinstance(command[field], int)
            or command[field] < 0
        ):
            raise ValueError(f"{field} must be a non-negative integer")
    for field in ("start", "end"):
        if field in command:
            try:
                value = float(command[field])
            except (TypeError, ValueError):
                raise ValueError(f"{field} must be a finite number") from None
            if not math.isfinite(value):
                raise ValueError(f"{field} must be a finite number")
    if "delta" in command:
        try:
            delta = float(command["delta"])
        except (TypeError, ValueError):
            raise ValueError("delta must be a finite number") from None
        if not math.isfinite(delta):
            raise ValueError("delta must be a finite number")
    if command_type == "SetTimingOverride" and float(command["end"]) < float(command["start"]):
        raise ValueError("timing override end must be greater than or equal to start")
    if command_type == "SplitCaption":
        supplied = {field for field in ("left_display_text", "right_display_text") if field in command}
        if supplied and supplied != {"left_display_text", "right_display_text"}:
            raise ValueError("SplitCaption requires both left_display_text and right_display_text")
        for field in supplied:
            if not isinstance(command[field], str) or not command[field].strip():
                raise ValueError(f"{field} must be a non-empty string")
    if command_type == "MergeCaptions" and "display_text" in command:
        if not isinstance(command["display_text"], str) or not command["display_text"].strip():
            raise ValueError("MergeCaptions display_text must be a non-empty string")
    return dict(command)


def _caption(project: dict, caption_id: str) -> tuple[int, dict]:
    for index, caption in enumerate(project["captions"]):
        if caption["id"] == caption_id:
            return index, caption
    raise ValueError(f"caption not found: {caption_id}")


def _refresh_word_bound(caption: dict, words: list[dict]) -> None:
    start_id = caption["start_word_id"]
    end_id = caption["end_word_id"]
    caption["source_text"] = "".join(
        str(word.get("text", word.get("word", "")))
        for word in words[start_id : end_id + 1]
    )
    start_time, _ = _effective_word_timing(caption, start_id, words)
    _, end_time = _effective_word_timing(caption, end_id, words)
    offset = float(caption.get("position_offset", 0.0) or 0.0)
    caption["start"] = round(start_time + offset, 6)
    caption["end"] = round(end_time + offset, 6)
    caption["timing_mode"] = (
        "position_override" if offset else
        "word_override" if caption.get("word_timing_overrides") else
        "word_bound"
    )


def _word_index(words: list[dict], word_id: int) -> int:
    """Return the positional index for a stable Whisper word_id."""
    if isinstance(word_id, bool) or not isinstance(word_id, int) or word_id < 0:
        raise ValueError("word_id must be a non-negative integer")
    if word_id >= len(words) or words[word_id].get("word_id") != word_id:
        raise ValueError(f"word_idがWhisper word列に存在しません: {word_id}")
    return word_id


def _is_unedited_caption(caption: dict) -> bool:
    return (
        caption.get("text_status") == "as_transcribed"
        and caption.get("display_text") == caption.get("source_text")
    )


def _timing_overrides(caption: dict) -> list[dict]:
    raw_overrides = caption.get("word_timing_overrides") or []
    if isinstance(raw_overrides, dict):
        raw_overrides = [
            {"word_id": word_id, "start": timing["start"], "end": timing["end"]}
            for word_id, timing in raw_overrides.items()
        ]
    return [
        {
            "word_id": int(override["word_id"]),
            "start": float(override["start"]),
            "end": float(override["end"]),
        }
        for override in raw_overrides
    ]


def _overrides_in_range(caption: dict, start_id: int, end_id: int) -> list[dict]:
    return [
        override
        for override in _timing_overrides(caption)
        if start_id <= override["word_id"] <= end_id
    ]


def _combined_overrides(left: dict, right: dict) -> list[dict]:
    overrides = _timing_overrides(left) + _timing_overrides(right)
    return sorted(overrides, key=lambda override: override["word_id"])


def _set_display_text(caption: dict, display_text: str) -> None:
    caption["display_text"] = display_text
    caption["text_status"] = (
        "as_transcribed" if display_text == caption["source_text"] else "manually_corrected"
    )


def apply_command(project: dict, words: list[dict], command: dict) -> dict:
    command = validate_command_envelope(command)
    current_revision = project.get("revision")
    if isinstance(current_revision, bool) or not isinstance(current_revision, int) or current_revision < 0:
        raise ValueError("project revision must be a non-negative integer")
    if command["base_revision"] != current_revision:
        raise ValueError("revision競合です。projectを再読込してください。")
    result = deepcopy(project)
    command_type = command["type"]

    if command_type == "UpdateCaptionText":
        _, caption = _caption(result, command["caption_id"])
        caption["display_text"] = command["display_text"]
        caption["text_status"] = "manually_corrected"
    elif command_type == "MoveCaption":
        _, caption = _caption(result, command["caption_id"])
        if caption.get("deleted"):
            raise ValueError("削除済み字幕は移動できません。")
        next_offset = round(float(caption.get("position_offset", 0.0) or 0.0) + float(command["delta"]), 6)
        start_time, _ = _effective_word_timing(caption, caption["start_word_id"], words)
        _, end_time = _effective_word_timing(caption, caption["end_word_id"], words)
        if start_time + next_offset < 0:
            raise ValueError("字幕を動画の先頭より前へ移動できません。")
        caption["position_offset"] = next_offset
        caption["start"] = round(start_time + next_offset, 6)
        caption["end"] = round(end_time + next_offset, 6)
        caption["timing_mode"] = "position_override" if next_offset else (
            "word_override" if caption.get("word_timing_overrides") else "word_bound"
        )
    elif command_type == "DeleteCaption":
        _, caption = _caption(result, command["caption_id"])
        if caption.get("deleted"):
            raise ValueError("字幕はすでに削除されています。")
        caption["deleted"] = True
    elif command_type == "ApplyCaptionStyle":
        for caption_id in command["caption_ids"]:
            _, caption = _caption(result, caption_id)
            caption["template_id"] = command["template_id"]
    elif command_type == "MoveBoundary":
        left_index, left = _caption(result, command["left_id"])
        right_index, right = _caption(result, command["right_id"])
        if right_index != left_index + 1 or left["end_word_id"] + 1 != right["start_word_id"]:
            raise ValueError("MoveBoundary applies only to adjacent contiguous captions")
        boundary = command["boundary_word_id"]
        _word_index(words, boundary)
        if boundary < left["start_word_id"] or boundary >= right["end_word_id"]:
            raise ValueError("境界移動で空字幕、word欠落、または重複が発生します。")
        left_was_unedited = _is_unedited_caption(left)
        right_was_unedited = _is_unedited_caption(right)
        combined_overrides = _combined_overrides(left, right)
        left["end_word_id"] = boundary
        right["start_word_id"] = boundary + 1
        left["word_timing_overrides"] = [
            override for override in combined_overrides if override["word_id"] <= boundary
        ]
        right["word_timing_overrides"] = [
            override for override in combined_overrides if override["word_id"] > boundary
        ]
        _refresh_word_bound(left, words)
        _refresh_word_bound(right, words)
        if left_was_unedited:
            _set_display_text(left, left["source_text"])
        if right_was_unedited:
            _set_display_text(right, right["source_text"])
    elif command_type == "SplitCaption":
        index, caption = _caption(result, command["caption_id"])
        after = command["after_word_id"]
        _word_index(words, after)
        if after < caption["start_word_id"] or after >= caption["end_word_id"]:
            raise ValueError("SplitCaption must be before the caption's last word")
        supplied_text = "left_display_text" in command or "right_display_text" in command
        is_edited = (
            caption.get("text_status") != "as_transcribed"
            or caption.get("display_text") != caption.get("source_text")
        )
        if is_edited and not supplied_text:
            raise ValueError("edited captions require both display text parts for SplitCaption")
        if supplied_text and command["left_display_text"] + command["right_display_text"] != caption["display_text"]:
            raise ValueError("SplitCaption display text must be preserved exactly across both parts")
        left = deepcopy(caption)
        right = deepcopy(caption)
        left["end_word_id"] = after
        right["start_word_id"] = after + 1
        right["id"] = f"{caption['id']}_split_r{project['revision'] + 1}"
        existing_ids = {item["id"] for item in result["captions"]}
        suffix = 2
        while right["id"] in existing_ids:
            right["id"] = f"{caption['id']}_split_r{project['revision'] + 1}_{suffix}"
            suffix += 1
        left["word_timing_overrides"] = _overrides_in_range(left, left["start_word_id"], left["end_word_id"])
        right["word_timing_overrides"] = _overrides_in_range(right, right["start_word_id"], right["end_word_id"])
        _refresh_word_bound(left, words)
        _refresh_word_bound(right, words)
        if supplied_text:
            _set_display_text(left, command["left_display_text"])
            _set_display_text(right, command["right_display_text"])
        else:
            _set_display_text(left, left["source_text"])
            _set_display_text(right, right["source_text"])
        result["captions"][index : index + 1] = [left, right]
    elif command_type == "MergeCaptions":
        left_index, left = _caption(result, command["left_id"])
        right_index, right = _caption(result, command["right_id"])
        if right_index != left_index + 1 or left["end_word_id"] + 1 != right["start_word_id"]:
            raise ValueError("MergeCaptions applies only to adjacent word ranges")
        merged_display_text = command.get(
            "display_text", str(left["display_text"]) + str(right["display_text"])
        )
        left["end_word_id"] = right["end_word_id"]
        left["word_timing_overrides"] = _combined_overrides(left, right)
        _refresh_word_bound(left, words)
        _set_display_text(left, merged_display_text)
        result["captions"].pop(right_index)
    elif command_type == "SetTimingOverride":
        _, caption = _caption(result, command["caption_id"])
        word_id = command["word_id"]
        _word_index(words, word_id)
        if word_id < caption["start_word_id"] or word_id > caption["end_word_id"]:
            raise ValueError("timing override word_id must belong to the caption")
        replacement = {
            "word_id": word_id,
            "start": round(float(command["start"]), 6),
            "end": round(float(command["end"]), 6),
        }
        overrides = [
            override
            for override in _timing_overrides(caption)
            if int(override["word_id"]) != word_id
        ]
        overrides.append(replacement)
        caption["word_timing_overrides"] = sorted(overrides, key=lambda item: int(item["word_id"]))
        _refresh_word_bound(caption, words)

    result["revision"] = current_revision + 1
    issues = validate_timeline_project(result, words)
    if issues:
        raise ValueError("invalid command result: " + "; ".join(issues))
    return result


class CaptionCommandService:
    """Persist project snapshots and command history with undo/redo support."""

    def __init__(self, project_path, words, history_path=None, history_limit=100):
        self.project_path = Path(project_path)
        self.history_path = Path(history_path or self.project_path.with_name("caption_history.jsonl"))
        self.words = deepcopy(words)
        self.history_limit = max(100, int(history_limit))
        self._history = self._load_history()

    def _load_project(self):
        return json.loads(self.project_path.read_text(encoding="utf-8"))

    def _load_history(self):
        if not self.history_path.exists():
            return {"cursor": 0, "entries": []}
        records = [
            json.loads(line)
            for line in self.history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not records or records[-1].get("record_type") != "state":
            raise ValueError("caption history has no state record")
        entries = [record["entry"] for record in records[:-1] if record.get("record_type") == "command"]
        history = {"cursor": records[-1].get("cursor"), "entries": entries}
        if not isinstance(history.get("entries"), list) or not isinstance(history.get("cursor"), int):
            raise ValueError("invalid caption history")
        return history

    def _write_history(self, history):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.history_path.with_suffix(self.history_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            for entry in history["entries"]:
                file.write(json.dumps({"record_type": "command", "entry": entry}, ensure_ascii=False) + "\n")
            file.write(json.dumps({"record_type": "state", "cursor": history["cursor"]}, ensure_ascii=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        atomic_replace(temporary, self.history_path)

    def _save_state(self, project, history):
        _atomic_json_write(self.project_path, project)
        self._write_history(history)
        self._history = history

    def execute(self, command):
        before = self._load_project()
        after = apply_command(before, self.words, command)
        entries = self._history["entries"][: self._history["cursor"]]
        entries.append({
            "command": validate_command_envelope(command),
            "before": before,
            "after": after,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(entries) > self.history_limit:
            entries = entries[-self.history_limit :]
        history = {"cursor": len(entries), "entries": entries}
        self._save_state(after, history)
        return deepcopy(after)

    def undo(self):
        if self._history["cursor"] == 0:
            raise ValueError("no operation to undo")
        history = deepcopy(self._history)
        history["cursor"] -= 1
        project = deepcopy(history["entries"][history["cursor"]]["before"])
        current = self._load_project()
        project["revision"] = int(current.get("revision", 0)) + 1
        self._save_state(project, history)
        return deepcopy(project)

    def redo(self):
        if self._history["cursor"] >= len(self._history["entries"]):
            raise ValueError("no operation to redo")
        history = deepcopy(self._history)
        project = deepcopy(history["entries"][history["cursor"]]["after"])
        current = self._load_project()
        project["revision"] = int(current.get("revision", 0)) + 1
        history["cursor"] += 1
        self._save_state(project, history)
        return deepcopy(project)
