import json
import os
import uuid
from copy import deepcopy
from pathlib import Path

from caption_settings import DEFAULT_MAX_LINES, validate_max_chars, validate_max_lines
from ui_helpers import parse_font_weight


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_USER_TEMPLATE_PATH = PROJECT_ROOT / "data" / "user_caption_templates.json"
PROJECT_SCHEMA_VERSION = 1

BUILTIN_TEMPLATES = {
    "standard": {
        "id": "standard",
        "name": "標準（下中央）",
        "font_name": "Meiryo",
        "font_size": 54,
        "primary_color": "#FFFFFF",
        "outline_color": "#000000",
        "bold": True,
        "italic": False,
        "outline": 3.0,
        "shadow": 1.0,
        "shadow_color": "#000000",
        "background_color": "#000000",
        "background_opacity": 50,
        "border_style": 1,
        "underline": False,
        "strikeout": False,
        "scale_x": 100.0,
        "scale_y": 100.0,
        "letter_spacing": 0.0,
        "angle": 0.0,
        "alignment": 2,
        "margin_v": 80,
        "position_x": None,
        "position_y": None,
    },
    "emphasis": {
        "id": "emphasis",
        "name": "強調（黄・太字）",
        "font_name": "Meiryo",
        "font_size": 64,
        "primary_color": "#FFF200",
        "outline_color": "#C00000",
        "bold": True,
        "italic": False,
        "outline": 4.0,
        "shadow": 2.0,
        "shadow_color": "#7A0000",
        "background_color": "#4A0000",
        "background_opacity": 35,
        "border_style": 1,
        "underline": False,
        "strikeout": False,
        "scale_x": 100.0,
        "scale_y": 100.0,
        "letter_spacing": 0.0,
        "angle": 0.0,
        "alignment": 2,
        "margin_v": 90,
        "position_x": None,
        "position_y": None,
    },
    "top": {
        "id": "top",
        "name": "上中央",
        "font_name": "Meiryo",
        "font_size": 50,
        "primary_color": "#FFFFFF",
        "outline_color": "#000000",
        "bold": True,
        "italic": False,
        "outline": 3.0,
        "shadow": 1.0,
        "shadow_color": "#000000",
        "background_color": "#000000",
        "background_opacity": 50,
        "border_style": 1,
        "underline": False,
        "strikeout": False,
        "scale_x": 100.0,
        "scale_y": 100.0,
        "letter_spacing": 0.0,
        "angle": 0.0,
        "alignment": 8,
        "margin_v": 70,
        "position_x": None,
        "position_y": None,
    },
}


def _atomic_json_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temp_path, path)


def _validate_color(value: str) -> str:
    value = str(value or "").upper()
    if len(value) != 7 or not value.startswith("#"):
        raise ValueError("色は #RRGGBB 形式で指定してください。")
    int(value[1:], 16)
    return value


