import json
import os
import uuid
from copy import deepcopy
from pathlib import Path

from caption_settings import validate_max_chars, validate_max_lines


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS_TEMPLATE_PATH = PROJECT_ROOT / "data" / "user_project_settings.json"

BUILTIN_PROJECT_SETTINGS = {
    "fhd_landscape": {
        "id": "fhd_landscape",
        "name": "【標準】横画面 FHD (1920x1080)",
        "output_width": 1920,
        "output_height": 1080,
        "output_fps": 60,
        "max_chars": 32,
        "max_lines": 2,
        "segmentation_mode": "llm",
        "caption_model": "qwen2.5:14b",
        "default_template_id": "standard",
        "video_encoder": "auto",
    },
    "fhd_portrait": {
        "id": "fhd_portrait",
        "name": "【Shorts用】縦画面 (1080x1920)",
        "output_width": 1080,
        "output_height": 1920,
        "output_fps": 60,
        "max_chars": 20,
        "max_lines": 2,
        "segmentation_mode": "llm",
        "caption_model": "qwen2.5:14b",
        "default_template_id": "standard",
        "video_encoder": "auto",
    },
    "hd_landscape": {
        "id": "hd_landscape",
        "name": "横画面 HD (1280x720) 高速",
        "output_width": 1280,
        "output_height": 720,
        "output_fps": 30,
        "max_chars": 32,
        "max_lines": 2,
        "segmentation_mode": "heuristic",
        "caption_model": "qwen2.5:14b",
        "default_template_id": "standard",
        "video_encoder": "auto",
    },
}


def _atomic_json_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def validate_project_settings(settings: dict) -> dict:
    normalized = {
        "id": str(settings.get("id") or f"preset_{uuid.uuid4().hex[:8]}"),
        "name": str(settings.get("name") or "名称未設定").strip(),
        "output_width": int(settings.get("output_width", 1920)),
        "output_height": int(settings.get("output_height", 1080)),
        "output_fps": int(settings.get("output_fps", 60)),
        "max_chars": validate_max_chars(settings.get("max_chars")),
        "max_lines": validate_max_lines(settings.get("max_lines")),
        "segmentation_mode": str(settings.get("segmentation_mode", "llm")),
        "caption_model": str(settings.get("caption_model", "qwen2.5:14b")),
        "default_template_id": str(settings.get("default_template_id", "standard")),
        "video_encoder": str(settings.get("video_encoder", "auto")),
    }
    if not normalized["name"]:
        raise ValueError("テンプレート名を入力してください。")
    if normalized["output_width"] < 320 or normalized["output_height"] < 240:
        raise ValueError("解像度は320×240以上にしてください。")
    if not 1 <= normalized["output_fps"] <= 240:
        raise ValueError("FPSは1〜240で指定してください。")
    if normalized["segmentation_mode"] not in {"llm", "heuristic"}:
        raise ValueError("字幕区切り方式が不正です。")
    return normalized


def _load_user_settings_data(path: str | os.PathLike | None = None) -> dict:
    settings_path = Path(path) if path else DEFAULT_SETTINGS_TEMPLATE_PATH
    if not settings_path.exists() or settings_path.stat().st_size == 0:
        return {"schema_version": 1, "presets": []}
    with settings_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
        raise ValueError("プロジェクト設定JSONの形式が不正です。")
    return data


def load_project_settings_presets(path: str | os.PathLike | None = None) -> dict[str, dict]:
    presets = deepcopy(BUILTIN_PROJECT_SETTINGS)
    for preset in _load_user_settings_data(path)["presets"]:
        validated = validate_project_settings(preset)
        presets[validated["id"]] = validated
    return presets


def save_project_settings_preset(
    settings: dict,
    path: str | os.PathLike | None = None,
) -> dict:
    settings_path = Path(path) if path else DEFAULT_SETTINGS_TEMPLATE_PATH
    data = _load_user_settings_data(settings_path)
    existing = None
    requested_id = settings.get("id")
    for index, item in enumerate(data["presets"]):
        if requested_id and item.get("id") == requested_id:
            existing = index
            break
        if item.get("name") == settings.get("name"):
            existing = index
            settings["id"] = item.get("id")
            break
    validated = validate_project_settings(settings)
    if validated["id"] in BUILTIN_PROJECT_SETTINGS:
        validated["id"] = f"preset_{uuid.uuid4().hex[:8]}"
    if existing is None:
        data["presets"].append(validated)
    else:
        data["presets"][existing] = validated
    _atomic_json_write(settings_path, data)
    return validated


def delete_project_settings_preset(
    preset_id: str,
    path: str | os.PathLike | None = None,
) -> bool:
    if preset_id in BUILTIN_PROJECT_SETTINGS:
        raise ValueError("組み込みテンプレートは削除できません。")
    settings_path = Path(path) if path else DEFAULT_SETTINGS_TEMPLATE_PATH
    data = _load_user_settings_data(settings_path)
    remaining = [item for item in data["presets"] if item.get("id") != preset_id]
    if len(remaining) == len(data["presets"]):
        return False
    data["presets"] = remaining
    _atomic_json_write(settings_path, data)
    return True
