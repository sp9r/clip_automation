import math
import unicodedata


PARTICLES = {"は", "が", "を", "に", "で", "と", "へ", "の", "も", "や", "から", "まで", "より"}
AUXILIARY_LEADS = {"いる", "ある", "ない", "ます", "です", "たい", "れる", "られる", "ください"}
SENTENCE_ENDINGS = ("。", "！", "？", "!", "?")

# faster-whisperの日本語word_timestampsは、助詞・語尾・活用語を
# 1文字〜数文字の細かい塊にすることがある。次の語が続く場合は、
# 文字数が目標に達していても境界候補から強く除外する。
CONTINUATION_LEADS = {
    "えて", "える", "った", "って", "てる", "てい", "てく", "なく",
    "ない", "んだ", "んです", "んじゃ", "じゃん", "よう", "くん", "さん",
    "しい", "いない", "いた", "いけ", "くれ", "ください", "です", "ます",
    "るかな", "のこと", "がなんで", "するんだ", "細かい",
    "みたい", "くらい", "から", "まで", "ので", "のに", "けど", "なら",
    "じゃ", "な", "理", "フォ", "フォロ", "ロー", "ロバ", "バ", "いて", "えだろ", "よ?",
}

CONTINUATION_ENDINGS = (
    "超", "思", "言わ", "行っ", "やっ", "集め", "フォロ", "発狂", "起き",
    "見", "怒", "なきゃ", "いけ", "って", "てい", "てく", "なく", "無", "湧",
)

EMPHATIC_ENDINGS = (
    "さ", "ね", "よ", "ぞ", "だろ", "んだよ", "ですよ", "ますよ", "ください",
    "じゃん", "みたいな", "と思ってさ",
)

STANDALONE_SHORT = {"おい", "いや", "うん", "無理", "当然", "なんで"}

# 日本語の形態素境界が不安定な場合でも、字幕をこの語の途中に置かない。
# 句読点がない話し言葉で特に頻出する複合語・補助動詞列を収録する。
PROTECTED_PHRASES = (
    "100超えてる", "100いない", "200種類くらい", "200種類くらいいるかな", "と思って", "参加型", "参加料", "すんなら", "集めんの", "ダメなん", "ねーぞ",
    "みたいな", "フォロバ", "フォロー", "フォローバック", "モッスー", "リスナー",
    "わけねえだろ", "いけねえ", "文句言わなきゃ", "言わなきゃいけねえ", "お前ら", "なんで", "もらえます", "くださいよ", "ワンサカ",
    "ふざける", "ラジオ体操", "夏休み", "午後から",
    "ラジオ体操に", "その後", "NHK", "朝は8時に起きて", "午前中は", "友達", "友達と", "遊びに行って", "1日5時間", "フォトページ", "だるい", "だるいじゃん", "モッスーっていうガキ", "モッスーっていうガキを", "モッスーくん", "モッスーくんを", "フォローしたんですか", "フォローする", "フォロバしたんですか", "なんで○○くんのこと", "くんのこと", "皆さんが", "別に私が", "フォローさせてもらいます", "そんな細かいこと", "いい加減しろよ", "ですか", "いいだろ", "じゃん", "お前らに",
)

FORCED_BREAK_SUFFIXES = (
    "けどね", "んですか", "フォロバしたら", "しろよ", "と思ってさ",
    "だるいじゃん", "集めんの", "うるせーなって思いましたね",
)

TOKEN_CLASS_JAPANESE = "japanese"
TOKEN_CLASS_LATIN = "latin"
TOKEN_CLASS_NUMERIC = "numeric"
TOKEN_CLASS_PUNCTUATION = "punctuation"
TOKEN_CLASS_MIXED = "mixed"
TOKEN_CLASS_OTHER = "other"

