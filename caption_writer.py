"""LLMに字幕の区切りと本文を書かせる。

LLMの答えは「単語番号の範囲＋本文」。本文の作り方は2通りある。

- correct(既定): スペクトルが過去の切り抜きで手直しした字幕の作り方。音声認識の
  聞き間違いを文脈から直し、？～（笑）を付け、フィラーは消す（過去の字幕にフィラーが
  残っているのは手間で消していないだけ、と本人。2026-10-22）。
  直した本文は、元の単語と読みが近いことを確かめる（caption_reading）。
- clean: フィラー・繰り返し・句読点を消すだけ。1文字ずつ照合して確かめる。

合格しなかった字幕だけ元の言葉に戻す。合格した字幕は後段で書き換えない。
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from caption_layout import split_to_fit
from caption_reading import is_plausible_correction, reading_similarity
from caption_text import normalize_caption_display_text

# 言いよどみのフィラー。単語のかたまりの先頭から始まるものだけを消せる。
FILLER_PATTERNS = [
    "えーっと", "えーと", "えっと", "え+ー+", "あー+", "あのー+", "あの", "うーん", "う+ー+", "ん+ー+",
    "まあ", "まぁ", "なんか", "そのー+", "その", "ねえ",
]
# 1文字の間投詞（驚き・反応にもなる）は、それだけで1かたまりか句読点が続くときだけ消せる。
SHORT_FILLERS = ("え", "あ", "お", "ね")
_FILLER_RE = [re.compile(pattern) for pattern in FILLER_PATTERNS]
_PUNCTUATION = set("、。，．,.　 ")
# ?! は残すよう指示するが、LLMが消しても内容は変わらないので許す。
_MARKS = set("?？!！")
MAX_UNITS_PER_BATCH = 70
MAX_ATTEMPTS = 2
WRITER_STYLES = ("correct", "clean")
GLOSSARY_PATH = Path(__file__).with_name("caption_glossary.txt")
# correct で句読点を表示しないとき、LLMが書いた 、。 を消す。？！～（笑）は残す。
_HIDDEN_PUNCTUATION = str.maketrans("", "", "、。，．")
# スペクトルの字幕の疑問符・感嘆符は全角。
_FULL_WIDTH_MARKS = str.maketrans({"?": "？", "!": "！"})


def load_glossary(path: Path = GLOSSARY_PATH) -> list[str]:
    """よく出る言葉の表記（1行1語、#から後は説明）。

    「シバエリ: 柴犬, 芝居」のように書くと、音声認識での聞き間違いの例として
    「シバエリ（柴犬・芝居と認識されがち）」の形でLLMに渡す。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    terms: list[str] = []
    for line in lines:
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        term, _, aliases = entry.partition(":")
        heard = [alias.strip() for alias in aliases.split(",") if alias.strip()]
        terms.append(f"{term.strip()}（{'・'.join(heard)}と認識されがち）" if heard else term.strip())
    return terms


_LEADING_FILLER_SPACE = re.compile(r"^(?:ま|まあ|あ|え|いや|あの|なんか) ")


def tidy_spaces(text: str) -> str:
    """半角スペースは、文頭のつなぎの後と、長めの言葉どうしの区切りにだけ残す。

    LLMは「達 が やっ た の」のように語ごとに空けることがある。スペクトルの字幕で
    スペースが入るのは「ま 薄利多売だよね」「こんばんは 聞こえてますか」の形。
    """
    text = re.sub(r"[ \u3000]+", " ", text).strip()
    lead = ""
    match = _LEADING_FILLER_SPACE.match(text)
    if match:
        lead, text = match.group(0), text[match.end():]
    chunks = text.split(" ")
    result = chunks[0]
    for chunk in chunks[1:]:
        left = result.rsplit(" ", 1)[-1]
        result += (" " if len(left) >= 3 and len(chunk) >= 3 else "") + chunk
    return lead + result


