import os
import sys
import hashlib
import json
import subprocess
from pathlib import Path

from ui_helpers import build_raw_caption_html

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_ROOT / "data" / "html_caption_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _get_chrome_path() -> str:
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not os.path.exists(chrome_path):
        chrome_path = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    if not os.path.exists(chrome_path):
        raise FileNotFoundError("Google Chrome (chrome.exe) が見つかりません。")
    return chrome_path

def render_caption_html_png(
    text: str,
    template: dict,
    output_width: int,
    output_height: int,
    output_png_path: Path
) -> Path:
    """単一の字幕テキストをHTML5 (Chromium Headless) でレンダリングし、透過PNGを生成する"""
    if output_png_path.exists() and output_png_path.stat().st_size > 0:
        return output_png_path

    html_body = build_raw_caption_html(
        sample_text=text,
        font_name=template.get("font_name", "Noto Sans JP ExtraBold"),
        font_size=template.get("font_size", 71),
        primary_color=template.get("primary_color", "#FFFFFF"),
        outline_color=template.get("outline_color", "#000000"),
        bold=template.get("bold", False),
        italic=template.get("italic", False),
        outline=template.get("outline", 15.0),
        shadow=template.get("shadow", 0.0),
        alignment=template.get("alignment", 2),
        margin_v=template.get("margin_v", 80),
        use_custom_position=template.get("position_x") is not None,
        position_x=template.get("position_x"),
        position_y=template.get("position_y"),
        output_width=output_width,
        output_height=output_height
    )


    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
html, body {{
    margin: 0;
    padding: 0;
    width: {output_width}px;
    height: {output_height}px;
    background: transparent !important;
    overflow: hidden;
}}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

    temp_html = output_png_path.with_suffix(".html")
    temp_html.write_text(full_html, encoding="utf-8")

    chrome_cmd = [
        _get_chrome_path(),
        "--headless=old",
        "--disable-gpu",
        "--default-background-color=00000000",
        "--hide-scrollbars",
        f"--window-size={output_width},{output_height}",
        f"--screenshot={output_png_path.resolve()}",
        f"file:///{temp_html.resolve()}"
    ]

    
    try:
        subprocess.run(chrome_cmd, check=True, capture_output=True)
    finally:
        if temp_html.exists():
            temp_html.unlink()

    return output_png_path


def _map_time_to_edl(t: float, edl: list[dict]) -> float:
    accum = 0.0
    for seg in edl:
        s_start = float(seg["start"])
        s_end = float(seg["end"])
        s_dur = s_end - s_start
        if t < s_start:
            return accum
        if s_start <= t <= s_end:
            return accum + (t - s_start)
        accum += s_dur
    return accum


def prepare_html_captions(project: dict, edl: list[dict] | None = None) -> list[dict]:
    """
    プロジェクト内の全字幕を透明PNGとしてレンダリングし、
    FFmpeg overlay フィルターで使用するパラメータリストを返す。
    """
    output_width = int(project["output"]["width"])
    output_height = int(project["output"]["height"])
    templates = {t["id"]: t for t in project.get("templates", [])}
    default_template_id = project.get("default_template_id", "standard")
    
    rendered_items = []
    
    for i, caption in enumerate(project.get("captions", [])):
        if caption.get("deleted"):
            continue
        text = caption["text"]
        start = float(caption["start"])
        end = float(caption["end"])


        t_id = caption.get("template_id", default_template_id)
        template = templates.get(t_id, templates.get(default_template_id, {}))
        
        # ハッシュ計算で無駄な描画を防止
        hash_src = f"{text}_{json.dumps(template, sort_keys=True)}_{output_width}_{output_height}"
        img_hash = hashlib.md5(hash_src.encode("utf-8")).hexdigest()
        png_path = CACHE_DIR / f"cap_{img_hash}.png"
        
        render_caption_html_png(text, template, output_width, output_height, png_path)
        
        rendered_items.append({
            "index": i,
            "start": start,
            "end": end,
            "png_path": str(png_path.resolve()).replace("\\", "/")
        })
        
    return rendered_items



def build_ffmpeg_html_overlay_filter(rendered_items: list[dict]) -> tuple[list[str], str]:
    """
    FFmpeg用の入力引数リストと overlay filter_complex 文字列を生成する。
    """
    if not rendered_items:
        return [], ""
        
    input_args = []
    filter_chains = []
    
    last_v = "[0:v]"
    for idx, item in enumerate(rendered_items):
        input_args.extend(["-i", item["png_path"]])
        in_img_idx = idx + 1
        out_v = f"[v_cap_{idx}]" if idx < len(rendered_items) - 1 else "[v_out]"
        start = item["start"]
        end = item["end"]
        
        filter_chains.append(
            f"{last_v}[{in_img_idx}:v]overlay=0:0:eof_action=repeat:enable='between(t,{start:.3f},{end:.3f})'{out_v}"
        )
        last_v = out_v


        
    filter_complex_str = ";".join(filter_chains)
    return input_args, filter_complex_str