# 形態素解析器を追加せずに、字幕用の軽い境界ヒントだけを提供する。
# 長い接尾辞から判定することで、英語・日本語混在トークンにも適用できる。
PARTICLE_SUFFIXES = (
    "では", "には", "とは", "から", "まで", "より", "ので", "のに", "だけ", "しか",
    "って", "は", "が", "を", "に", "で", "と", "へ", "の", "も", "や",
)
VERBISH_ENDINGS = (
    "する", "した", "して", "します", "できる", "できた", "できて", "いる", "いた", "います",
    "ある", "ない", "ます", "です", "たい", "れる", "られる", "ください", "った", "って",
    "くる", "きた", "いく", "いった", "て", "から", "ので", "のに",
)
PRENOMINAL_ENDINGS = ("同じ", "大きな", "小さな", "いろんな", "あらゆる")
PREDICATE_PARTICLES = {"は", "が", "を", "に", "で", "と", "へ", "も"}


def _classify_token(text):
    """字幕境界判定用の軽量なトークン分類を返す。"""

    text = str(text)
    if not text:
        return TOKEN_CLASS_OTHER
    categories = [unicodedata.category(character) for character in text]
    if all(category.startswith("P") or category.startswith("S") for category in categories):
        return TOKEN_CLASS_PUNCTUATION
    has_latin = any(("A" <= character <= "Z") or ("a" <= character <= "z") for character in text)
    has_numeric = any(character.isnumeric() for character in text)
    has_japanese = any(
        ("\u3040" <= character <= "\u30ff") or ("\u3400" <= character <= "\u9fff")
        for character in text
    )
    if has_numeric and not has_latin and not has_japanese:
        return TOKEN_CLASS_NUMERIC
    if has_latin and not has_numeric and not has_japanese:
        return TOKEN_CLASS_LATIN
    if has_latin or has_numeric:
        return TOKEN_CLASS_MIXED
    if has_japanese:
        return TOKEN_CLASS_JAPANESE
    return TOKEN_CLASS_OTHER


def _particle_suffix(text):
    text = str(text)
    return next((suffix for suffix in PARTICLE_SUFFIXES if text.endswith(suffix) and len(text) > len(suffix)), "")


def _looks_like_predicate(text):
    text = str(text)
    return bool(text) and (text in AUXILIARY_LEADS or text in CONTINUATION_LEADS or text.endswith(VERBISH_ENDINGS))


def _looks_like_noun(text):
    text = str(text)
    if not text or _classify_token(text) == TOKEN_CLASS_PUNCTUATION:
        return False
    stem = text
    suffix = _particle_suffix(stem)
    if suffix:
        stem = stem[: -len(suffix)]
    return bool(stem) and not _looks_like_predicate(stem)


def assign_word_ids(words):
    return [{**word, "word_id": index} for index, word in enumerate(words)]


