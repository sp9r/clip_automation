"""字幕レイアウト設定の共通検証と既定値。

``max_chars`` は既存の字幕ブロック全体の文字数上限であり、
rendererの1行幅ではない。``max_lines`` は字幕表示側へ渡すレイアウト制約で、
このリポジトリの字幕生成器が表示行を再分割することはない。
"""

from __future__ import annotations

from numbers import Integral


DEFAULT_MAX_CHARS = 32
DEFAULT_MAX_LINES = 2
MIN_MAX_CHARS = 8
MAX_MAX_CHARS = 80
MIN_MAX_LINES = 1
MAX_MAX_LINES = 16


def _require_integer(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name}は整数で指定してください。")
    return int(value)


def validate_max_chars(value: object, *, default: int = DEFAULT_MAX_CHARS) -> int:
    """字幕ブロック全体の文字数上限を検証する。"""

    normalized = _require_integer(default if value is None else value, name="字幕の文字数上限")
    if not MIN_MAX_CHARS <= normalized <= MAX_MAX_CHARS:
        raise ValueError(
            f"字幕の文字数上限は{MIN_MAX_CHARS}〜{MAX_MAX_CHARS}で指定してください。"
        )
    return normalized


def validate_max_lines(value: object, *, default: int = DEFAULT_MAX_LINES) -> int:
    """字幕の表示行数制約を検証する。

    1〜16行を許容し、既存UI・manifestから誤って渡されるboolを整数扱いしない。
    """

    normalized = _require_integer(default if value is None else value, name="字幕の最大行数")
    if not MIN_MAX_LINES <= normalized <= MAX_MAX_LINES:
        raise ValueError(
            f"字幕の最大行数は{MIN_MAX_LINES}〜{MAX_MAX_LINES}で指定してください。"
        )
    return normalized


def normalize_caption_settings(settings: dict | None = None) -> dict:
    """字幕関連設定だけを後方互換的に正規化する。"""

    values = dict(settings or {})
    values["max_chars"] = validate_max_chars(values.get("max_chars"))
    values["max_lines"] = validate_max_lines(values.get("max_lines"))
    return values