class CaptionWriterError(ValueError):
    pass


def _display_length(text: str) -> int:
    return len(normalize_caption_display_text(text, "hide").replace(" ", "").replace("　", ""))


def _strip(text: str) -> str:
    return "".join(character for character in text if character not in _PUNCTUATION and character not in _MARKS)


def align_deletions(units: list[str], text: str, *, allow_punctuation: bool) -> list[str] | None:
    """text を units から削除だけで作れるなら、各 unit に残った文字列を返す。

    消してよいもの: かたまりの先頭から始まるフィラー、句読点（許可時）、?!、
    直前か直後のかたまりと同じ内容のかたまり丸ごと（言い直し・繰り返し）。
    """
    source = "".join(units)
    n, m = len(source), len(text)
    owner: list[int] = []
    unit_starts: dict[int, int] = {}
    unit_ends: set[int] = set()
    offset = 0
    for index, unit in enumerate(units):
        unit_starts[offset] = index
        owner.extend([index] * len(unit))
        offset += len(unit)
        unit_ends.add(offset)
    # skips[i]: 位置 i から丸ごと消せる区間の終端
    skips: list[list[int]] = [[] for _ in range(n + 1)]
    for i, index in unit_starts.items():
        if i >= n:
            continue
        for pattern in _FILLER_RE:
            match = pattern.match(source, i)
            if match and match.end() > i:
                skips[i].append(match.end())
        if source[i] in SHORT_FILLERS and (i + 1 in unit_ends or (i + 1 < n and source[i + 1] in _PUNCTUATION)):
            skips[i].append(i + 1)
        stripped = _strip(units[index])
        neighbours = [units[index - 1]] if index > 0 else []
        neighbours += [units[index + 1]] if index + 1 < len(units) else []
        if stripped and any(_strip(other) == stripped for other in neighbours):
            skips[i].append(i + len(units[index]))
    # reachable[i][j]: source[i:] から text[j:] を作れるか
    reachable = [[False] * (m + 1) for _ in range(n + 1)]
    reachable[n][m] = True
    for i in range(n - 1, -1, -1):
        character = source[i]
        removable = character in _MARKS or (allow_punctuation and character in _PUNCTUATION)
        for j in range(m, -1, -1):
            reachable[i][j] = (
                (j < m and character == text[j] and reachable[i + 1][j + 1])
                or (removable and reachable[i + 1][j])
                or any(reachable[end][j] for end in skips[i])
            )
    if not reachable[0][0]:
        return None
    kept = [""] * len(units)
    i = j = 0
    while i < n:
        character = source[i]
        if j < m and character == text[j] and reachable[i + 1][j + 1]:
            kept[owner[i]] += character
            i, j = i + 1, j + 1
            continue
        if (character in _MARKS or (allow_punctuation and character in _PUNCTUATION)) and reachable[i + 1][j]:
            i += 1
            continue
        i = next(end for end in skips[i] if reachable[end][j])
    return kept


def is_filler_deletion(units: list[str], text: str, *, allow_punctuation: bool) -> bool:
    return align_deletions(units, text, allow_punctuation=allow_punctuation) is not None