def validate_template(template: dict) -> dict:
    normalized = {
        "id": str(template.get("id") or f"user_{uuid.uuid4().hex[:8]}"),
        "name": str(template.get("name") or "名称未設定").strip(),
        "font_name": str(template.get("font_name") or "Meiryo").strip(),
        "font_size": int(template.get("font_size", 54)),
        "primary_color": _validate_color(template.get("primary_color", "#FFFFFF")),
        "outline_color": _validate_color(template.get("outline_color", "#000000")),
        "bold": bool(template.get("bold", True)),
        "italic": bool(template.get("italic", False)),
        "outline": float(template.get("outline", 3.0)),
        "shadow": float(template.get("shadow", 1.0)),
        "shadow_color": _validate_color(template.get("shadow_color", "#000000")),
        "background_color": _validate_color(template.get("background_color", "#000000")),
        "background_opacity": int(template.get("background_opacity", 50)),
        "border_style": int(template.get("border_style", 1)),
        "underline": bool(template.get("underline", False)),
        "strikeout": bool(template.get("strikeout", False)),
        "scale_x": float(template.get("scale_x", 100.0)),
        "scale_y": float(template.get("scale_y", 100.0)),
        "letter_spacing": float(template.get("letter_spacing", 0.0)),
        "angle": float(template.get("angle", 0.0)),
        "alignment": int(template.get("alignment", 2)),
        "margin_v": int(template.get("margin_v", 80)),
        "position_x": template.get("position_x"),
        "position_y": template.get("position_y"),
    }
    if not normalized["name"]:
        raise ValueError("テンプレート名を入力してください。")
    if not 8 <= normalized["font_size"] <= 300:
        raise ValueError("文字サイズは8〜300で指定してください。")
    if normalized["alignment"] not in range(1, 10):
        raise ValueError("配置はASSの1〜9で指定してください。")
    if normalized["margin_v"] < 0:
        raise ValueError("上下余白は0以上で指定してください。")
    if not 0 <= normalized["background_opacity"] <= 100:
        raise ValueError("背景不透明度は0〜100で指定してください。")
    if normalized["border_style"] not in {1, 3}:
        raise ValueError("背景スタイルは1（通常）または3（ボックス）で指定してください。")
    if not 50 <= normalized["scale_x"] <= 200 or not 50 <= normalized["scale_y"] <= 200:
        raise ValueError("文字の横幅・縦幅は50〜200で指定してください。")
    if not -20 <= normalized["letter_spacing"] <= 50:
        raise ValueError("文字間隔は-20〜50で指定してください。")
    if not -180 <= normalized["angle"] <= 180:
        raise ValueError("回転角度は-180〜180で指定してください。")
    for key in ("position_x", "position_y"):
        if normalized[key] in ("", None):
            normalized[key] = None
        else:
            normalized[key] = int(normalized[key])
    return normalized


def _load_user_template_data(path: str | os.PathLike | None = None) -> dict:
    template_path = Path(path) if path else DEFAULT_USER_TEMPLATE_PATH
    if not template_path.exists() or template_path.stat().st_size == 0:
        return {"schema_version": 1, "templates": []}
    with template_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("templates"), list):
        raise ValueError("ユーザーテンプレートJSONの形式が不正です。")
    return data


def load_templates(path: str | os.PathLike | None = None) -> dict[str, dict]:
    templates = deepcopy(BUILTIN_TEMPLATES)
    for template in _load_user_template_data(path)["templates"]:
        validated = validate_template(template)
        templates[validated["id"]] = validated
    return templates


def save_user_template(
    template: dict,
    path: str | os.PathLike | None = None,
) -> dict:
    template_path = Path(path) if path else DEFAULT_USER_TEMPLATE_PATH
    data = _load_user_template_data(template_path)
    existing = None
    requested_id = template.get("id")
    for index, item in enumerate(data["templates"]):
        if requested_id and item.get("id") == requested_id:
            existing = index
            break
        if item.get("name") == template.get("name"):
            existing = index
            template["id"] = item.get("id")
            break
    validated = validate_template(template)
    if validated["id"] in BUILTIN_TEMPLATES:
        validated["id"] = f"user_{uuid.uuid4().hex[:8]}"
    if existing is None:
        data["templates"].append(validated)
    else:
        data["templates"][existing] = validated
    _atomic_json_write(template_path, data)
    return validated


def delete_user_template(
    template_id: str,
    path: str | os.PathLike | None = None,
) -> bool:
    if template_id in BUILTIN_TEMPLATES:
        raise ValueError("組み込みテンプレートは削除できません。")
    template_path = Path(path) if path else DEFAULT_USER_TEMPLATE_PATH
    data = _load_user_template_data(template_path)
    remaining = [item for item in data["templates"] if item.get("id") != template_id]
    if len(remaining) == len(data["templates"]):
        return False
    data["templates"] = remaining
    _atomic_json_write(template_path, data)
    return True


