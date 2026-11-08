import html
import os
import re
from functools import lru_cache


RESOLUTION_PRESETS = {
    "hd_landscape": (1280, 720, "720p 横（16:9）"),
    "fhd_landscape": (1920, 1080, "FHD 横（16:9）"),
    "uhd_landscape": (3840, 2160, "4K 横（16:9）"),
    "hd_portrait": (720, 1280, "720p 縦（9:16）"),
    "fhd_portrait": (1080, 1920, "FHD 縦（9:16）"),
    "uhd_portrait": (2160, 3840, "4K 縦（9:16）"),
    "square": (1080, 1080, "正方形（1:1）"),
}

RESOLUTION_CHOICES = [
    (label, preset_id)
    for preset_id, (_, _, label) in RESOLUTION_PRESETS.items()
] + [("カスタム（数値入力）", "custom")]

COMMON_FONT_NAMES = [
    "Meiryo",
    "Meiryo UI",
    "Yu Gothic",
    "Yu Gothic UI",
    "Yu Mincho",
    "MS Gothic",
    "MS PGothic",
    "MS Mincho",
    "Noto Sans JP",
    "Noto Serif JP",
    "BIZ UDPGothic",
    "BIZ UDPMincho",
    "Hiragino Sans",
    "Hiragino Kaku Gothic ProN",
    "LINE Seed JP",
    "Gen Jyuu Gothic",
    "M PLUS Rounded 1c",
    "Arial",
    "Arial Black",
    "Segoe UI",
    "Impact",
]


def apply_resolution_preset(preset_id: str, current_width, current_height):
    preset = RESOLUTION_PRESETS.get(str(preset_id))
    if preset is None:
        return int(current_width), int(current_height)
    return preset[0], preset[1]


def detect_resolution_preset(width, height) -> str:
    try:
        size = (int(width), int(height))
    except (TypeError, ValueError):
        return "custom"
    for preset_id, (preset_width, preset_height, _) in RESOLUTION_PRESETS.items():
        if size == (preset_width, preset_height):
            return preset_id
    return "custom"


@lru_cache(maxsize=1)
def installed_font_names() -> list[str]:
    names = set(COMMON_FONT_NAMES)
    if os.name == "nt":
        try:
            import winreg

            registry_locations = [
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
                ),
                (
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
                ),
            ]
            for hive, registry_path in registry_locations:
                try:
                    with winreg.OpenKey(hive, registry_path) as key:
                        index = 0
                        while True:
                            try:
                                value_name = winreg.EnumValue(key, index)[0]
                            except OSError:
                                break
                            index += 1
                            cleaned = re.sub(
                                r"\s*(?:\(TrueType(?:;)?\)|\(OpenType(?:;)?\)|\(All Res(?:;)?\)|&|;\s*).*$",
                                "",
                                value_name,
                                flags=re.IGNORECASE,
                            ).strip()
                            if not cleaned:
                                continue
                            names.add(cleaned)
                            for family_name in cleaned.split(" & "):
                                if family_name.strip():
                                    names.add(family_name.strip())
                except OSError:
                    continue
        except ImportError:
            pass

    priority = {name.casefold(): index for index, name in enumerate(COMMON_FONT_NAMES)}
    return sorted(
        names,
        key=lambda name: (
            priority.get(name.casefold(), len(priority)),
            name.casefold(),
        ),
    )


