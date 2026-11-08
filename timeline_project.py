"""Timeline Editor v1 の字幕プロジェクト v2 契約と v1 移行。

このモジュールは既存の ``caption_project.json`` を変更しない。v2 は不変の
word 正本を参照し、caption は連続した word ID 範囲だけを所有する。
"""

import json
import math
import os
import time
from copy import deepcopy
from pathlib import Path

from caption_text import TEXT_STATUSES, is_display_normalization


V2_SCHEMA_VERSION = 2


def atomic_replace(source: Path, target: Path, attempts: int = 4) -> None:
    """Windowsの短時間の共有違反だけを限定retryしてatomic replaceする。"""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _atomic_json_write(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    atomic_replace(temporary, path)


def _word_text(word: dict) -> str:
    return str(word.get("text", word.get("word", "")))


def _normalize_text(value: str) -> str:
    return "".join(str(value).split())


def _iter_timing_overrides(caption: dict):
    """Yield normalized ``(word_id, start, end)`` timing overrides."""
    raw_overrides = caption.get("word_timing_overrides") or []
    if isinstance(raw_overrides, dict):
        for word_id, timing in raw_overrides.items():
            yield int(word_id), float(timing["start"]), float(timing["end"])
        return
    for override in raw_overrides:
        yield int(override["word_id"]), float(override["start"]), float(override["end"])


def _effective_word_timing(caption: dict, word_id: int, words: list[dict]) -> tuple[float, float]:
    for override_word_id, start, end in _iter_timing_overrides(caption):
        if override_word_id == word_id:
            return start, end
    word = words[word_id]
    return float(word["timeline_start"]), float(word["timeline_end"])


def build_transcript_words(raw_segments: list[dict]) -> list[dict]:
    """Whisper segment 出力を、v2 が参照する不変 word schema へ変換する。"""
    result = []
    for segment in raw_segments:
        source_segment_start = float(segment.get("source_start", segment.get("start", 0.0)))
        timeline_segment_start = float(segment.get("timeline_start", segment.get("start", 0.0)))
        for raw_word in segment.get("words") or []:
            text = _word_text(raw_word)
            source_start = float(raw_word.get("source_start", raw_word.get("start", source_segment_start)))
            source_end = float(raw_word.get("source_end", raw_word.get("end", source_start)))
            timeline_start = float(raw_word.get("timeline_start", raw_word.get("start", timeline_segment_start)))
            timeline_end = float(raw_word.get("timeline_end", raw_word.get("end", timeline_start)))
            result.append(
                {
                    "word_id": len(result),
                    "text": text,
                    "probability": raw_word.get("probability"),
                    "source_start": round(source_start, 6),
                    "source_end": round(source_end, 6),
                    "timeline_start": round(timeline_start, 6),
                    "timeline_end": round(timeline_end, 6),
                }
            )
    issues = validate_transcript_words(result)
    if issues:
        raise ValueError("word schemaが不正です: " + "; ".join(issues))
    return result


def validate_transcript_words(words: list[dict]) -> list[str]:
    issues = []
    previous_end = -1.0
    for expected_id, word in enumerate(words):
        if word.get("word_id") != expected_id:
            issues.append(f"word_idが連番ではありません: {word.get('word_id')}")
        if not _normalize_text(_word_text(word)):
            issues.append(f"word {expected_id} の本文が空です")
        try:
            start = float(word["timeline_start"])
            end = float(word["timeline_end"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"word {expected_id} のtimeline時刻が不正です")
            continue
        if end < start:
            issues.append(f"word {expected_id} の終了時刻が開始時刻より前です")
        if start < previous_end:
            issues.append(f"word {expected_id} のtimeline時刻が逆行しています")
        previous_end = max(previous_end, end)
    return issues


def _caption_from_word_range(v1_caption: dict, caption_number: int, start_id: int, end_id: int, words: list[dict]) -> dict:
    source_text = "".join(_word_text(word) for word in words[start_id : end_id + 1])
    display_text = str(v1_caption.get("text", "")).strip()
    result = {
        "id": f"cap_{caption_number:06d}",
        "start_word_id": start_id,
        "end_word_id": end_id,
        "start": words[start_id]["timeline_start"],
        "end": words[end_id]["timeline_end"],
        "source_text": source_text,
        "display_text": display_text,
        "timing_mode": "word_bound",
        "word_timing_overrides": [],
        "template_id": str(v1_caption.get("template_id") or ""),
    }
    # Legacy v1 may trim leading whitespace while retaining it in source words.
    # Such projects remain loadable without claiming the new status semantics.
    if display_text == source_text:
        result["text_status"] = "as_transcribed"
    return result


def migrate_v1_project(v1_project: dict, words: list[dict]) -> dict:
    """v1 の表示文字列を word 列へ厳密に対応付けて、新しい v2 dict を返す。"""
    if v1_project.get("schema_version") != 1:
        raise ValueError("v1字幕プロジェクトだけを移行できます。")
    word_issues = validate_transcript_words(words)
    if word_issues:
        raise ValueError("word schemaが不正です: " + "; ".join(word_issues))
    captions = v1_project.get("captions")
    if not isinstance(captions, list) or not captions:
        raise ValueError("v1字幕プロジェクトに字幕がありません。")

    migrated_captions = []
    cursor = 0
    for index, caption in enumerate(captions, 1):
        target = _normalize_text(caption.get("text", ""))
        if not target:
            raise ValueError(f"字幕{index}の本文が空のため対応付けできません。")
        assembled = ""
        start_id = cursor
        while cursor < len(words) and len(assembled) < len(target):
            assembled += _normalize_text(_word_text(words[cursor]))
            cursor += 1
        if assembled != target:
            raise ValueError(f"字幕{index}をWhisper word列へ対応付けできません。v1を変更せず手動確認してください。")
        migrated_captions.append(_caption_from_word_range(caption, index, start_id, cursor - 1, words))
    if cursor != len(words):
        raise ValueError("末尾のWhisper wordが字幕へ対応付けできません。v1を変更せず手動確認してください。")

    project = {
        "schema_version": V2_SCHEMA_VERSION,
        "revision": 0,
        "run_dir": str(v1_project.get("run_dir", "")),
        "source_path": str(v1_project.get("source_path", "")),
        "edl_path": str(v1_project.get("edl_path", "")),
        "output": deepcopy(v1_project.get("output", {})),
        "caption_settings": deepcopy(v1_project.get("caption_settings", {})),
        "default_template_id": str(v1_project.get("default_template_id", "standard")),
        "captions": migrated_captions,
    }
    issues = validate_timeline_project(project, words)
    if issues:
        raise ValueError("v2移行結果が不正です: " + "; ".join(issues))
    return project


def build_native_v2_project(native_project: dict, words: list[dict]) -> dict:
    """明示されたcaption word ownershipからnative v2 projectを構築する。

    production caption経路では、表示文字列から範囲を再推測しない。
    ``start_word_id``/``end_word_id``と``source_text``を入力正本として検証する。
    """
    word_issues = validate_transcript_words(words)
    if word_issues:
        raise ValueError("word schemaが不正です: " + "; ".join(word_issues))
    captions = native_project.get("captions")
    if not isinstance(captions, list) or not captions:
        raise ValueError("native字幕プロジェクトに字幕がありません。")

    native_captions = []
    expected_word_id = 0
    for index, caption in enumerate(captions, 1):
        try:
            start_id = int(caption["start_word_id"])
            end_id = int(caption["end_word_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"native字幕{index}に明示的なword範囲がありません。") from error
        if start_id != expected_word_id or start_id < 0 or end_id < start_id or end_id >= len(words):
            raise ValueError(f"native字幕{index}のword範囲が連続していません。")
        source_text = "".join(_word_text(word) for word in words[start_id : end_id + 1])
        if caption.get("source_text") != source_text:
            raise ValueError(f"native字幕{index}のsource_textがword正本と一致しません。")
        display_text = str(caption.get("text", caption.get("display_text", "")))
        if not display_text:
            raise ValueError(f"native字幕{index}のdisplay_textが空です。")
        status = caption.get("text_status")
        if status not in TEXT_STATUSES:
            raise ValueError(f"native字幕{index}のtext_statusが不正です。")
        if status == "as_transcribed" and display_text != source_text:
            raise ValueError(f"native字幕{index}のas_transcribed表示がsourceと一致しません。")
        if status == "display_normalized" and not is_display_normalization(source_text, display_text):
            raise ValueError(f"native字幕{index}のdisplay_normalized表示がsourceから導出できません。")
        native_captions.append(
            {
                "id": f"cap_{index:06d}",
                "start_word_id": start_id,
                "end_word_id": end_id,
                "start": words[start_id]["timeline_start"],
                "end": words[end_id]["timeline_end"],
                "source_text": source_text,
                "display_text": display_text,
                "text_status": status,
                "timing_mode": "word_bound",
                "word_timing_overrides": [],
                "template_id": str(caption.get("template_id") or ""),
            }
        )
        expected_word_id = end_id + 1
    if expected_word_id != len(words):
        raise ValueError("native字幕で末尾のwordが未割当です。")

    project = {
        "schema_version": V2_SCHEMA_VERSION,
        "revision": 0,
        "run_dir": str(native_project.get("run_dir", "")),
        "source_path": str(native_project.get("source_path", "")),
        "edl_path": str(native_project.get("edl_path", "")),
        "output": deepcopy(native_project.get("output", {})),
        "caption_settings": deepcopy(native_project.get("caption_settings", {})),
        "default_template_id": str(native_project.get("default_template_id", "standard")),
        "captions": native_captions,
    }
    issues = validate_timeline_project(project, words)
    if issues:
        raise ValueError("native v2生成結果が不正です: " + "; ".join(issues))
    return project


def validate_timeline_project(project: dict, words: list[dict]) -> list[str]:
    issues = validate_transcript_words(words)
    if project.get("schema_version") != V2_SCHEMA_VERSION:
        issues.append("schema_versionがv2ではありません")
    if not isinstance(project.get("revision"), int) or project["revision"] < 0:
        issues.append("revisionが0以上の整数ではありません")
    captions = project.get("captions")
    if not isinstance(captions, list) or not captions:
        return issues + ["字幕がありません"]

    expected_word_id = 0
    seen_ids = set()
    effective_starts = [float(word["timeline_start"]) for word in words]
    effective_ends = [float(word["timeline_end"]) for word in words]
    for index, caption in enumerate(captions, 1):
        caption_id = caption.get("id")
        if not isinstance(caption_id, str) or not caption_id or caption_id in seen_ids:
            issues.append(f"字幕{index}のidが一意ではありません")
        seen_ids.add(caption_id)
        try:
            start_id = int(caption["start_word_id"])
            end_id = int(caption["end_word_id"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"字幕{index}のword範囲が不正です")
            continue
        if start_id > end_id or start_id < 0 or end_id >= len(words):
            issues.append(f"字幕{index}のword範囲が不正です")
            continue
        if start_id < expected_word_id:
            issues.append(f"字幕{index}でwordが重複しています")
        elif start_id > expected_word_id:
            issues.append(f"字幕{index}の前にword欠落があります")
        expected_word_id = end_id + 1
        expected_source = "".join(_word_text(word) for word in words[start_id : end_id + 1])
        if caption.get("source_text") != expected_source:
            issues.append(f"字幕{index}のsource_textがword正本と一致しません")
        if not str(caption.get("display_text", "")).strip():
            issues.append(f"字幕{index}のdisplay_textが空です")
        text_status = caption.get("text_status")
        if text_status is not None:
            if text_status not in TEXT_STATUSES:
                issues.append(f"字幕{index}のtext_statusが不正です")
            elif text_status == "as_transcribed" and caption.get("display_text") != expected_source:
                issues.append(f"字幕{index}のas_transcribed表示がsourceと一致しません")
            elif text_status == "display_normalized" and not is_display_normalization(expected_source, caption.get("display_text")):
                issues.append(f"字幕{index}のdisplay_normalized表示がsourceから導出できません")
        try:
            overrides = list(_iter_timing_overrides(caption))
        except (KeyError, TypeError, ValueError):
            overrides = []
            issues.append(f"caption {index} has invalid word_timing_overrides")
        seen_override_ids = set()
        for override_word_id, override_start, override_end in overrides:
            if override_word_id in seen_override_ids:
                issues.append(f"caption {index} has duplicate timing override for word {override_word_id}")
            seen_override_ids.add(override_word_id)
            if override_word_id < start_id or override_word_id > end_id:
                issues.append(f"caption {index} timing override is outside its word range")
            if not all(math.isfinite(value) for value in (override_start, override_end)) or override_end < override_start:
                issues.append(f"caption {index} has invalid timing override bounds")
            elif 0 <= override_word_id < len(words):
                effective_starts[override_word_id] = override_start
                effective_ends[override_word_id] = override_end
        try:
            position_offset = float(caption.get("position_offset", 0.0) or 0.0)
        except (TypeError, ValueError):
            position_offset = 0.0
            issues.append(f"字幕{index}のposition_offsetが不正です")
        if not math.isfinite(position_offset):
            issues.append(f"字幕{index}のposition_offsetが有限値ではありません")
        if "deleted" in caption and not isinstance(caption.get("deleted"), bool):
            issues.append(f"字幕{index}のdeletedが真偽値ではありません")
        expected_timing_mode = (
            "position_override" if position_offset else
            "word_override" if overrides else
            "word_bound"
        )
        if caption.get("timing_mode") != expected_timing_mode:
            issues.append(f"字幕{index}のtiming_modeが未対応です")
        expected_start = effective_starts[start_id] + position_offset
        expected_end = effective_ends[end_id] + position_offset
        if not math.isclose(float(caption.get("start", -1)), expected_start, abs_tol=1e-6) or not math.isclose(float(caption.get("end", -1)), expected_end, abs_tol=1e-6):
            issues.append(f"字幕{index}の時刻がword正本から導出されていません")
    previous_end = -1.0
    for word_id, (start, end) in enumerate(zip(effective_starts, effective_ends)):
        if start < previous_end:
            issues.append(f"word {word_id} timing overlaps the preceding word")
        previous_end = max(previous_end, end)
    if expected_word_id != len(words):
        issues.append("末尾に未割当wordがあります")
    return issues


def migrate_v1_project_file(v1_path: str | os.PathLike, words: list[dict]) -> Path:
    """v1を残したまま backup と独立した ``caption_project.v2.json`` を作る。"""
    source = Path(v1_path)
    with source.open("r", encoding="utf-8") as file:
        v1_project = json.load(file)
    v2_project = migrate_v1_project(v1_project, words)
    backup_path = source.with_name(f"{source.stem}.v1.backup{source.suffix}")
    v2_path = source.with_name(f"{source.stem}.v2{source.suffix}")
    if not backup_path.exists():
        _atomic_json_write(backup_path, v1_project)
    _atomic_json_write(v2_path, v2_project)
    return v2_path


def load_timeline_project(path: str | os.PathLike) -> dict:
    with Path(path).open("r", encoding="utf-8") as file:
        project = json.load(file)
    if project.get("schema_version") != V2_SCHEMA_VERSION:
        raise ValueError("未対応のTimeline字幕プロジェクト形式です。")
    return project
