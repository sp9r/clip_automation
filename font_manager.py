import os
import sys
import ctypes
import tempfile
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

# GDI API
AddFontResourceW = ctypes.windll.gdi32.AddFontResourceW
RemoveFontResourceW = ctypes.windll.gdi32.RemoveFontResourceW

CACHE_DIR = Path(__file__).resolve().parent / "data" / "font_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_REGISTERED_FONTS = {}

def get_installed_font_path(font_family: str) -> str | None:
    """Windowsのフォントフォルダから指定ファミリのTTF/OTFを探す"""
    search_dirs = [
        Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    ]
    
    # 代表的なファイル名パターン
    clean_name = font_family.replace(" ", "").lower()
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                f_clean = f.name.replace(" ", "").lower()
                if clean_name in f_clean or "notosansjp" in f_clean:
                    return str(f)
    return None

def ensure_static_font_registered(font_name: str, weight: int) -> str:
    """
    バリアブルフォントから指定ウェイトのStaticフォントを生成してGDIに一時登録し、
    GDIが100%正しく認識できるフォントファミリー名を返す。
    """
    cache_key = f"{font_name}_{weight}"
    if cache_key in _REGISTERED_FONTS:
        return _REGISTERED_FONTS[cache_key]

    vf_path = get_installed_font_path(font_name)
    if not vf_path or not os.path.exists(vf_path):
        return font_name  # 見つからなければ元のフォント名を返す

    try:
        font = TTFont(vf_path)
        if "fvar" not in font:
            return font_name  # バリアブルフォントでなければそのまま返す
            
        custom_family_name = f"AGY_{font_name.replace(' ', '')}_{weight}"
        output_ttf = CACHE_DIR / f"{custom_family_name}.ttf"
        
        if not output_ttf.exists():
            # 特定ウェイトにインスタンス化
            instantiateVariableFont(font, {"wght": weight}, inplace=True, overlap=False)
            
            # NameTable書き換え
            name_table = font["name"]
            for record in name_table.names:
                if record.nameID in (1, 4, 6):
                    new_str = custom_family_name
                    record.string = new_str.encode("utf-16-be") if record.isUnicode() else new_str.encode("mac-roman", "ignore")
            font.save(str(output_ttf))
            
        # GDIに登録
        res = AddFontResourceW(str(output_ttf))
        if res > 0:
            # GDIフォントキャッシュを更新
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001D, 0, 0, 0, 1000, None)
            _REGISTERED_FONTS[cache_key] = custom_family_name
            return custom_family_name
    except Exception as e:
        print(f"Font instantiation failed: {e}")

    return font_name

def cleanup_registered_fonts():
    for cache_key, custom_name in list(_REGISTERED_FONTS.items()):
        ttf_path = CACHE_DIR / f"{custom_name}.ttf"
        if ttf_path.exists():
            RemoveFontResourceW(str(ttf_path))
    _REGISTERED_FONTS.clear()