def build_writer_prompt(
    units: list[str],
    first_index: int,
    *,
    previous_captions: list[str],
    max_chars: int,
    line_capacity: int | None,
    max_lines: int,
    hide_punctuation: bool,
    feedback: str = "",
    style: str = "clean",
    glossary: list[str] | None = None,
    topic: str = "",
) -> str:
    if style == "correct":
        return _correct_prompt(
            units, first_index, previous_captions=previous_captions, max_chars=max_chars, line_capacity=line_capacity,
            max_lines=max_lines, hide_punctuation=hide_punctuation, feedback=feedback, glossary=glossary or [], topic=topic,
        )
    last_index = first_index + len(units) - 1
    per_line = line_capacity or max_chars
    listing = "\n".join(f"{first_index + offset}: {unit}" for offset, unit in enumerate(units))
    context = " / ".join(previous_captions[-3:]) if previous_captions else "(なし)"
    punctuation_rule = (
        "句読点（、。）は本文に入れない。?や!はそのまま残す。"
        if hide_punctuation
        else "句読点は単語に含まれるとおりに残す。"
    )
    retry = f"\n【前回の答えの問題点】\n{feedback}\nこの問題を直して、もう一度すべてを出力してください。\n" if feedback else ""
    return f"""あなたは日本語YouTube（ゲーム実況・雑談の切り抜き）の字幕編集者です。
音声認識の単語列から、視聴者がテンポよく読める字幕を作ってください。

【区切り方】
- 意味のまとまり（文・文節・言い切り）で区切る。1枚は{per_line}文字×{max_lines}行まで、表示する文字数で合計{max_chars}文字以内。
- 長い文は、読みやすい位置で2枚以上に分ける。短すぎる断片（1〜2文字だけの字幕）は前後とつなげる。ただし「え?」「マジか」のような反応は1枚にしてよい。
- 文末の「か」「よ」「ね」「な」「わ」や「?」を次の字幕の頭に回さない。「かもしれない」などの途中で切らない。
- 話し手が変わったと分かる所、話題が変わる所では切る。

【本文の作り方】
- 本文は、その範囲の単語をつなげた文字列から、言いよどみのフィラー（えー、えーと、えっと、あー、あのー、うーん、んー、つなぎの「まあ」「なんか」「その」など）を消しただけにする。
- 同じ言葉をすぐ繰り返している所（「これだ、これだ」「よし来た。よし来た」）や言い直しは、1回にまとめてよい。
- 驚き・反応の「え」「あ」「お」は、意味があれば残す。言いよどみなら消す。
- {punctuation_rule}
- 言い換え・言葉の付け足し・漢字の変更・聞き間違いの推測修正は禁止。単語にない文字は1文字も書かない。
- フィラーだけの範囲は text を "" にする（字幕にしない）。

【出力】
- 単語番号 {first_index} から {last_index} までを、ちょうど1回ずつ、順番どおりに使う。各字幕は [最初の番号, 最後の番号, "本文"]。
- JSONだけを出力する: {{"c":[[{first_index},終わりの番号,"本文"], ...]}}

【例】（1行12文字・1行の場合）
単語: 0: え、 1: 今日は 2: えーと、 3: フォートナイトを 4: やって 5: いきます。 6: え、 7: なんだ 8: これ。
出力: {{"c":[[0,3,"今日はフォートナイトを"],[4,5,"やっていきます"],[6,8,"えなんだこれ"]]}}
{retry}
【直前の字幕】{context}

【単語】
{listing}
""".strip()