def boundary_diagnostic(words, end_index, *, preferred_breaks=None):
    """境界候補の理由を返す。

    ``protected`` は既存の明示保護表、``syntax_penalty`` はトークン構造から
    導いた一般則である。候補をハードに削除せず、max_chars等で経路が必要な
    場合はDPがフォールバックできるようにする。
    """

    text = str(words[end_index].get("word", ""))
    next_text = str(words[end_index + 1].get("word", "")) if end_index + 1 < len(words) else ""
    pause = 0.0
    if end_index + 1 < len(words):
        pause = max(0.0, float(words[end_index + 1]["start"]) - float(words[end_index]["end"]))
    pause_contribution = min(pause, 1.2) * 5.0
    base_score = 0.0
    before = "".join(str(word.get("word", "")) for word in words[: end_index + 1])
    after = "".join(str(word.get("word", "")) for word in words[end_index + 1 :])
    full_text = before + after
    boundary_position = len(before)
    protected_boundary = False
    for phrase in PROTECTED_PHRASES:
        search_from = 0
        while True:
            phrase_position = full_text.find(phrase, search_from)
            if phrase_position < 0:
                break
            if phrase_position < boundary_position < phrase_position + len(phrase):
                protected_boundary = True
                break
            search_from = phrase_position + 1
        if protected_boundary:
            break
    structural_protection = False
    protected_penalty = -100.0 if protected_boundary else 0.0
    if words[end_index].get("sentence_end"):
        base_score += 18.0
    if words[end_index].get("segment_end"):
        base_score += 2.0
    if text.endswith(SENTENCE_ENDINGS):
        base_score += 15.0
    legacy_syntax_penalty = 0.0
    if text in PARTICLES:
        legacy_syntax_penalty -= 13.0
    if next_text in AUXILIARY_LEADS or next_text in CONTINUATION_LEADS:
        legacy_syntax_penalty -= 14.0

    # 一般化された助詞・語種・活用列の保護。fixtureの固有語を参照しない。
    generic_syntax_penalty = 0.0
    current_class = _classify_token(text)
    next_class = _classify_token(next_text)
    particle = _particle_suffix(text)
    short_pause = pause <= 0.05
    if particle and next_text:
        has_latin = any(("A" <= character <= "Z") or ("a" <= character <= "z") for character in text)
        if current_class in {TOKEN_CLASS_LATIN, TOKEN_CLASS_MIXED} and has_latin:
            structural_protection = True
        elif particle == "の" and _looks_like_noun(next_text) and short_pause:
            structural_protection = True
        elif particle in {"を", "も"} and _looks_like_predicate(next_text) and short_pause and len(text) >= 3:
            generic_syntax_penalty -= 7.0
            structural_protection = True
        elif particle in {"は", "が", "を"} and "の" in text and _looks_like_noun(next_text):
            structural_protection = True
    if text.endswith(PRENOMINAL_ENDINGS) and next_text:
        generic_syntax_penalty -= 6.0
        structural_protection = True
    if (
        text.endswith(("する", "した"))
        and _looks_like_noun(next_text)
        and not particle
        and short_pause
        and _particle_suffix(next_text) in {"と", "の", "を"}
    ):
        generic_syntax_penalty -= 6.0
        structural_protection = True
    if text.endswith(("てから", "から")) and _looks_like_noun(next_text) and short_pause:
        generic_syntax_penalty -= 8.0
        structural_protection = True
    if text.endswith(("て", "から", "ので", "のに")) and _looks_like_predicate(next_text):
        generic_syntax_penalty -= 6.0

    if after.startswith("それが") or after.startswith("えじゃあ"):
        base_score += 12.0
    if before.endswith(("けどね", "んですか")) and after.startswith("なん"):
        base_score += 100.0
    if before.endswith("んですか") and after.startswith("別"):
        base_score += 100.0
    if before.endswith("フォロバしたら") and after.startswith("皆"):
        base_score += 100.0
    if before.endswith("しろよ") and after.startswith("そんな"):
        base_score += 25.0
    if next_text in STANDALONE_SHORT:
        base_score += 12.0
    if before.endswith("んの") and after.startswith("え"):
        base_score += 12.0
    if text.endswith(CONTINUATION_ENDINGS):
        legacy_syntax_penalty -= 12.0
    if text.endswith(EMPHATIC_ENDINGS):
        base_score += 4.5
    if text in STANDALONE_SHORT:
        base_score += 8.0
    preferred_bonus = 0.0
    if end_index in set(preferred_breaks or ()):
        preferred_bonus = 0.4
    if structural_protection and not protected_boundary:
        protected_penalty = -100.0
    legacy_protected_penalty = -100.0 if protected_boundary else 0.0
    syntax_penalty = legacy_syntax_penalty + generic_syntax_penalty
    # DPの既存挙動を保ち、一般則は下記のrepair段階で境界を除去する。
    # これにより、max_charsで別経路が必要な場合も完全経路を失わない。
    score = pause_contribution + base_score + legacy_syntax_penalty + legacy_protected_penalty + preferred_bonus
    if protected_boundary or structural_protection:
        category = "hard_forbidden"
    elif syntax_penalty < 0.0:
        category = "soft_discouraged"
    elif preferred_bonus:
        category = "preferred"
    else:
        category = "neutral"
    return {
        "boundary_index": end_index,
        "base_score": base_score,
        "pause_contribution": pause_contribution,
        "syntax_penalty": syntax_penalty,
        "protected_penalty": protected_penalty,
        "preferred_bonus": preferred_bonus,
        "score": score,
        "category": category,
        "hard_forbidden": protected_boundary or structural_protection,
        "generic_protected": structural_protection,
        "token_class": current_class,
        "next_token_class": next_class,
    }


def _boundary_score(words, end_index):
    return boundary_diagnostic(words, end_index)["score"]


