"""Whisperの日本語単語トークン(ほぼ1文字)を文節単位へまとめる。

字幕の区切りをトークン境界でしか選べないため、単語の途中で切れないよう
fugashi(UniDic)の形態素解析で文節境界を求め、境界に一致するトークン境界
だけを残す。解析器が使えなければ入力をそのまま返す。
"""
from __future__ import annotations

_tagger = None
_tagger_failed = False

# 直前が助詞のときは、非自立可能な語でも新しい文節として始める(「に行っ」など)。
_CONTENT_POS = {"名詞", "動詞", "形容詞", "形状詞", "副詞", "代名詞", "連体詞", "接続詞", "感動詞"}
_ALWAYS_ATTACH_POS = {"助詞", "助動詞", "接尾辞"}
# 直後の名詞と複合語にならない形式名詞(「こと|面接官」)。
_FORMAL_NOUNS = {"こと", "もの", "ところ", "とき", "ため", "わけ", "はず", "ほう", "方", "の"}
# 1単位がこの文字数を超える場合は、形態素境界でも分割する(字幕の最大文字数より十分小さく)。
MAX_UNIT_CHARS = 20
_SYMBOL_POS = {"記号", "補助記号", "空白"}


def _get_tagger():
    global _tagger, _tagger_failed
    if _tagger is None and not _tagger_failed:
        try:
            import fugashi

            _tagger = fugashi.Tagger()
        except Exception:
            _tagger_failed = True
    return _tagger


def _starts_phrase(pos1: str, pos2: str, prev_pos1: str | None, prev_pos2: str | None, prev_is_prefix: bool, prev_surface: str = "") -> bool:
    if prev_pos1 is None:
        return True
    if prev_is_prefix:
        return False  # 接頭辞は次の語と結合する
    if pos1 in _SYMBOL_POS or pos1 in _ALWAYS_ATTACH_POS:
        return False
    if pos1 == "形状詞" and pos2 == "助動詞語幹":
        return False  # 「ように」「みたいな」の よう/みたい
    if pos1 == "接頭辞":
        return True
    if pos2 in {"非自立可能", "非自立"}:
        # 動詞・形容詞・名詞の補助(「開催|さ|れ」「願い|し|ます」「なかっ」)は前へ付ける。
        if prev_pos1 == "助詞":
            return not (prev_pos2 == "接続助詞")
        return False
    if pos1 == "名詞" and prev_pos1 == "名詞" and prev_surface in _FORMAL_NOUNS:
        return True
    if pos1 == "名詞" and prev_pos1 == "名詞" and pos2 != "固有名詞":
        return False  # 複合名詞(オフ+会、数詞の連続)
    if pos1 == "名詞" and prev_pos1 == "名詞" and prev_pos2 == "固有名詞":
        return False
    return pos1 in _CONTENT_POS


def bunsetsu_boundaries(text: str) -> tuple[set[int], set[int]] | None:
    """(文節の開始位置, 形態素の開始位置)の文字オフセット集合。解析できなければNone。"""
    tagger = _get_tagger()
    if tagger is None or not text:
        return None
    try:
        morphemes = list(tagger(text))
    except Exception:
        return None
    boundaries: set[int] = set()
    morpheme_starts: set[int] = set()
    offset = 0
    prev = None
    prev_is_prefix = False
    prev_surface = ""
    for morpheme in morphemes:
        surface = morpheme.surface
        feature = morpheme.feature
        pos1 = getattr(feature, "pos1", "") or ""
        pos2 = getattr(feature, "pos2", "") or ""
        # MeCabは空白を落とすので、表層形の前の空白は次の形態素へ含める。
        found = text.find(surface, offset)
        if found < 0 or text[offset:found].strip():
            return None
        morpheme_starts.add(offset)
        if _starts_phrase(pos1, pos2, prev[0] if prev else None, prev[1] if prev else None, prev_is_prefix, prev_surface):
            boundaries.add(offset)
        prev = (pos1, pos2)
        prev_surface = surface
        prev_is_prefix = pos1 == "接頭辞"
        offset = found + len(surface)
    if text[offset:].strip():
        return None
    return boundaries, morpheme_starts


def _display_length(text: str, hide_punctuation: bool) -> int:
    """単位の長さ。非表示にする句読点(、。など)は数えない。"""
    if not hide_punctuation:
        return len(text)
    from caption_text import normalize_caption_display_text

    return len(normalize_caption_display_text(text, "hide"))


def _merge_segment_words(words: list[dict], max_unit_chars: int, hide_punctuation: bool = False) -> list[dict]:
    texts = [str(word.get("word", "")) for word in words]
    analysis = bunsetsu_boundaries("".join(texts))
    if analysis is None:
        return words
    boundaries, morpheme_starts = analysis
    merged: list[dict] = []
    offset = 0
    for word, text in zip(words, texts):
        too_long = (
            merged
            and offset in morpheme_starts
            and _display_length(merged[-1]["word"] + text, hide_punctuation) > max_unit_chars
        )
        if merged and offset not in boundaries and not too_long:
            # 文節の途中: 直前の単位へ連結(文字列は連結のみ、時刻は最初のstartと最後のend)。
            unit = merged[-1]
            unit["word"] += text
            unit["end"] = word.get("end", unit["end"])
            if "probability" in unit and "probability" in word:
                unit["probability"] = min(unit["probability"], word["probability"])  # 確率は最小値
        else:
            merged.append(dict(word, word=text))
        offset += len(text)
    return merged


def merge_bunsetsu_units(segments: list[dict], max_chars: int | None = None, hide_punctuation: bool = False) -> list[dict]:
    """各セグメントのwordsを文節単位へまとめた新しいリストを返す(入力は変更しない)。

    max_charsは字幕1行の最大文字数(呼び出し側が有効上限min(maxChars, 1行容量)を渡す)。
    1単位はこれを超えないよう、形態素の境界で分ける(形態素の途中では分けない)。
    hide_punctuationが真なら、表示しない句読点は文字数に数えない。
    """
    max_unit_chars = MAX_UNIT_CHARS if not max_chars else max(1, min(MAX_UNIT_CHARS, int(max_chars)))
    result = []
    for segment in segments:
        words = segment.get("words") if isinstance(segment, dict) else None
        if not words or not all(isinstance(w, dict) and isinstance(w.get("word", ""), str) for w in words):
            result.append(segment)
            continue
        copied = dict(segment)
        copied["words"] = _merge_segment_words([dict(w) for w in words], max_unit_chars, hide_punctuation)
        result.append(copied)
    return result