def _correct_prompt(
    units: list[str],
    first_index: int,
    *,
    previous_captions: list[str],
    max_chars: int,
    line_capacity: int | None,
    max_lines: int,
    hide_punctuation: bool,
    feedback: str,
    glossary: list[str],
    topic: str,
) -> str:
    last_index = first_index + len(units) - 1
    per_line = line_capacity or max_chars
    listing = "\n".join(f"{first_index + offset}: {unit}" for offset, unit in enumerate(units))
    context = " / ".join(previous_captions[-4:]) if previous_captions else "(なし)"
    terms = "、".join(glossary) if glossary else "(なし)"
    punctuation_rule = "句読点（、。）は使わない。" if hide_punctuation else "句読点（、。）は単語にあるとおりに残す。"
    retry = f"\n【前回の答えの問題点】\n{feedback}\nこの問題を直して、もう一度すべてを出力してください。\n" if feedback else ""
    topic_line = f"\n【動画のタイトル（話題の手がかり）】{topic}" if topic else ""
    return f"""あなたは日本語YouTube（フォートナイト実況者シャッポの配信の切り抜き）の字幕編集者です。
音声認識の単語列から、編集者が手直しを終えた状態の字幕を作ってください。

【区切り方】
- 1枚は短くする。だいたい8〜12文字で、長くても{max_chars}文字（1行{per_line}文字×{max_lines}行）まで。言い切り・文節・息つぎの所で区切る。
- 語や文節の途中では絶対に切らない（「何回 / ぐらい」「やってた / だ」のような切り方はしない）。前の字幕の終わりの助詞・語尾を次の字幕の頭に回さない。
- 「え？」「マジか」のような短い反応は1枚にしてよい。1〜2文字だけの断片は前後とつなげる。
- 文末の「か」「よ」「ね」「な」「わ」「？」を次の字幕の頭に回さない。話題や話し手が変わる所では切る。

【本文の作り方】
- 言いよどみのフィラー（えー、えーと、えっと、あー、あのー、うーん、んー、つなぎの「まあ」「なんか」「その」）は消す。驚き・反応の「え？」「あ」「お」や、意味のある「なんか」は残す。
- 同じ言葉をすぐ繰り返している所（「これだ、これだ」）や言い直しは1回にまとめてよい。それ以外の言葉は省略・要約しない。
- 単語の間は空けない。フィラーだけの範囲は text を "" にする（字幕にしない）。
- 意味が通らない所は、前後の字幕・動画の話題から音声認識の聞き間違いを直す。直すのは読みが近い言葉だけ（監視は→看守は、何でコーティング→何でゴースティング、今バタが→コンバーターが）。
- 下の「よく出る言葉」に当てはまるものは、その表記で書く。（）の中は音声認識がよく間違える形で、そう認識されていたら直す。
- 英語の名前・サービス名は正しい綴りにする（ゆーちゅーぶ→YouTube、ブイチューバ→VTuber、オンリーリアップ→OnlyUp）。数は算用数字にする（にひゃくよんじゅー歳→240歳）。
- 疑問・聞き返しには「？」、語尾を伸ばす所には「～」、笑っている所には「（笑）」を付けてよい。{punctuation_rule}
- 話していない内容の付け足し、言い換え、読みが大きく違う言葉への書き換えはしない。分からない所は単語のまま書く。

【よく出る言葉】{terms}{topic_line}

【出力】
- 単語番号 {first_index} から {last_index} までを、ちょうど1回ずつ、順番どおりに使う。各字幕は [最初の番号, 最後の番号, "本文"]。
- JSONだけを出力する: {{"c":[[{first_index},終わりの番号,"本文"], ...]}}

【例】（1行14文字・1行の場合）
単語: 0: まあ、 1: 薄利 2: 多売 3: だよね。 4: 監視 5: は 6: 何で 7: コーティング 8: してんの? 9: えーと、 10: ブイ 11: チューバ 12: って 13: さ 14: 擦ってる 15: やつ 16: って 17: わら
出力: {{"c":[[0,3,"薄利多売だよね"],[4,5,"看守は"],[6,8,"何でゴースティングしてんの？"],[9,13,"VTuberってさ"],[14,17,"擦ってるやつって（笑）"]]}}
{retry}
【直前の字幕】{context}

【単語】
{listing}
""".strip()


def _parse_response(response: str, first_index: int, last_index: int) -> list[tuple[int, int, str]]:
    match = re.search(r"\{.*\}", response, re.DOTALL)
    if not match:
        raise CaptionWriterError("JSONがありません。")
    data = json.loads(match.group(0))
    items = data.get("c") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise CaptionWriterError('"c" が空か配列ではありません。')
    result: list[tuple[int, int, str]] = []
    expected = first_index
    for item in items:
        if not (isinstance(item, list) and len(item) == 3 and all(isinstance(value, int) and not isinstance(value, bool) for value in item[:2]) and isinstance(item[2], str)):
            raise CaptionWriterError(f"形式が違う要素があります: {json.dumps(item, ensure_ascii=False)[:80]}")
        start, end, text = item
        if start != expected or end < start or end > last_index:
            raise CaptionWriterError(f"単語番号の範囲が連続していません（{expected}から始まるはずの所が[{start},{end}]）。")
        result.append((start, end, text))
        expected = end + 1
    if expected != last_index + 1:
        raise CaptionWriterError(f"最後の単語{last_index}まで使っていません（{expected - 1}で終了）。")
    return result


