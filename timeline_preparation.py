"""Timeline Editor P0 の新規生成と既存run非破壊準備。"""

import hashlib
import json
import os
from pathlib import Path

from caption_settings import DEFAULT_MAX_LINES, validate_max_chars, validate_max_lines

from timeline_project import (
    _atomic_json_write,
    _normalize_text,
    _word_text,
    build_native_v2_project,
    build_transcript_words,
    migrate_v1_project,
    validate_timeline_project,
)


REPORT_SCHEMA_VERSION = 1


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mismatch(v1_project: dict, words: list[dict]) -> dict | None:
    cursor = 0
    for index, caption in enumerate(v1_project.get("captions") or []):
        target = _normalize_text(caption.get("text", ""))
        assembled = ""
        start = cursor
        while cursor < len(words) and len(assembled) < len(target):
            assembled += _normalize_text(_word_text(words[cursor]))
            cursor += 1
        if not target or assembled != target:
            return {
                "caption_index": index + 1,
                "caption_text": str(caption.get("text", "")),
                "word_text": "".join(_word_text(word) for word in words[start:cursor]),
                "reason": "empty_caption" if not target else "text_mismatch",
            }
    if cursor != len(words):
        return {
            "caption_index": len(v1_project.get("captions") or []) + 1,
            "caption_text": "",
            "word_text": "".join(_word_text(word) for word in words[cursor : cursor + 20]),
            "reason": "unassigned_words",
        }
    return None


class TimelinePreparationService:
    """入力を変更せず、成功時だけv2成果物を公開する。"""

    def __init__(self, run_dir: str | os.PathLike):
        self.run_dir = Path(run_dir).resolve()
        self.clean_path = self.run_dir / "clean_transcript.json"
        self.v1_path = self.run_dir / "caption_project.json"
        self.words_path = self.run_dir / "transcript_words.json"
        self.v2_path = self.run_dir / "caption_project.v2.json"
        self.report_path = self.run_dir / "migration_report.json"

    def prepare(self) -> dict:
        if not self.clean_path.is_file() or not self.v1_path.is_file():
            raise ValueError("clean_transcript.jsonとcaption_project.jsonが必要です。")
        hashes = {
            "clean_transcript.json": _sha256(self.clean_path),
            "caption_project.json": _sha256(self.v1_path),
        }
        segments = _load_json(self.clean_path)
        v1_project = _load_json(self.v1_path)
        words = build_transcript_words(segments)
        mismatch = _mismatch(v1_project, words)
        base_report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run_id": self.run_dir.name,
            "source_hashes": hashes,
            "word_count": len(words),
            "caption_count": len(v1_project.get("captions") or []),
        }
        if mismatch:
            report = {**base_report, "state": "needs_review", "first_mismatch": mismatch}
            _atomic_json_write(self.report_path, report)
            return report
        try:
            project = migrate_v1_project(v1_project, words)
        except ValueError as error:
            report = {
                **base_report,
                "state": "invalid",
                "first_mismatch": {"reason": "validation_failed", "message": str(error)},
            }
            _atomic_json_write(self.report_path, report)
            return report
        issues = validate_timeline_project(project, words)
        if issues:
            report = {
                **base_report,
                "state": "invalid",
                "first_mismatch": {"reason": "validation_failed", "issues": issues},
            }
            _atomic_json_write(self.report_path, report)
            return report

        # reportを最後に公開する。途中中断時はready判定にならない。
        _atomic_json_write(self.words_path, words)
        _atomic_json_write(self.v2_path, project)
        report = {**base_report, "state": "ready", "first_mismatch": None}
        _atomic_json_write(self.report_path, report)
        return report


def create_native_timeline_project(
    run_dir: str | os.PathLike,
    *,
    captions: list[dict],
    source_path: str,
    edl_path: str,
    default_template_id: str,
    width: int,
    height: int,
    fps: int,
    max_chars: int,
    max_lines: int = DEFAULT_MAX_LINES,
    timeline_transcript_path: str | os.PathLike | None = None,
) -> dict:
    """新規runのcaption stageで、v1ファイルを経由せずv2を生成する。"""
    directory = Path(run_dir).resolve()
    clean_path = directory / "clean_transcript.json"
    word_source_path = Path(timeline_transcript_path).resolve() if timeline_transcript_path else clean_path
    if not word_source_path.is_file():
        raise ValueError(f"timeline用文字起こしが見つかりません: {word_source_path.name}")
    max_chars = validate_max_chars(max_chars)
    max_lines = validate_max_lines(max_lines)
    words = build_transcript_words(_load_json(word_source_path))
    # native production経路は、caption生成時に確定したword ownershipを使う。
    in_memory_contract = {
        "schema_version": 1,
        "run_dir": str(directory),
        "source_path": str(source_path),
        "edl_path": str(edl_path),
        "output": {"width": int(width), "height": int(height), "fps": int(fps)},
        "caption_settings": {
            "max_chars": max_chars,
            "max_lines": max_lines,
            "max_lines_application": "renderer_owned",
        },
        "default_template_id": default_template_id,
        "captions": captions,
    }
    # 旧単体呼び出し（ownershipがないfixture等）だけは、legacy v1 migration
    # を互換経路として残す。fresh production outputはこの分岐に入らず、
    # 明示ownershipからbuild_native_v2_projectへ進む。
    if any("start_word_id" not in caption for caption in captions):
        project = migrate_v1_project(in_memory_contract, words)
    else:
        project = build_native_v2_project(in_memory_contract, words)
    issues = validate_timeline_project(project, words)
    if issues:
        raise ValueError("Timeline v2のネイティブ生成に失敗しました: " + "; ".join(issues))
    _atomic_json_write(directory / "transcript_words.json", words)
    _atomic_json_write(directory / "caption_project.v2.json", project)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": directory.name,
        "state": "ready",
        "source_hashes": {word_source_path.name: _sha256(word_source_path)},
        "word_count": len(words),
        "caption_count": len(project["captions"]),
        "first_mismatch": None,
        "generation": "native",
    }
    _atomic_json_write(directory / "migration_report.json", report)
    return report
