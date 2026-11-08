"""字幕の1行容量・最大行数に収める折り返しモデル。

video_editor の ``src/shared/subtitle-layout.ts`` (``layoutSubtitleText``) と同じ
幅モデルを使う: 全角=1、ASCII(U+0000-U+00FF)=0.58、空白=0.45 を1行の容量
(全角何文字ぶんか)と比べる。単位(文節/単語)は途中で折らず、1単位が1行に
入らない場合だけその単位内を文字単位で折る。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

ASCII_WIDTH = 0.58
SPACE_WIDTH = 0.45
_EPSILON = 1e-9


def char_width(character: str) -> float:
    if character.isspace():
        return SPACE_WIDTH
    if ord(character) <= 0xFF:
        return ASCII_WIDTH
    return 1.0


def text_width(text: str) -> float:
    return sum(char_width(character) for character in text)


def count_lines(units: Sequence[str], capacity: float) -> int:
    """単位列を容量 ``capacity`` で折り返したときの行数 (エディタの折り返しと同じ貪欲法)。"""
    if capacity <= 0:
        raise ValueError("line capacity must be positive")
    limit = capacity + _EPSILON
    lines = 0
    current = 0.0
    for unit in units:
        if not unit:
            continue
        width = text_width(unit)
        if width <= limit:
            if current and current + width > limit:
                lines += 1
                current = text_width(unit.lstrip())
            else:
                current += width
            continue
        # 1単位が1行より広い: この単位だけ文字単位で折る。
        if current:
            lines += 1
            current = 0.0
        for character in unit:
            character_width = char_width(character)
            if current and current + character_width > limit:
                lines += 1
                current = 0.0
            current += character_width
    if current:
        lines += 1
    return lines


def fits(units: Sequence[str], capacity: float, max_lines: int) -> bool:
    return count_lines(units, capacity) <= max_lines


def split_to_fit(
    units: Sequence[str],
    capacity: float,
    max_lines: int,
    *,
    is_soft_boundary: Callable[[str], bool] | None = None,
    soft_flags: Sequence[bool] | None = None,
) -> list[int]:
    """単位列を「各ブロックが max_lines 行以内」になる最少ブロックへ分ける。

    戻り値は各ブロックの末尾単位インデックス(昇順)。ブロック数が同じなら幅が
    均等な分割を優先し、文末・読点などの境界で終わる分割を少し優先する。
    1単位だけで行数を超えるブロックは分割できないためそのまま許容する。
    """
    count = len(units)
    if count == 0:
        return []
    widths = [text_width(unit) for unit in units]
    budget = capacity * max_lines
    # best[i] = (ブロック数, 幅の二乗和 - 境界ボーナス, 直前の開始位置)
    best: list[tuple[int, float, int] | None] = [None] * (count + 1)
    best[0] = (0, 0.0, -1)
    for end in range(1, count + 1):
        block_width = 0.0
        for start in range(end - 1, -1, -1):
            block_width += widths[start]
            block = units[start:end]
            if end - start > 1 and not fits(block, capacity, max_lines):
                break
            previous = best[start]
            if previous is None:
                continue
            bonus = 0.0
            if end < count and (
                (soft_flags is not None and soft_flags[end - 1])
                or (soft_flags is None and is_soft_boundary is not None and is_soft_boundary(units[end - 1]))
            ):
                bonus = 0.3 * budget * budget
            candidate = (previous[0] + 1, previous[1] + block_width * block_width - bonus, start)
            current = best[end]
            if current is None or candidate[:2] < current[:2]:
                best[end] = candidate
    ends: list[int] = []
    cursor = count
    while cursor > 0:
        entry = best[cursor]
        assert entry is not None
        ends.append(cursor - 1)
        cursor = entry[2]
    ends.reverse()
    return ends