def _validate(
    parsed: list[tuple[int, int, str]],
    words: list[dict],
    *,
    max_chars: int,
    hide_punctuation: bool,
    style: str = "clean",
) -> list[tuple[int, str]]:
    """不合格の字幕を (parsed内の番号, 理由) で返す。"""
    problems: list[tuple[int, str]] = []
    for position, (start, end, text) in enumerate(parsed):
        units = [str(word["word"]) for word in words[start : end + 1]]
        source = "".join(units)
        if text == "":
            if not is_filler_deletion(units, "", allow_punctuation=True):
                problems.append((position, f"[{start},{end}] はフィラーだけではないので本文が必要です（元: {source}）。"))
            continue
        if style == "correct":
            if not is_filler_deletion(units, text.replace(" ", ""), allow_punctuation=True) and not is_plausible_correction(source, text):
                problems.append((position, f"[{start},{end}] の本文「{text}」は、元の「{source}」と読みが違いすぎます。聞き間違いの修正は読みが近い言葉だけにし、言い換えや付け足しはしないでください。"))
                continue
            # 隣の字幕の言葉を書き写していないか（自分の範囲より隣の範囲に読みが近い本文は不合格）
            own = reading_similarity(source, text)
            for neighbour in (position - 1, position + 1):
                if 0 <= neighbour < len(parsed) and parsed[neighbour][2]:
                    other_start, other_end, _ = parsed[neighbour]
                    other = "".join(str(word["word"]) for word in words[other_start : other_end + 1])
                    if reading_similarity(other, text) > own:
                        problems.append((position, f"[{start},{end}] の本文「{text}」は、隣の範囲[{other_start},{other_end}]の言葉です。各字幕にはその範囲の言葉だけを書いてください。"))
                        break
            continue
        if not is_filler_deletion(units, text, allow_punctuation=hide_punctuation):
            problems.append((position, f"[{start},{end}] の本文「{text}」は、元の「{source}」からフィラー・繰り返し・句読点を消しただけになっていません。単語にない文字を書いたり、言い換えたりしないでください。"))
    # 長すぎる字幕は作り直させず、書かれた本文のまま単語の区切りで分ける（_split_long）。
    return problems


def _source_display(words: list[dict], start: int, end: int, hide_punctuation: bool) -> str:
    source = "".join(str(word["word"]) for word in words[start : end + 1])
    return normalize_caption_display_text(source, "hide") if hide_punctuation else source


def _split_long(start: int, end: int, text: str, words: list[dict], *, max_chars: int, line_capacity: int | None, max_lines: int, allow_punctuation: bool) -> list[tuple[int, int, str]]:
    """上限を超えた字幕を、LLMが書いた本文を保ったまま単語の区切りで分ける。"""
    if not text or _display_length(text) <= max_chars or start == end:
        return [(start, end, text)]
    units = [str(word["word"]) for word in words[start : end + 1]]
    kept = align_deletions(units, text, allow_punctuation=allow_punctuation) or _map_corrected(units, text)
    capacity = line_capacity or max_chars
    # 音声認識がカタカナ語を分けていることがある(「ゴー」「スティングや」)。語の中では切らない。
    groups: list[list[int]] = []
    for index, piece in enumerate(kept):
        if groups and _continues_word(kept[groups[-1][-1]], piece):
            groups[-1].append(index)
        else:
            groups.append([index])
    group_texts = ["".join(kept[index] for index in group) for group in groups]
    ends = split_to_fit(group_texts, capacity, max_lines if line_capacity else 1)
    if len(ends) <= 1:
        return [(start, end, text)]
    pieces: list[tuple[int, int, str]] = []
    cursor = 0
    for group_end in ends:
        first, last = groups[cursor][0], groups[group_end][-1]
        pieces.append((start + first, start + last, "".join(kept[first : last + 1])))
        cursor = group_end + 1
    return pieces


