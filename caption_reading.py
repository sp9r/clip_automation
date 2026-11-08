"""字幕ライターの「聞き間違いの修正」を、読みの近さで確かめる。

音声認識の誤り（監視→看守、コーティング→ゴースティング）は読みがほとんど同じで、
言い換えや作文（例えば→来るんじゃないですか）は読みが大きく違う。元の単語と
本文をカタカナの読みにして比べ、読みが近い修正だけを通す。英字と数字の綴り
（VTuber、240）は読みを決めきれないので比べる対象から外し、長さだけを見る。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_tagger = None
_IGNORED = re.compile(r"[\s、。，．,.!！?？～〜ー「」『』（）()…・:：;；\"'“”‘’]")
_WILDCARD = re.compile(r"[A-Za-z0-9０-９Ａ-Ｚａ-ｚ%％&＆+＋-]+")
# 修正として通す読みの近さ。ユーザーが過去の切り抜きで直した1,114件で決めた（2026-10-22）。
MIN_READING_SIMILARITY = 0.5


def _get_tagger():
    global _tagger
    if _tagger is None:
        import fugashi

        _tagger = fugashi.Tagger()
    return _tagger


def _to_katakana(text: str) -> str:
    return "".join(chr(ord(character) + 0x60) if "ぁ" <= character <= "ゖ" else character for character in text)


def reading(text: str) -> str:
    """表示用の記号を除いたカタカナの読み。英字・数字は読みに含めない。"""
    text = _WILDCARD.sub(" ", text)
    pieces = []
    for morpheme in _get_tagger()(text):
        kana = getattr(morpheme.feature, "kana", None)
        pieces.append(kana if kana and kana != "*" else morpheme.surface)
    return _IGNORED.sub("", _to_katakana("".join(pieces)))


def reading_similarity(source: str, text: str) -> float:
    left, right = reading(source), reading(text)
    if not left and not right:
        return 1.0
    if not left or not right:
        # 片方が英字・数字だけ（ブイチューバ→VTuber、にひゃく→200）。長さが極端でなければ通す。
        return 1.0 if _WILDCARD.search(source + text) and len(left + right) <= 12 else 0.0
    spelled = bool(_WILDCARD.search(text)) and not _WILDCARD.search(source)
    if len(right) > 1.5 * len(left) + 2 or (not spelled and len(right) < 0.75 * len(left) - 1):
        # 本文が元の言葉よりずっと長いのは隣の字幕の言葉の書き込みか付け足し、ずっと短いのは言葉の抜け。
        return 0.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    if spelled:
        # 英字・数字に直した所（おんりアップ→OnlyUp）は読みが消えるので、残りが元にあるかだけを見る。
        return sum(block.size for block in matcher.get_matching_blocks()) / len(right)
    return matcher.ratio()


def is_plausible_correction(source: str, text: str) -> bool:
    return reading_similarity(source, text) >= MIN_READING_SIMILARITY