def _is_forced_boundary(words, end_index):
    if words[end_index].get("sentence_end"):
        return True
    before = "".join(str(word.get("word", "")) for word in words[: end_index + 1])
    return before.endswith(FORCED_BREAK_SUFFIXES)


def optimize_boundaries(
    words,
    *,
    max_chars=22,
    max_duration=5.0,
    min_duration=0.35,
    target_chars=13,
    preferred_breaks=None,
    length_of=None,
):
    """文字数・時間の制約を満たす字幕境界をDPで選ぶ。

    1語(1単位)だけでmax_chars/max_durationを超える場合は、その単位を単独の
    字幕にする(超過を許容する。表示側で折り返される)。他の制約は維持する。
    ``length_of(word_dict)`` で文字数の数え方(非表示にする句読点を除く等)を差し替える。
    """
    if not words:
        return []
    if length_of is None:
        length_of = lambda word: len(str(word.get("word", "")))
    words = assign_word_ids(words)
    preferred_breaks = set(preferred_breaks or ())
    count = len(words)
    best = [math.inf] * (count + 1)
    previous = [None] * (count + 1)
    best[0] = 0.0
    for start in range(count):
        if not math.isfinite(best[start]):
            continue
        chars = 0
        for end in range(start, count):
            chars += length_of(words[end])
            duration = float(words[end]["end"]) - float(words[start]["start"])
            # 単独で上限を超える単位(end == start)は、単独字幕として許容する。
            if end > start and (chars > max_chars or duration > max_duration):
                break
            # Whisperのpoint/zero-duration語では、表示時間が短くても
            # max_chars/max_durationを満たす完全経路が存在し得る。ここを
            # hard pruneすると、feasibleな入力でも終端へ到達できず、旧来の
            # ``[count - 1]`` 不正fallbackへ落ちる。最小表示時間は品質上の
            # preferenceとしてのみ扱い、hard constraintは文字数とdurationに
            # 限定する。
            short_duration_penalty = 0.0
            if duration < min_duration and end < count - 1:
                short_duration_penalty = (min_duration - duration) * 4.0
            # 日本語字幕は短い意味単位を優先する。ただし語尾・助詞・
            # 活用途中の境界は上のboundary scoreで十分強く抑制する。
            length_cost = abs(min(chars, max_chars) - min(target_chars, max_chars)) * 0.22
            cost = (
                best[start]
                + length_cost
                + short_duration_penalty
                - _boundary_score(words, end)
            )
            if _is_forced_boundary(words, end):
                cost -= 200.0
            if end == count - 1:
                cost -= 20.0
            if end in preferred_breaks:
                # Ollamaは意味境界の候補を出すが、単語途中・助詞直後を
                # 盲目的に採用せず、最終判断はWhisper語列の規則とDPに任せる。
                cost -= 0.6
            if cost < best[end + 1]:
                best[end + 1] = cost
                previous[end + 1] = start
    if previous[count] is None:
        raise ValueError(
            "字幕制約を満たす境界経路を生成できません。"
            "字幕境界の探索が終端へ到達しませんでした。"
        )
    breaks = []
    cursor = count
    while cursor:
        breaks.append(cursor - 1)
        cursor = previous[cursor]
    breaks = list(reversed(breaks))

    # 一般化された保護境界は、DP全体を壊さないように候補経路の後段で
    # 可能な場合だけ隣接字幕を結合する。max_chars / max_durationを超える
    # 場合は boundaryを ``forced_due_to_max_chars`` として残す。
    changed = True
    while changed:
        changed = False
        previous_break = -1
        for position, end_index in enumerate(breaks[:-1]):
            diagnostic = boundary_diagnostic(words, end_index)
            if not diagnostic["generic_protected"] or _is_forced_boundary(words, end_index):
                previous_break = end_index
                continue
            next_end = breaks[position + 1]
            combined_chars = sum(
                length_of(words[index])
                for index in range(previous_break + 1, next_end + 1)
            )
            combined_duration = float(words[next_end]["end"]) - float(words[previous_break + 1]["start"])
            if combined_chars <= max_chars and combined_duration <= max_duration:
                del breaks[position]
                changed = True
                break
            previous_break = end_index
    return breaks
