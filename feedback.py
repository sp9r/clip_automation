import datetime as dt
import json
import math
import os
import uuid
from pathlib import Path


SCHEMA_VERSION = 1
VALID_VERDICTS = {"good", "bad", "corrected"}
DEFAULT_FEEDBACK_PATH = Path(__file__).resolve().parent / "data" / "feedback_log.json"


def _empty_database() -> dict:
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def load_feedback(path: str | os.PathLike | None = None) -> dict:
    feedback_path = Path(path) if path else DEFAULT_FEEDBACK_PATH
    if not feedback_path.exists() or feedback_path.stat().st_size == 0:
        return _empty_database()

    with feedback_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    # Phase 1で配列形式を使っていた場合も読み込めるようにする。
    if isinstance(data, list):
        data = {"schema_version": SCHEMA_VERSION, "entries": data}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("feedback_log.json の形式が不正です。")
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def _validated_interval(start, end) -> dict:
    try:
        start_value = float(start)
        end_value = float(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("開始秒と終了秒は数値で指定してください。") from exc

    if not math.isfinite(start_value) or not math.isfinite(end_value):
        raise ValueError("開始秒と終了秒は有限の数値で指定してください。")
    if start_value < 0 or end_value <= start_value:
        raise ValueError("0秒以上かつ、終了秒が開始秒より後になるよう指定してください。")
    if end_value - start_value > 60:
        raise ValueError("修正後のハイライトは60秒以内にしてください。")
    return {"start": round(start_value, 3), "end": round(end_value, 3)}


def record_feedback(
    *,
    run_dir: str,
    original_highlight: dict,
    verdict: str,
    corrected_start=None,
    corrected_end=None,
    comment: str = "",
    path: str | os.PathLike | None = None,
) -> dict:
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"未対応の評価です: {verdict}")

    correction = None
    if verdict == "corrected":
        correction = _validated_interval(corrected_start, corrected_end)

    entry = {
        "id": uuid.uuid4().hex,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "verdict": verdict,
        "original_highlight": original_highlight,
        "corrected_highlight": correction,
        "comment": str(comment or "").strip(),
    }

    feedback_path = Path(path) if path else DEFAULT_FEEDBACK_PATH
    database = load_feedback(feedback_path)
    database["entries"].append(entry)
    feedback_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = feedback_path.with_suffix(feedback_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(database, file, ensure_ascii=False, indent=2)
        file.write("\n")

    try:
        os.replace(temp_path, feedback_path)
    except PermissionError:
        if feedback_path.exists():
            try:
                os.remove(feedback_path)
            except OSError:
                pass
        import shutil
        shutil.move(str(temp_path), str(feedback_path))
    return entry




def build_feedback_prompt(
    path: str | os.PathLike | None = None,
    *,
    limit: int = 20,
) -> str:
    database = load_feedback(path)
    entries = database["entries"][-limit:] if limit > 0 else []
    if not entries:
        return "過去の編集フィードバックはまだありません。"

    labels = {"good": "採用", "bad": "不採用", "corrected": "区間修正"}
    lines = ["過去の編集フィードバック（新しい順）:"]
    for entry in reversed(entries):
        original = entry.get("original_highlight") or {}
        interval = f"{original.get('start', '?')}〜{original.get('end', '?')}秒"
        parts = [f"- {labels.get(entry.get('verdict'), entry.get('verdict'))}: 元候補 {interval}"]
        correction = entry.get("corrected_highlight")
        if correction:
            parts.append(f"修正後 {correction['start']}〜{correction['end']}秒")
        if entry.get("comment"):
            parts.append(f"メモ: {entry['comment']}")
        lines.append(" / ".join(parts))
    return "\n".join(lines)