def _map_corrected(units: list[str], text: str) -> list[str]:
    """直した本文を、元の単語の切れ目に合わせて単語ごとの断片に分ける（つなぐと text になる）。"""
    source = "".join(units)
    matcher = SequenceMatcher(None, source, text, autojunk=False)
    blocks = matcher.get_matching_blocks()

    def text_position(source_position: int) -> int:
        # 一致した所はそのまま、直した所は前後の一致から比例で位置を決める
        previous_source, previous_text = 0, 0
        for block in blocks:
            if source_position < block.a:
                span_source = block.a - previous_source
                span_text = block.b - previous_text
                return previous_text + (round(span_text * (source_position - previous_source) / span_source) if span_source else 0)
            if source_position <= block.a + block.size:
                return block.b + (source_position - block.a)
            previous_source, previous_text = block.a + block.size, block.b + block.size
        return len(text)

    pieces: list[str] = []
    cursor = 0
    offset = 0
    for index, unit in enumerate(units):
        offset += len(unit)
        boundary = len(text) if index == len(units) - 1 else max(cursor, min(len(text), text_position(offset)))
        pieces.append(text[cursor:boundary])
        cursor = boundary
    return pieces


_KATAKANA = re.compile(r"[\u30a0-\u30ffー]")


def _continues_word(previous: str, following: str) -> bool:
    return bool(previous and following and _KATAKANA.match(previous[-1]) and _KATAKANA.match(following[0]))