def _number(value, default, minimum=None, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if minimum is not None:
        result = max(float(minimum), result)
    if maximum is not None:
        result = min(float(maximum), result)
    return result


def _safe_color(value, fallback):
    value = str(value or "").upper()
    return value if re.fullmatch(r"#[0-9A-F]{6}", value) else fallback


def _safe_font(value):
    cleaned = re.sub(r"[;'\"\\<>]", "", str(value or "Meiryo")).strip()
    return cleaned or "Meiryo"


def parse_font_weight(font_name: str, bold_flag: bool = False) -> tuple[str, int]:
    weight_map = {
        "thin": 100, "hairline": 100,
        "extralight": 200, "ultralight": 200,
        "light": 300,
        "regular": 400, "normal": 400, "w3": 400,
        "medium": 500, "w4": 500,
        "semibold": 600, "demibold": 600, "w5": 600,
        "extrabold": 800, "ultrabold": 800, "w7": 800,
        "black": 900, "heavy": 900, "w8": 900,
        "extrablack": 900, "ultrablack": 900, "w9": 900,
        "bold": 700, "w6": 700,
    }
    
    cleaned_name = str(font_name or "").strip()
    lower_name = cleaned_name.casefold()
    
    matched_weight = None
    matched_keyword = None
    
    for keyword in sorted(weight_map.keys(), key=len, reverse=True):
        if keyword in lower_name:
            pattern = re.compile(r'\b' + re.escape(keyword) + r'\b', re.IGNORECASE)
            if pattern.search(lower_name) or lower_name.endswith(keyword):
                matched_weight = weight_map[keyword]
                matched_keyword = keyword
                break
                
    if matched_weight is not None:
        pattern = re.compile(r'\s*' + re.escape(matched_keyword) + r'\b', re.IGNORECASE)
        base_family = pattern.sub('', cleaned_name).strip()
        if base_family == cleaned_name:
             pattern2 = re.compile(re.escape(matched_keyword) + r'$', re.IGNORECASE)
             base_family = pattern2.sub('', cleaned_name).strip()
    else:
        base_family = cleaned_name
        matched_weight = 400

    if bold_flag and matched_weight < 700:
        matched_weight = 700
        
    return base_family or "Meiryo", matched_weight


def build_raw_caption_html(
    sample_text,
    font_name,
    font_size,
    primary_color,
    outline_color,
    bold,
    italic,
    outline,
    shadow,
    alignment,
    margin_v,
    use_custom_position,
    position_x,
    position_y,
    output_width,
    output_height,
) -> str:
    width = int(_number(output_width, 1920, 320, 7680))
    height = int(_number(output_height, 1080, 240, 7680))
    size = _number(font_size, 54, 8, 300)
    outline_size = _number(outline, 3, 0, 20)
    shadow_size = _number(shadow, 1, 0, 20)
    margin = _number(margin_v, 80, 0, height)
    try:
        alignment_id = int(alignment)
    except (TypeError, ValueError):
        alignment_id = 2
    if alignment_id not in range(1, 10):
        alignment_id = 2

    if use_custom_position and position_x is not None and position_y is not None:
        left_percent = _number(position_x, width / 2, 0, width) / width * 100
        top_percent = _number(position_y, height / 2, 0, height) / height * 100
        translate_x = -50
        translate_y = -50
    else:
        column = (alignment_id - 1) % 3
        row = (alignment_id - 1) // 3
        left_percent = (5, 50, 95)[column]
        translate_x = (0, -50, -100)[column]
        margin_percent = margin / height * 100
        if row == 0:
            top_percent = 100 - margin_percent
            translate_y = -100
        elif row == 1:
            top_percent = 50
            translate_y = -50
        else:
            top_percent = margin_percent
            translate_y = 0

    # ピクセルパーフェクト（指定解像度の高さを100%基準とした確実なpx/cqh計算）
    libass_scale = 1.0
    font_percent = (size / height * 100) * libass_scale
    outline_percent = (outline_size / height * 100) * libass_scale
    shadow_percent = (shadow_size / height * 100) * libass_scale


    primary = _safe_color(primary_color, "#FFFFFF")
    outline_color_value = _safe_color(outline_color, "#000000")
    font = _safe_font(font_name)
    base_family, parsed_weight = parse_font_weight(font, bool(bold))
    text = html.escape(str(sample_text or "字幕プレビューを入力してください。"))
    font_css = html.escape(base_family, quote=True)
    weight = parsed_weight
    style = "italic" if bool(italic) else "normal"

    return f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@100..900&display=swap');
    .template-preview-stage {{
        width: 100%;
        height: 100%;
        position: relative;
        container-type: size;
        background: transparent;
    }}
    </style>
    <div class="template-preview-stage">
      <div class="template-preview-text" style="
        position:absolute;
        left:{left_percent:.4f}%;top:{top_percent:.4f}%;
        transform:translate({translate_x}%,-0%) translateY({translate_y}%);
        font-family:'{font_css}','Noto Sans JP',sans-serif;
        font-size:{font_percent:.5f}cqh;
        color:{primary};font-weight:{weight};font-style:{style};
        -webkit-text-stroke:{outline_percent:.5f}cqh {outline_color_value};
        paint-order:stroke fill;
        text-shadow:{shadow_percent:.5f}cqh {shadow_percent:.5f}cqh {max(shadow_percent * 1.5, 0.01):.5f}cqh rgba(0,0,0,.9);
        white-space:nowrap;
      ">{text}</div>
    </div>
    """


def build_template_preview(
    sample_text,
    font_name,
    font_size,
    primary_color,
    outline_color,
    bold,
    italic,
    outline,
    shadow,
    alignment,
    margin_v,
    use_custom_position,
    position_x,
    position_y,
    output_width,
    output_height,
) -> str:
    width = int(_number(output_width, 1920, 320, 7680))
    height = int(_number(output_height, 1080, 240, 7680))
    is_portrait = height > width
    aspect_ratio = width / height
    orientation_label = "縦動画 (Shorts 9:16)" if is_portrait else ("正方形 (1:1)" if width == height else "横動画 (16:9)")

    raw_caption_html = build_raw_caption_html(
        sample_text, font_name, font_size, primary_color, outline_color,
        bold, italic, outline, shadow, alignment, margin_v,
        use_custom_position, position_x, position_y, width, height
    )

    shorts_safe_area = ""
    if is_portrait:
        shorts_safe_area = """
        <div style="position:absolute; inset:0; pointer-events:none; z-index:5;">
            <!-- 右側アイコンエリア -->
            <div style="position:absolute; right:0; bottom:15%; width:18%; height:45%; background:repeating-linear-gradient(45deg, rgba(255,50,50,0.15), rgba(255,50,50,0.15) 8px, transparent 8px, transparent 16px); border-left:1.5px dashed rgba(255,100,100,0.5); display:flex; align-items:center; justify-content:center; color:rgba(255,150,150,0.8); font-size:12px; font-weight:bold;">Shorts UI</div>
            <!-- 下部タイトル・音声エリア -->
            <div style="position:absolute; left:0; bottom:0; width:100%; height:16%; background:repeating-linear-gradient(45deg, rgba(255,50,50,0.15), rgba(255,50,50,0.15) 8px, transparent 8px, transparent 16px); border-top:1.5px dashed rgba(255,100,100,0.5); display:flex; align-items:center; padding-left:12px; color:rgba(255,150,150,0.8); font-size:12px; font-weight:bold;">タイトル・字幕UI被り注意</div>
            <!-- 上部ヘッダーエリア -->
            <div style="position:absolute; left:0; top:0; width:100%; height:10%; background:repeating-linear-gradient(45deg, rgba(255,50,50,0.15), rgba(255,50,50,0.15) 8px, transparent 8px, transparent 16px); border-bottom:1.5px dashed rgba(255,100,100,0.5);"></div>
        </div>
        """

    return f"""
    <style>
    .template-preview-shell {{
        width: 100%;
        min-height: 480px;
        max-height: 640px;
        height: 55vh;
        background: #0d1117;
        background-image: 
            radial-gradient(rgba(255,255,255,0.05) 1px, transparent 0);
        background-size: 20px 20px;
        position: relative;
        overflow: hidden;
        box-sizing: border-box;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.1);
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        padding: 16px;
    }}

    .template-preview-badge {{
        position: absolute;
        top: 12px;
        left: 14px;
        z-index: 10;
        background: rgba(22, 27, 34, 0.85);
        backdrop-filter: blur(8px);
        border: 1px solid rgba(255, 255, 255, 0.15);
        color: #e6edf3;
        font-family: system-ui, -apple-system, sans-serif;
        font-size: 13px;
        font-weight: 600;
        padding: 4px 12px;
        border-radius: 20px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        display: flex;
        align-items: center;
        gap: 6px;
    }}

    .template-preview-badge::before {{
        content: "";
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: #3fb950;
        box-shadow: 0 0 8px #3fb950;
    }}

    .video-monitor-wrapper {{
        position: relative;
        width: calc(min(100%, (50vh - 40px) * {aspect_ratio:.6f}));
        height: auto;
        aspect-ratio: {aspect_ratio:.6f};
        box-shadow: 0 16px 40px rgba(0, 0, 0, 0.8), 0 0 0 1.5px rgba(255, 255, 255, 0.2);
        border-radius: 8px;
        overflow: hidden;
        background: #000000;
        margin: auto;
    }}
    </style>
    <div class="template-preview-shell">
      <div class="template-preview-badge">
        画面解像度: {width} × {height} ({orientation_label})
      </div>
      <div class="video-monitor-wrapper">
        {shorts_safe_area}
        {raw_caption_html}
      </div>
    </div>
    """