def create_caption_project(
    *,
    captions: list[dict],
    source_path: str,
    edl_path: str,
    run_dir: str,
    default_template_id: str = "standard",
    width: int = 1920,
    height: int = 1080,
    fps: int = 60,
    max_chars: int = 24,
    max_lines: int = DEFAULT_MAX_LINES,
) -> dict:
    max_chars = validate_max_chars(max_chars)
    max_lines = validate_max_lines(max_lines)
    project = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "source_path": str(source_path),
        "edl_path": str(edl_path),
        "output": {"width": int(width), "height": int(height), "fps": int(fps)},
        "caption_settings": {
            "max_chars": max_chars,
            "max_lines": max_lines,
            "max_lines_application": "renderer_owned",
        },
        "default_template_id": default_template_id,
        "captions": [],
    }
    for index, caption in enumerate(captions, 1):
        if caption.get("deleted"):
            continue
        project["captions"].append(
            {
                "id": index,
                "start": round(float(caption["start"]), 3),
                "end": round(float(caption["end"]), 3),
                "text": str(caption["text"]),
                "template_id": caption.get("template_id") or "",
                **{
                    key: caption[key]
                    for key in ("start_word_id", "end_word_id", "source_text", "text_status")
                    if key in caption
                },
            }
        )
    return project


def save_caption_project(project: dict, path: str | os.PathLike):
    project_path = Path(path)
    _atomic_json_write(project_path, project)
    return str(project_path)


def load_caption_project(path: str | os.PathLike) -> dict:
    with Path(path).open("r", encoding="utf-8") as file:
        project = json.load(file)
    if project.get("schema_version") != PROJECT_SCHEMA_VERSION:
        raise ValueError("未対応の字幕プロジェクト形式です。")
    return project


def project_to_rows(project: dict) -> list[list]:
    return [
        [
            caption["id"],
            caption["start"],
            caption["end"],
            caption["text"],
            caption.get("template_id", ""),
        ]
        for caption in project["captions"]
    ]


def update_project_from_rows(
    project: dict,
    rows,
    *,
    default_template_id: str,
    width: int,
    height: int,
    fps: int,
    max_chars: int,
    max_lines: int = DEFAULT_MAX_LINES,
) -> dict:
    templates = load_templates()
    if default_template_id not in templates:
        raise ValueError("デフォルトテンプレートが見つかりません。")
    width, height, fps = int(width), int(height), int(fps)
    max_chars = validate_max_chars(max_chars)
    max_lines = validate_max_lines(max_lines)
    if width < 320 or height < 240:
        raise ValueError("解像度は320×240以上にしてください。")
    if not 1 <= fps <= 240:
        raise ValueError("FPSは1〜240で指定してください。")

    updated_captions = []
    for row_index, row in enumerate(rows or [], 1):
        if len(row) < 5:
            raise ValueError(f"字幕表の{row_index}行目に必要な列がありません。")
        caption_id, start, end, text, template_id = row[:5]
        start, end = float(start), float(end)
        if start < 0 or end <= start:
            raise ValueError(f"字幕{caption_id}の開始・終了時刻が不正です。")
        text = str(text or "").strip()
        if not text:
            raise ValueError(f"字幕{caption_id}の本文が空です。")
        template_id = str(template_id or "").strip()
        if template_id and template_id not in templates:
            raise ValueError(f"字幕{caption_id}のテンプレートが見つかりません: {template_id}")
        updated_captions.append(
            {
                "id": int(caption_id),
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "template_id": template_id,
            }
        )
    if not updated_captions:
        raise ValueError("字幕が1件もありません。")

    project["captions"] = updated_captions
    project["default_template_id"] = default_template_id
    project["output"] = {"width": width, "height": height, "fps": fps}
    project["caption_settings"] = {
        "max_chars": max_chars,
        "max_lines": max_lines,
        "max_lines_application": "renderer_owned",
    }
    return project


def _ass_color(hex_color: str, alpha: str = "00") -> str:
    color = _validate_color(hex_color)[1:]
    red, green, blue = color[0:2], color[2:4], color[4:6]
    return f"&H{alpha}{blue}{green}{red}"


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds_int, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_int:02d}.{centiseconds:02d}"