def _batches(words: list[dict], size: int = MAX_UNITS_PER_BATCH):
    start = 0
    while start < len(words):
        end = min(start + size, len(words))
        if end < len(words):
            # 文末(。?!)で終わる位置まで戻して、文の途中でバッチを切らない
            for candidate in range(end - 1, start + size // 2, -1):
                if re.search(r"[。?？!！]$", str(words[candidate]["word"])):
                    end = candidate + 1
                    break
        yield start, end
        start = end


def write_captions(
    words: list[dict],
    *,
    generate: Callable[[str, int], str],
    max_chars: int,
    max_lines: int,
    line_capacity: int | None,
    hide_punctuation: bool,
    fallback: Callable[[list[dict]], list[tuple[int, int]]],
    log: Callable[[str], None] = print,
    batch_size: int = MAX_UNITS_PER_BATCH,
    seconds_per_unit: float = 6.0,
    progress: Callable[[int, int], None] | None = None,
    style: str = "correct",
    glossary: list[str] | None = None,
    topic: str = "",
) -> tuple[list[dict], int]:
    """字幕を書き、(字幕, LLMが失敗してルール方式にしたバッチ数) を返す。

    generate(prompt, timeout) はLLMの生テキストを返す。fallback(batch) はバッチ内の
    相対範囲[(start, end), ...]を返すルール方式の区切り。
    """
    if style not in WRITER_STYLES:
        raise ValueError(f"unknown caption writer style: {style}")
    if glossary is None:
        glossary = load_glossary() if style == "correct" else []
    captions: list[dict] = []
    previous_texts: list[str] = []
    fallback_batches = 0
    rejected_captions = 0
    batches = list(_batches(words, batch_size))
    for batch_number, (batch_start, batch_end) in enumerate(batches):
        if progress:
            progress(batch_number, len(batches))
        batch = words[batch_start:batch_end]
        units = [str(word["word"]) for word in batch]
        feedback = ""
        accepted: list[tuple[int, int, str]] | None = None
        for _attempt in range(MAX_ATTEMPTS):
            prompt = build_writer_prompt(
                units,
                batch_start,
                previous_captions=previous_texts,
                max_chars=max_chars,
                line_capacity=line_capacity,
                max_lines=max_lines,
                hide_punctuation=hide_punctuation,
                feedback=feedback,
                style=style,
                glossary=glossary,
                topic=topic,
            )
            timeout = int(120 + seconds_per_unit * len(units))
            try:
                parsed = _parse_response(generate(prompt, timeout), batch_start, batch_end - 1)
                if style == "correct":
                    parsed = [
                        (start, end, tidy_spaces((text.translate(_HIDDEN_PUNCTUATION) if hide_punctuation else text).translate(_FULL_WIDTH_MARKS)))
                        for start, end, text in parsed
                    ]
                problems = _validate(parsed, words, max_chars=max_chars, hide_punctuation=hide_punctuation, style=style)
            except (CaptionWriterError, json.JSONDecodeError) as exc:
                parsed, problems = None, [(-1, str(exc))]
            last_attempt = _attempt == MAX_ATTEMPTS - 1
            if parsed is not None and (not problems or len(problems) <= 2 or last_attempt):
                # 不合格の字幕だけは元の言葉（句読点だけ除く）にし、区切りはLLMの答えを使う。
                bad = {position for position, _ in problems}
                parsed = [
                    (start, end, _source_display(words, start, end, hide_punctuation) if position in bad else text)
                    for position, (start, end, text) in enumerate(parsed)
                ]
                rejected_captions += len(bad)
                accepted = [
                    piece
                    for start, end, text in parsed
                    for piece in _split_long(start, end, text, words, max_chars=max_chars, line_capacity=line_capacity, max_lines=max_lines, allow_punctuation=hide_punctuation)
                ]
                break
            feedback = "\n".join(message for _, message in problems[:8])
            log(f"字幕ライター: 単語{batch_start}〜{batch_end - 1}の答えに問題があります（{len(problems)}件）")
        if accepted is None:
            fallback_batches += 1
            accepted = [
                (batch_start + start, batch_start + end, _source_display(words, batch_start + start, batch_start + end, hide_punctuation))
                for start, end in fallback(batch)
            ]
        for start, end, text in accepted:
            if style == "correct":
                text = text.translate(_FULL_WIDTH_MARKS).strip()
            source = "".join(str(word["word"]) for word in words[start : end + 1])
            captions.append({
                "start": words[start]["start"],
                "end": words[end]["end"],
                "text": text,
                "start_word_id": start,
                "end_word_id": end,
                "source_text": source,
            })
            if text:
                previous_texts.append(text)
    if progress:
        progress(len(batches), len(batches))
    if rejected_captions:
        log(f"字幕ライター: {rejected_captions}件の字幕は本文を元の言葉のままにしました")
    return _absorb_empty(captions), fallback_batches


def _absorb_empty(captions: list[dict]) -> list[dict]:
    """フィラーだけの範囲は字幕にせず、その単語を次の字幕(最後なら前の字幕)に持たせる。"""
    result: list[dict] = []
    pending: dict | None = None
    for caption in captions:
        if not caption["text"]:
            pending = caption if pending is None else {**pending, "end_word_id": caption["end_word_id"], "source_text": pending["source_text"] + caption["source_text"]}
            continue
        if pending is not None:
            caption = {
                **caption,
                "start_word_id": pending["start_word_id"],
                "source_text": pending["source_text"] + caption["source_text"],
                # 表示は発話の始まりから。フィラー部分は短いので先に出しても読める。
                "start": pending["start"],
            }
            pending = None
        result.append(caption)
    if pending is not None:
        if not result:
            raise CaptionWriterError("字幕にできる発話がありません。")
        last = result[-1]
        result[-1] = {**last, "end_word_id": pending["end_word_id"], "source_text": last["source_text"] + pending["source_text"], "end": pending["end"]}
    return result
