"""字幕のsource/display差分を一箇所で定義する共通ユーティリティ。"""

from __future__ import annotations

import re
from typing import Iterable


DISPLAY_PUNCTUATION_PATTERN = re.compile(r"[、。！？!?…・〜～]+")
# 「句読点を表示しない」: 読点・句点(全角/半角)だけを除く。?!…などは残す。
# 半角の , . は数字に挟まれたもの(3.14, 1,000)だけ残す。
HIDE_PUNCTUATION_PATTERN = re.compile(r"[、。，．]|(?<!\d)[,.]|[,.](?!\d)")
TEXT_STATUSES = frozenset({"as_transcribed", "display_normalized", "manually_corrected"})
# legacy: 旧仕様(! ? … ・ 〜 も除去)。hide: 読点・句点だけ除去。show: 何も除かない。
PUNCTUATION_MODES = ("legacy", "hide", "show")


def normalize_caption_display_text(text: object, mode: str = "legacy") -> str:
    """字幕の表示文字列を、source文字列から導出する。

    ``mode`` は句読点の扱い。``legacy`` は従来仕様(!?…も除去)、``hide`` は
    読点・句点だけ除去(区切りの空白は入れず、そのまま詰める)、``show`` は無加工。
    """
    value = str(text)
    if mode == "hide":
        return HIDE_PUNCTUATION_PATTERN.sub("", value)
    if mode == "show":
        return value
    return DISPLAY_PUNCTUATION_PATTERN.sub("", value)


def punctuation_mode(hide_punctuation: bool) -> str:
    return "hide" if hide_punctuation else "show"


def is_display_normalization(source_text: object, display_text: object) -> bool:
    """displayがsourceからの既知の表示正規化(旧仕様・句読点非表示)で導出できるか。"""
    source = str(source_text)
    display = str(display_text)
    return any(normalize_caption_display_text(source, mode) == display for mode in ("hide", "legacy"))


def resolve_display_mode(source_text: object, display_text: object) -> str:
    """display_textを導出する正規化modeを返す(不明ならlegacy)。"""
    source = str(source_text)
    display = str(display_text)
    for mode in ("hide", "legacy"):
        if normalize_caption_display_text(source, mode) == display:
            return mode
    return "legacy"


def classify_caption_text_status(source_text: object, display_text: object) -> str:
    source = str(source_text)
    display = str(display_text)
    if display == source:
        return "as_transcribed"
    if is_display_normalization(source, display):
        return "display_normalized"
    return "manually_corrected"


def align_caption_word_ownership(captions: list[dict], words: list[dict], mode: str = "legacy") -> list[dict]:
    """字幕表示文字列を、単語途中で切らずに連続word範囲へ対応付ける。

    句読点だけのsource wordは直前のcaptionへ含める。先頭にある場合だけ
    次のcaptionへ含める。この規則により、editorへ空文字wordを出さず、
    source ownershipは失わない。
    """
    if not captions:
        raise ValueError("字幕が空です。")
    if not words:
        raise ValueError("字幕に使用できる発話語がありません。")

    result: list[dict] = []
    cursor = 0
    for caption_index, caption in enumerate(captions, 1):
        display_text = str(caption.get("text", ""))
        if not display_text:
            raise ValueError(f"字幕{caption_index}の本文が空です。")

        start_id = cursor
        # 前captionが存在しない場合の先頭句読点だけを次のcaptionへ持たせる。
        while cursor < len(words) and not normalize_caption_display_text(_word_text(words[cursor]), mode):
            cursor += 1

        assembled = ""
        while cursor < len(words):
            token = normalize_caption_display_text(_word_text(words[cursor]), mode)
            if not token:
                # 直前の表示語に続く句読点は直前captionへ帰属させる。
                cursor += 1
                continue
            candidate = assembled + token
            if not display_text.startswith(candidate):
                if assembled == display_text or display_text.startswith(assembled):
                    raise ValueError(
                        f"caption_boundary_inside_word: 字幕{caption_index}の境界がword途中です。"
                    )
                raise ValueError(
                    f"caption_word_ownership_mismatch: 字幕{caption_index}をword列へ対応付けできません。"
                )
            assembled = candidate
            cursor += 1
            if assembled == display_text:
                # 表示されない句読点wordは、直前の表示captionへ含める。
                while cursor < len(words) and not normalize_caption_display_text(_word_text(words[cursor]), mode):
                    cursor += 1
                break

        if assembled != display_text:
            raise ValueError(
                f"caption_word_ownership_mismatch: 字幕{caption_index}をword列へ対応付けできません。"
            )
        end_id = cursor - 1
        if end_id < start_id:
            raise ValueError(f"字幕{caption_index}にsource wordがありません。")
        source_text = "".join(_word_text(word) for word in words[start_id : end_id + 1])
        status = classify_caption_text_status(source_text, display_text)
        result.append(
            {
                **caption,
                "start_word_id": start_id,
                "end_word_id": end_id,
                "source_text": source_text,
                "text_status": status,
            }
        )

    if cursor != len(words):
        # 残った句読点だけのwordは最後のcaptionへ含める。
        if result and all(
            not normalize_caption_display_text(_word_text(word), mode) for word in words[cursor:]
        ):
            result[-1]["end_word_id"] = len(words) - 1
            result[-1]["source_text"] = "".join(
                _word_text(word)
                for word in words[result[-1]["start_word_id"] :]
            )
            result[-1]["text_status"] = classify_caption_text_status(
                result[-1]["source_text"], result[-1]["text"]
            )
        else:
            raise ValueError("末尾のsource wordが字幕へ対応付けできません。")
    return result


def _word_text(word: dict) -> str:
    return str(word.get("word", word.get("text", "")))


def writer_text_status(source_text: object, display_text: object) -> str:
    """字幕ライターの本文の状態。

    元の単語から文字を消しただけの本文は ai_cleaned、聞き間違いを直したり
    ？～を付けたりした本文は ai_corrected（人が確かめるとよい字幕）。
    """
    status = classify_caption_text_status(source_text, display_text)
    if status != "manually_corrected":
        return status
    return "ai_cleaned" if _is_deletion_derived(str(source_text), str(display_text)) else "ai_corrected"


def _is_deletion_derived(source: str, display: str) -> bool:
    if not display:
        return False
    cursor = 0
    for character in display:
        cursor = source.find(character, cursor)
        if cursor < 0:
            return False
        cursor += 1
    return True