def _ass_text(text: str) -> str:
    return (
        str(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def write_ass_subtitles(
    project: dict,
    output_ass: str | os.PathLike,
    *,
    template_path: str | os.PathLike | None = None,
) -> str:
    templates = load_templates(template_path)
    default_id = project.get("default_template_id", "standard")
    if default_id not in templates:
        raise ValueError(f"デフォルトテンプレートが見つかりません: {default_id}")

    width = int(project["output"]["width"])
    height = int(project["output"]["height"])
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",

        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]

    for template_id, raw_template in templates.items():
        template = validate_template(raw_template)
        base_family, parsed_weight = parse_font_weight(template["font_name"], template["bold"])
        template["_parsed_weight"] = parsed_weight
        try:
            from font_manager import ensure_static_font_registered
            font_family = ensure_static_font_registered(base_family, parsed_weight)
        except Exception:
            font_family = base_family
        lines.append(
            "Style: "
            + ",".join(
                [
                    template_id,
                    font_family,
                    str(template["font_size"]),

                    _ass_color(template["primary_color"]),
                    _ass_color(template["primary_color"]),
                    _ass_color(template["outline_color"]),
                    _ass_color(template["background_color"], f"{round(255 - template['background_opacity'] * 2.55):02X}"),
                    "-1" if template["bold"] else "0",
                    "-1" if template["italic"] else "0",
                    "-1" if template["underline"] else "0",
                    "-1" if template["strikeout"] else "0",
                    str(template["scale_x"]),
                    str(template["scale_y"]),
                    str(template["letter_spacing"]),
                    str(template["angle"]),
                    str(template["border_style"]),
                    str(template["outline"]),
                    str(template["shadow"]),
                    str(template["alignment"]),
                    "40",
                    "40",
                    str(template["margin_v"]),
                    "1",
                ]
            )
        )

    lines.extend(
        [
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    for caption in project["captions"]:
        template_id = caption.get("template_id") or default_id
        if template_id not in templates:
            template_id = default_id
        template = templates[template_id]
        override = ""
        # Per-template shadow color is supported by ASS override tags.
        if template.get("shadow_color"):
            override = f"{{\\4c{_ass_color(template['shadow_color'])}}}"
        if template.get("position_x") is not None and template.get("position_y") is not None:
            override = f"{{\\pos({template['position_x']},{template['position_y']})}}"
        weight_tag = f"{{\\b{template.get('_parsed_weight', 700 if template.get('bold') else 400)}}}"
        lines.append(
            f"Dialogue: 0,{_ass_time(caption['start'])},{_ass_time(caption['end'])},"
            f"{template_id},,0,0,0,,{weight_tag}{override}{_ass_text(caption['text'])}"
        )

    output_path = Path(output_ass)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return str(output_path)


def realign_caption_project_texts(project: dict, new_texts: list[str], raw_segments: list[dict]) -> dict:
    """ユーザーが自由編集・区切り指定したテキストに対し、元音声の時間軸を照合してタイムスタンプを全自動再同期する。"""
    words = []
    for segment in raw_segments:
        segment_words = segment.get("words")
        if segment_words:
            words.extend(segment_words)
        else:
            words.append({
                "word": segment.get("text", ""),
                "start": segment["start"],
                "end": segment["end"]
            })

    timed_characters = []
    for word in words:
        w_text = word["word"]
        duration = word["end"] - word["start"]
        char_count = max(1, len(w_text))
        for index, character in enumerate(w_text):
            timed_characters.append({
                "character": character,
                "start": word["start"] + duration * index / char_count,
                "end": word["start"] + duration * (index + 1) / char_count
            })

    if not timed_characters:
        return project

    total_time_end = timed_characters[-1]["end"]
    total_chars = len(timed_characters)

    cursor = 0
    updated_captions = []
    for text_line in new_texts:
        clean_text = text_line.strip()
        if not clean_text:
            continue
        text_len = len(clean_text)
        
        if cursor < total_chars:
            start_t = timed_characters[cursor]["start"]
            end_idx = min(total_chars - 1, cursor + text_len - 1)
            end_t = timed_characters[end_idx]["end"]
            cursor += text_len
        else:
            start_t = total_time_end
            end_t = total_time_end

        updated_captions.append({
            "id": len(updated_captions) + 1,
            "start": round(start_t, 3),
            "end": round(end_t, 3),
            "text": clean_text,
            "template_id": ""
        })

    project["captions"] = updated_captions
    return project
