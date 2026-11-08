import json
import math
import os
import re
import urllib.error
import urllib.request
import caption_llm
from caption_writer import write_captions
from caption_boundary_v2 import optimize_boundaries
from caption_layout import fits as layout_fits, split_to_fit as layout_split_to_fit
from caption_settings import DEFAULT_MAX_LINES, MIN_MAX_CHARS, validate_max_chars, validate_max_lines
from caption_text import align_caption_word_ownership, classify_caption_text_status, normalize_caption_display_text, punctuation_mode, writer_text_status


TARGET_MIN_CHARS = 4
TARGET_MAX_CHARS = 16
HARD_MAX_CHARS = 22


HARD_MAX_DURATION_SECONDS = 5.0
PAUSE_THRESHOLD_SECONDS = 0.35
# 文字単位のWhisper語列をそのままモデルへ渡すため、長すぎる一括入力では
# JSON応答が崩れやすい。180語程度で分割し、各バッチの文脈を保つ。
MAX_WORDS_PER_LLM_BATCH = 180


BREAK_CHARS = ("。", "！", "？", "!", "?")
SOFT_BREAK_SUFFIXES = (
    "、",
    "から",
    "けど",
    "ので",
    "のに",
    "なら",
    "まで",
    "でも",
    "って",
    "という",
    "とか",
    "たり",
)
TOPIC_SHIFT_PREFIXES = (
    "さて",
    "ところで",
    "そして",
    "しかし",
    "だが",
    "つまり",
    "では",
    "それでは",
    "はい",
    "えー",
    "あの",
    "まず",
    "次に",
    "最後に",
    "要するに",
    "ちなみに",
    "一方",
    "結果として",
)
ALIGNMENT_IGNORED_CHARACTERS = set("、。！？!? \t\r\n")

UNNATURAL_START_PREFIXES = (
    "と思",
    "っていう",
    "というの",
    "なんで",
    "ので",
    "か取り",
)


def format_srt_time(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_int, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_int:02d},{milliseconds:03d}"


def _flatten_words(segments: list[dict]) -> list[dict]:
    words = []
    for segment in segments:
        segment_words = []
        for word in segment.get("words", []):
            text = str(word.get("word", ""))
            if not text:
                continue
            segment_words.append(
                {
                    "word": text,
                    "start": float(word["start"]),
                    "end": float(word["end"]),
                }
            )
        if not segment_words:
            continue

        # Whisperのword配列から句読点が落ちても、segment.textに残る
        # 文末記号を対応する最後の語へ戻して、強い境界シグナルにする。
        segment_text = str(segment.get("text", ""))
        normalized_text = "".join(
            character for character in segment_text if character not in BREAK_CHARS
        )
        word_text = "".join(item["word"] for item in segment_words)
        normalized_word_text = "".join(
            character for character in word_text if character not in BREAK_CHARS
        )
        sentence_end_indices = set()
        if normalized_text == normalized_word_text:
            character_count = 0
            for character in segment_text:
                if character in BREAK_CHARS:
                    if character_count:
                        consumed = 0
                        for index, item in enumerate(segment_words):
                            consumed += sum(
                                1
                                for character in item["word"]
                                if character not in BREAK_CHARS
                            )
                            if consumed >= character_count:
                                sentence_end_indices.add(index)
                                break
                else:
                    character_count += 1

        for index, item in enumerate(segment_words):
            item["word_id"] = len(words)
            item["segment_end"] = index == len(segment_words) - 1
            item["sentence_end"] = index in sentence_end_indices
            words.append(item)
    if not words:
        raise ValueError("字幕に使用できる発話語がありません。")
    return words


def _is_sentence_boundary(text: str) -> bool:
    return any(char in text for char in BREAK_CHARS)


def _is_soft_boundary(text: str) -> bool:
    return _is_sentence_boundary(text) or text.endswith(SOFT_BREAK_SUFFIXES)


def _dlen(text: str, hide_punctuation: bool = False) -> int:
    """字幕の表示文字数。句読点を非表示にする設定では、その句読点を数えない。"""
    value = str(text)
    return len(normalize_caption_display_text(value, "hide")) if hide_punctuation else len(value)


def _word_char_spans(words: list[dict]) -> set[tuple[int, int]]:
    """各語(単位)が原文全体の中で占める文字範囲。単位1つぶんの字幕かの判定に使う。"""
    spans = set()
    offset = 0
    for word in words:
        length = len(str(word["word"]))
        spans.add((offset, offset + length))
        offset += length
    return spans


def _validate_break_indices(
    breaks: list,
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    hide_punctuation: bool = False,
) -> list[int]:
    if not isinstance(breaks, list) or not breaks:
        raise ValueError("区切り位置が空です。")
    if any(isinstance(index, bool) or not isinstance(index, int) for index in breaks):
        raise ValueError("区切り位置は整数の配列である必要があります。")
    if breaks != sorted(set(breaks)):
        raise ValueError("区切り位置は重複のない昇順である必要があります。")
    if breaks[0] < 0 or breaks[-1] != len(words) - 1:
        raise ValueError("最後の語句まで字幕区間に含める必要があります。")

    total_chars = len("".join(word["word"] for word in words))
    maximum_blocks = max(1, math.ceil(total_chars / TARGET_MIN_CHARS) + 2)
    if len(breaks) > maximum_blocks:
        raise ValueError(
            f"字幕が{len(breaks)}個に細分化されています。"
            f"{maximum_blocks}個以下にまとめてください。"
        )

    previous = -1
    for end_index in breaks:
        if end_index >= len(words):
            raise ValueError("区切り位置が語句数の範囲外です。")
        block = words[previous + 1 : end_index + 1]
        text_length = _dlen("".join(word["word"] for word in block), hide_punctuation)
        duration = block[-1]["end"] - block[0]["start"]
        if len(block) > 1 and text_length > max_chars:
            raise ValueError(
                f"{previous + 1}〜{end_index}番の字幕が{max_chars}文字を超えています。"
            )
        if len(block) > 1 and duration > HARD_MAX_DURATION_SECONDS:
            raise ValueError(
                f"{previous + 1}〜{end_index}番の字幕が{HARD_MAX_DURATION_SECONDS}秒を超えています。"
            )
        previous = end_index
    return breaks


def _heuristic_break_indices(
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    hide_punctuation: bool = False,
) -> list[int]:
    """ポーズ間隔や文節・話題転換を最優先した字幕分割。

    単独で上限を超える単位は、単独の字幕として許容する(全体を失敗させない)。
    """
    return _validate_break_indices(
        optimize_boundaries(
            words,
            max_chars=max_chars,
            max_duration=HARD_MAX_DURATION_SECONDS,
            length_of=lambda word: _dlen(word["word"], hide_punctuation),
        ),
        words,
        max_chars=max_chars,
        hide_punctuation=hide_punctuation,
    )


def _failsafe_subtitles(
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    max_duration: float = HARD_MAX_DURATION_SECONDS,
    hide_punctuation: bool = False,
) -> list[dict]:
    """Build a lossless caption sequence without LLM or quality heuristics.

    This is deliberately boring: it consumes at least one raw word per
    iteration and starts a new block before the next word would violate the
    display constraints. A single raw word that is itself longer than a
    constraint is kept intact rather than being rewritten or discarded.
    """
    if not words:
        raise ValueError("字幕に使用できる発話語がありません。")
    if max_chars <= 0 or max_duration <= 0:
        raise ValueError("failsafe caption constraints must be positive")

    breaks: list[int] = []
    block_start = 0
    block_chars = _dlen(words[0]["word"], hide_punctuation)
    for index in range(1, len(words)):
        word = words[index]
        candidate_chars = block_chars + _dlen(word["word"], hide_punctuation)
        candidate_duration = float(word["end"]) - float(words[block_start]["start"])
        if candidate_chars > max_chars or candidate_duration > max_duration:
            breaks.append(index - 1)
            block_start = index
            block_chars = _dlen(word["word"], hide_punctuation)
        else:
            block_chars = candidate_chars
    breaks.append(len(words) - 1)
    subtitles = _build_subtitles(words, breaks)
    if not subtitles or any(not subtitle["text"] for subtitle in subtitles):
        raise ValueError("failsafe caption generation produced an empty caption")
    return subtitles


def deterministic_failsafe_captions(
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    max_duration: float = HARD_MAX_DURATION_SECONDS,
) -> list[dict]:
    """Return lossless captions with explicit source-word ownership.

    This is the same deterministic fallback used by ``generate_subtitles``;
    the worker can reuse it when the quality result cannot be assembled into
    the versioned result contract.  It never changes the raw word objects.
    """

    captions = _failsafe_subtitles(words, max_chars=max_chars, max_duration=max_duration)
    return _align_exact_caption_word_ownership(captions, words)


def _caption_fallback_diagnostic(code: str, message: str) -> dict:
    return {"level": "warning", "code": code, "message": message}


def _align_exact_caption_word_ownership(captions: list[dict], words: list[dict]) -> list[dict]:
    """Attach ownership to failsafe captions without display normalization."""
    if not captions or not words:
        raise ValueError("failsafe caption ownership input is empty")
    result: list[dict] = []
    cursor = 0
    for caption_index, caption in enumerate(captions, 1):
        text = str(caption.get("text", ""))
        if not text:
            raise ValueError(f"failsafe caption {caption_index} is empty")
        start_id = cursor
        assembled = ""
        while cursor < len(words) and assembled != text:
            token = str(words[cursor].get("word", ""))
            candidate = assembled + token
            if not text.startswith(candidate):
                raise ValueError(f"failsafe caption {caption_index} does not match raw words")
            assembled = candidate
            cursor += 1
        if assembled != text:
            raise ValueError(f"failsafe caption {caption_index} does not consume raw words")
        end_id = cursor - 1
        source_text = "".join(str(word.get("word", "")) for word in words[start_id : end_id + 1])
        result.append({
            **caption,
            "start_word_id": start_id,
            "end_word_id": end_id,
            "source_text": source_text,
            "text_status": classify_caption_text_status(source_text, text),
        })
    if cursor != len(words):
        raise ValueError("failsafe captions did not consume the final raw word")
    return result



def _make_llm_batches(words: list[dict]):
    start = 0
    while start < len(words):
        end = min(start + MAX_WORDS_PER_LLM_BATCH, len(words))
        if end < len(words):
            minimum_end = start + MAX_WORDS_PER_LLM_BATCH // 2
            for candidate in range(end - 1, minimum_end - 1, -1):
                if _is_sentence_boundary(words[candidate]["word"]):
                    end = candidate + 1
                    break
        yield start, end
        start = end


def _build_llm_prompt(
    words: list[dict],
    previous_error: str = "",
    *,
    max_chars: int = HARD_MAX_CHARS,
) -> str:
    source_text = "".join(word["word"] for word in words)
    target_length = max(TARGET_MIN_CHARS, min(16, max_chars - 4))
    target_blocks = max(1, math.ceil(len(source_text) / target_length))
    minimum_blocks = max(1, target_blocks - 2)
    maximum_blocks = target_blocks + 4
    retry_note = ""
    if previous_error:
        retry_note = f"\n前回の出力エラー: {previous_error}\nこの問題を修正してください。\n"

    indexed_words = [
        {"i": index, "text": word["word"]}
        for index, word in enumerate(words)
    ]

    return f"""
あなたは日本語YouTube切り抜き動画のプロ字幕編集者です。
以下のWhisper単語列を、視聴者がストレスなく読める「自然な話し言葉のテンポ・文節・息継ぎ」で字幕分割してください。

【ユーザー指定の理想の切り分け手本パターン】:
・「俺もうさ」「〜かと思ってさ」「〜と思ったらさ」「〜だけどね」「〜みたいなね」「おいガキ」「いや」「うん」「あでも」などの終助詞・話し言葉の相槌・語尾・ポーズで美しく区切る。
・「無理」「多分」「当然」「うん」「ガキで」などの2〜4文字の短い感情表現・感動詞も、単独の1字幕としてテンポよく分割する。
・「なんで○○くんのこと」「フォローしたんですか」「フォロバしたんですか」のように、質問・ツッコミの呼応単位で分ける。

【最重要・必須ルール】:
1. 出力は {{"break_after":[単語番号,...]}} のJSON形式のみ。番号は0始まりで、その番号の単語を字幕の末尾にする。
2. 最後の単語番号 {len(words) - 1} を必ず含めること。番号は昇順・重複なしにすること。
3. 各字幕は原則6〜16文字、最大{max_chars}文字、最大{HARD_MAX_DURATION_SECONDS}秒。短い感動詞（「おい」「いや」「うん」「無理」）は単独可。
4. 単語や形態素を途中で切ることは厳禁:
   - 「フォロバ」「フォロー」「モッスー」等の単語を途中で切らない（✕「フォロ」「バしたって」 → ○「フォロバしたって」）。
   - 数値＋述語（「100いない」等）を途中で切らない（✕「100い」「ないんだよ」 → ○「100いないんだよ」）。
   - 述語・補助動詞（「〜と思っている」「〜してねえんだよ」「〜やってくださいよ」）の途中で切らない（✕「〜いけ」「ねえんだよ」 → ○「〜いけねえんだよ」）。
5. 「俺もうさ」「〜かと思ってさ」「〜だけどね」「〜みたいなね」「おいガキ」「いや」「うん」など、意味が完結する話し言葉の単位は優先して区切る。
{retry_note}
Whisper単語列(JSON):
{json.dumps(indexed_words, ensure_ascii=False)}

参考原文（変更して出力してはいけない）:
{json.dumps(source_text, ensure_ascii=False)}
""".strip()





def _ollama_generate(prompt: str, model: str, timeout: int) -> str:
    if caption_llm.configured():
        return caption_llm.generate(prompt, model, timeout)
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    request = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        envelope = json.loads(response.read().decode("utf-8"))
    return str(envelope.get("response", ""))


def get_ollama_runtime_diagnostic(model: str, timeout: float = 2.0) -> dict:
    """Probe the configured backend; retain the legacy function for compatibility."""

    if caption_llm.configured():
        return caption_llm.runtime_diagnostic(model, timeout)

    model_name = str(model)
    if not model_name or "/" in model_name or "\\" in model_name:
        model_name = "configured-model"
    request = urllib.request.Request("http://localhost:11434/api/ps", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {"status": "unavailable", "model": model_name, "reason": "ollama_api_unavailable"}

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return {"status": "unavailable", "model": model_name, "reason": "invalid_ollama_runtime"}
    loaded = next(
        (
            item for item in models
            if isinstance(item, dict)
            and item.get("name") in {model, model_name}
            or isinstance(item, dict)
            and item.get("model") in {model, model_name}
        ),
        None,
    )
    if loaded is None:
        return {"status": "unavailable", "model": model_name, "reason": "model_not_loaded"}

    try:
        total_bytes = int(loaded["size"])
        vram_bytes = int(loaded["size_vram"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unavailable", "model": model_name, "reason": "runtime_memory_unavailable"}
    if total_bytes <= 0 or vram_bytes < 0:
        return {"status": "unavailable", "model": model_name, "reason": "runtime_memory_invalid"}
    if vram_bytes == 0:
        status = "cpu"
    elif vram_bytes >= total_bytes:
        status = "gpu"
    else:
        status = "mixed"
    return {
        "status": status,
        "model": model_name,
        "sizeBytes": total_bytes,
        "vramBytes": vram_bytes,
        "source": "ollama_api_ps",
    }


def _build_subtitles_from_caption_texts(
    words: list[dict], captions: list[str], hide_punctuation: bool = False, max_chars: int = 24
) -> list[dict]:
    timed_characters = []
    for word in words:
        text = word["word"]
        character_count = len(text)
        duration = max(0.0, word["end"] - word["start"])
        for index, character in enumerate(text):
            timed_characters.append(
                {
                    "character": character,
                    "start": word["start"] + duration * index / character_count,
                    "end": word["start"] + duration * (index + 1) / character_count,
                }
            )

    subtitles = []
    cursor = 0
    for caption in captions:
        caption_characters = timed_characters[cursor : cursor + len(caption)]
        if not caption_characters:
            raise ValueError("空の字幕が含まれています。")
        subtitles.append(
            {
                "start": caption_characters[0]["start"],
                "end": caption_characters[-1]["end"],
                "text": caption,
            }
        )
        cursor += len(caption)
    if cursor != len(timed_characters):
        raise ValueError("字幕が原文の全文字を消費していません。")
    return _clean_unnatural_subtitles(subtitles, max_chars=max_chars, hide_punctuation=hide_punctuation)


UNNATURAL_SPLIT_PREFIXES = (
    "と思", "っていう", "というの", "ので", "か取り", "バした", "ないんだよ", "ねえんだよ", "行って", "てるか", "バ"
)

FORBIDDEN_SPLIT_PAIRS = [
    ("フォロ", "バ"),
    ("フォ", "ロー"),
    ("ロ", "バ"),
    ("超", "えて"),
    ("無", "理"),
    ("じゃ", "ん"),
    ("みたい", "な"),
    ("さ", "せ"),
    ("いい加減し", "ろよ"),
    ("別に私", "が"),
    ("そんな", "細かい"),
    ("湧", "いて"),
    ("100い", "ない"),
    ("100", "い"),
    ("いけ", "ねえ"),
    ("いけ", "ない"),
    ("なきゃ", "いけ"),
    ("したっ", "て"),
    ("やって", "くだ"),
    ("スーパー", "チャット"),
]



def _force_merge_broken_phrases(subtitles: list[dict], max_chars: int = 24, hide_punctuation: bool = False) -> list[dict]:
    """絶対に切ってはいけない単語・述語ペアが不自然に分断されていた場合、強制結合する。"""
    if not subtitles or len(subtitles) <= 1:
        return subtitles

    merged = []
    skip_next = False
    for i in range(len(subtitles)):
        if skip_next:
            skip_next = False
            continue

        curr = dict(subtitles[i])
        if i < len(subtitles) - 1:
            nxt = subtitles[i + 1]
            curr_text = curr["text"].strip()
            nxt_text = nxt["text"].strip()

            # 不自然切断ペアの判定
            is_broken = any(
                curr_text.endswith(p1) and nxt_text.startswith(p2)
                for p1, p2 in FORBIDDEN_SPLIT_PAIRS
            )

            if is_broken and (_dlen(curr_text, hide_punctuation) + _dlen(nxt_text, hide_punctuation)) <= max_chars:
                curr["text"] = curr_text + nxt_text
                curr["end"] = max(curr["end"], nxt["end"])
                skip_next = True

        merged.append(curr)

    return merged


def _clean_unnatural_subtitles(subtitles: list[dict], max_chars: int = 24, hide_punctuation: bool = False) -> list[dict]:

    """不自然な単語分断や超短時間字幕を前後の文節へ自動結合するプロ仕様後処理。"""
    if not subtitles or len(subtitles) <= 1:
        return subtitles

    pass1 = _force_merge_broken_phrases(subtitles, max_chars=max_chars, hide_punctuation=hide_punctuation)
    cleaned = [dict(pass1[0])]
    for current in pass1[1:]:
        prev = cleaned[-1]
        duration = current["end"] - current["start"]
        text = current["text"].strip()
        combined_text = prev["text"] + text
        combined_len = _dlen(combined_text, hide_punctuation)

        is_too_short = (
            duration < 0.55
            and len(text) <= 4
            and text not in {"おい", "いや", "うん", "無理", "当然", "なんで", "ガキ"}
        )
        starts_unnatural = any(text.startswith(prefix) for prefix in UNNATURAL_SPLIT_PREFIXES)
        ends_unnatural = any(prev["text"].endswith(suffix) for suffix in ("100い", "フォロ", "いけ", "100", "あ", "なきゃ"))

        if (is_too_short or starts_unnatural or ends_unnatural) and combined_len <= max_chars:
            prev["end"] = max(prev["end"], current["end"])
            prev["text"] = combined_text
        else:
            cleaned.append(dict(current))

    return _force_merge_broken_phrases(cleaned, max_chars=max_chars, hide_punctuation=hide_punctuation)


def _split_obvious_topic_boundaries(captions: list[str]) -> list[str]:
    """意味の転換が明確な接続を、LLMの長い提案から再分割する。"""
    markers = (
        ("けどねなん", "けどね"),
        ("んですか別", "んですか"),
        ("フォロバしたら皆", "フォロバしたら"),
        ("しろよお前ら", "しろよ"),
        ("しろよ?お前ら", "しろよ?"),
        ("しろよ？お前ら", "しろよ？"),
        ("しろよそんな", "しろよ"),
        ("しろよ?そんな", "しろよ?"),
        ("しろよ？そんな", "しろよ？"),
    )
    result = []
    for caption in captions:
        remainder = caption
        while remainder:
            match = None
            for needle, left in markers:
                position = remainder.find(needle)
                if position >= 0 and (match is None or position < match[0]):
                    match = (position, left)
            if match is None:
                result.append(remainder)
                break
            position, left = match
            split_at = position + len(left)
            result.append(remainder[:split_at])
            remainder = remainder[split_at:]
    return result





def _align_caption_texts(captions: list[str], source_text: str) -> list[str]:
    """Align LLM boundaries while preserving the exact Whisper source text."""
    significant_source = []
    source_positions = []
    for index, character in enumerate(source_text):
        if character not in ALIGNMENT_IGNORED_CHARACTERS:
            significant_source.append(character)
            source_positions.append(index)

    significant_captions = [
        "".join(
            character
            for character in caption
            if character not in ALIGNMENT_IGNORED_CHARACTERS
        )
        for caption in captions
    ]
    if any(not caption for caption in significant_captions):
        raise ValueError("句読点・空白だけの字幕が含まれています。")
    if "".join(significant_captions) != "".join(significant_source):
        raise ValueError("LLM字幕が原文の文字を追加・削除・変更しています。")

    aligned = []
    normalized_cursor = 0
    source_cursor = 0
    for normalized_caption in significant_captions:
        normalized_cursor += len(normalized_caption)
        source_end = source_positions[normalized_cursor - 1] + 1
        while (
            source_end < len(source_text)
            and source_text[source_end] in ALIGNMENT_IGNORED_CHARACTERS
        ):
            source_end += 1
        aligned.append(source_text[source_cursor:source_end])
        source_cursor = source_end

    if source_cursor != len(source_text):
        raise ValueError("LLM字幕の区切りを原文末尾まで対応付けできません。")
    return aligned


def _split_at_sentence_boundaries(captions: list[str]) -> list[str]:
    split_captions = []
    for caption in captions:
        buffer = ""
        for character in caption:
            buffer += character
            if character in BREAK_CHARS:
                split_captions.append(buffer)
                buffer = ""
        if buffer:
            split_captions.append(buffer)
    return split_captions


def _consolidate_caption_candidates(
    candidates: list[str],
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    hide_punctuation: bool = False,
) -> list[str]:
    """Select a readable subset of LLM-proposed semantic boundaries."""
    candidates = _split_at_sentence_boundaries(candidates)
    candidate_subtitles = _build_subtitles_from_caption_texts(words, candidates, hide_punctuation, max_chars=max_chars)
    spans = _word_char_spans(words)
    offsets = [0]
    for candidate in candidates:
        offsets.append(offsets[-1] + len(candidate))
    count = len(candidates)
    costs = [math.inf] * (count + 1)
    previous = [None] * (count + 1)
    costs[0] = 0.0

    for start in range(count):
        if math.isinf(costs[start]):
            continue
        text = ""
        for end in range(start + 1, count + 1):
            text += candidates[end - 1]
            duration = (
                candidate_subtitles[end - 1]["end"]
                - candidate_subtitles[start]["start"]
            )
            if _dlen(text, hide_punctuation) > max_chars or duration > HARD_MAX_DURATION_SECONDS:
                # 単位(語)1つぶんだけで上限を超える候補は、単独字幕として許容する。
                if not (end == start + 1 and (offsets[start], offsets[end]) in spans):
                    break
            if any(character in text.strip()[:-1] for character in BREAK_CHARS):
                break

            stripped_text = text.strip()
            if (
                end < count
                and len(stripped_text) < TARGET_MIN_CHARS
                and not _is_sentence_boundary(stripped_text)
            ):
                continue
            if start > 0 and stripped_text.startswith(UNNATURAL_START_PREFIXES):
                continue

            target_length = max(TARGET_MIN_CHARS, min(20, max_chars - 2))
            length_cost = abs(len(text) - target_length)
            if len(text) < TARGET_MIN_CHARS:
                # 短い字幕へのペナルティを弱め、意味の区切りで切りやすくする
                length_cost += (TARGET_MIN_CHARS - len(text)) * 1.2
            duration_cost = abs(duration - 3.0) * 0.5
            boundary_bonus = 0.0
            if _is_sentence_boundary(stripped_text):
                boundary_bonus = -6.0
            elif _is_soft_boundary(stripped_text):
                boundary_bonus = -3.0
            cost = costs[start] + length_cost + duration_cost + boundary_bonus
            if cost < costs[end]:
                costs[end] = cost
                previous[end] = start

    if previous[count] is None:
        raise ValueError("LLMの意味境界から字幕制約を満たす組み合わせを作れません。")

    consolidated = []
    cursor = count
    while cursor > 0:
        start = previous[cursor]
        consolidated.append("".join(candidates[start:cursor]))
        cursor = start
    consolidated.reverse()
    return consolidated


def _validate_caption_texts(
    captions: list,
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    hide_punctuation: bool = False,
) -> list[dict]:
    if not isinstance(captions, list) or not captions:
        raise ValueError("captionsが空です。")
    if any(not isinstance(caption, str) or not caption for caption in captions):
        raise ValueError("captionsには空でない文字列だけを入れてください。")

    source_text = "".join(word["word"] for word in words)
    captions = _align_caption_texts(captions, source_text)
    captions = _consolidate_caption_candidates(captions, words, max_chars=max_chars, hide_punctuation=hide_punctuation)

    target_length = max(TARGET_MIN_CHARS, min(20, max_chars - 2))
    maximum_blocks = max(1, math.ceil(len(source_text) / target_length) + 4)
    if len(captions) > maximum_blocks:
        raise ValueError(
            f"字幕が{len(captions)}個に細分化されています。"
            f"{maximum_blocks}個以下にまとめてください。"
        )

    subtitles = _build_subtitles_from_caption_texts(words, captions, hide_punctuation, max_chars=max_chars)
    spans = _word_char_spans(words)
    caption_offset = 0
    for index, subtitle in enumerate(subtitles):
        text_length = _dlen(subtitle["text"], hide_punctuation)
        duration = subtitle["end"] - subtitle["start"]
        stripped_text = subtitle["text"].strip()
        # 単位(語)1つぶんだけの字幕は、上限超過でも単独字幕として許容する。
        single_unit = (caption_offset, caption_offset + len(subtitle["text"])) in spans
        caption_offset += len(subtitle["text"])
        if text_length > max_chars and not single_unit:
            raise ValueError(f"字幕{index + 1}が{max_chars}文字を超えています。")
        if duration > HARD_MAX_DURATION_SECONDS and not single_unit:
            raise ValueError(
                f"字幕{index + 1}が{HARD_MAX_DURATION_SECONDS}秒を超えています。"
            )
        if (
            index != len(subtitles) - 1
            and len(subtitle["text"]) < TARGET_MIN_CHARS
            and not _is_sentence_boundary(subtitle["text"])
        ):
            raise ValueError(
                f"字幕{index + 1}が{len(subtitle['text'])}文字しかなく、文末でもありません。"
            )
        if (
            index != len(subtitles) - 1
            and duration < 0.8
            and not _is_sentence_boundary(subtitle["text"])
        ):
            raise ValueError(f"字幕{index + 1}が{duration:.2f}秒しかありません。")
        if any(character in stripped_text[:-1] for character in BREAK_CHARS):
            raise ValueError(
                f"字幕{index + 1}が文末をまたいでいます。句点・感嘆符・疑問符で区切ってください。"
            )
        if index > 0 and stripped_text.startswith(UNNATURAL_START_PREFIXES):
            raise ValueError(
                f"字幕{index + 1}が接続表現「{stripped_text[:6]}」から始まっています。"
            )
    return subtitles


def _parse_llm_captions(
    response_text: str,
    words: list[dict],
    *,
    max_chars: int = HARD_MAX_CHARS,
    hide_punctuation: bool = False,
) -> list[dict]:
    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not match:
        raise ValueError("LLM応答にJSONがありません。")
    data = json.loads(match.group(0))
    if "break_after" in data:
        proposed = data.get("break_after")
        if (
            not isinstance(proposed, list)
            or not proposed
            or any(isinstance(index, bool) or not isinstance(index, int) for index in proposed)
            or proposed != sorted(set(proposed))
            or proposed[0] < 0
            or proposed[-1] != len(words) - 1
            or any(index >= len(words) for index in proposed)
        ):
            raise ValueError("break_afterが単語番号の昇順配列ではありません。")
        breaks = optimize_boundaries(
            words,
            max_chars=max_chars,
            max_duration=HARD_MAX_DURATION_SECONDS,
            preferred_breaks=proposed,
            length_of=lambda word: _dlen(word["word"], hide_punctuation),
        )
        breaks = _validate_break_indices(breaks, words, max_chars=max_chars, hide_punctuation=hide_punctuation)
        return _build_subtitles(words, breaks)
    # 旧形式も受け付ける。既存の外部fixture・過去のOllama応答を壊さない。
    return _validate_caption_texts(data.get("captions"), words, max_chars=max_chars, hide_punctuation=hide_punctuation)


def _request_llm_subtitles(
    words: list[dict],
    *,
    model: str,
    timeout: int,
    max_chars: int,
    hide_punctuation: bool = False,
) -> list[dict]:
    previous_error = ""
    for attempt in range(2):
        prompt = _build_llm_prompt(words, previous_error, max_chars=max_chars)
        try:
            response_text = _ollama_generate(prompt, model, timeout)
            return _parse_llm_captions(response_text, words, max_chars=max_chars, hide_punctuation=hide_punctuation)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            previous_error = str(exc)
            if attempt == 1:
                raise
    raise RuntimeError("LLM字幕分割に失敗しました。")


def _llm_subtitles(
    words: list[dict],
    *,
    model: str,
    timeout: int,
    max_chars: int,
    hide_punctuation: bool = False,
) -> tuple[list[dict], str]:
    all_subtitles = []
    used_fallback = False
    llm_failed = False

    for start, end in _make_llm_batches(words):
        batch = words[start:end]
        if llm_failed:
            local_breaks = _heuristic_break_indices(batch, max_chars=max_chars, hide_punctuation=hide_punctuation)
            batch_subtitles = _build_subtitles(batch, local_breaks)
            used_fallback = True
        else:
            try:
                batch_subtitles = _request_llm_subtitles(
                    batch,
                    model=model,
                    timeout=timeout,
                    max_chars=max_chars,
                    hide_punctuation=hide_punctuation,
                )
            except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                print(f"LLM字幕分割に失敗したためルール方式へ切り替えます: {exc}")
                local_breaks = _heuristic_break_indices(batch, max_chars=max_chars, hide_punctuation=hide_punctuation)
                batch_subtitles = _build_subtitles(batch, local_breaks)
                used_fallback = True
                llm_failed = True
        all_subtitles.extend(batch_subtitles)

    if "".join(subtitle["text"] for subtitle in all_subtitles) != "".join(
        word["word"] for word in words
    ):
        raise ValueError("字幕を連結した文字列が全体原文と一致しません。")
    return all_subtitles, "llm+fallback" if used_fallback else "llm"


def _build_subtitles(words: list[dict], breaks: list[int]) -> list[dict]:
    subtitles = []
    start_index = 0
    for end_index in breaks:
        block = words[start_index : end_index + 1]
        subtitles.append(
            {
                "start": block[0]["start"],
                "end": block[-1]["end"],
                "text": "".join(word["word"] for word in block),
            }
        )
        start_index = end_index + 1
    return subtitles


def _enforce_line_layout(
    subtitles: list[dict],
    words: list[dict],
    line_capacity: int,
    max_lines: int,
    hide_punctuation: bool = False,
) -> tuple[list[dict], int]:
    """``line_capacity`` x ``max_lines`` に折り返せない字幕をword境界で分割する。"""
    result: list[dict] = []
    split_count = 0
    for subtitle in subtitles:
        start_id = subtitle.get("start_word_id")
        end_id = subtitle.get("end_word_id")
        if not isinstance(start_id, int) or not isinstance(end_id, int):
            result.append(subtitle)
            continue
        block = words[start_id : end_id + 1]
        # 非表示にする句読点は幅に数えない(表示される文字だけで折り返しを判定する)。
        units = [
            normalize_caption_display_text(str(word["word"]), "hide") if hide_punctuation else str(word["word"])
            for word in block
        ]
        if layout_fits(units, line_capacity, max_lines):
            result.append(subtitle)
            continue
        soft_flags = [_is_soft_boundary(str(word["word"])) for word in block]
        ends = layout_split_to_fit(units, line_capacity, max_lines, soft_flags=soft_flags)
        if len(ends) <= 1:
            result.append(subtitle)  # 単一wordが長すぎる場合は分割しない
            continue
        cursor = 0
        for end in ends:
            piece = block[cursor : end + 1]
            result.append({
                "start": piece[0]["start"],
                "end": piece[-1]["end"],
                "text": "".join(str(word["word"]) for word in piece),
            })
            cursor = end + 1
        split_count += len(ends) - 1
    return result, split_count


def generate_subtitles(
    clean_transcript_json: str,
    output_srt: str,
    *,
    use_llm: bool = True,
    model: str = "qwen2.5:14b",
    timeout: int = 120,
    max_chars: int = HARD_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
    processing_device: str = "auto",
    collect_runtime: bool = False,
    line_capacity: int | None = None,
    hide_punctuation: bool = True,
    progress_callback=None,
    topic: str = "",
) -> dict:
    """Generate SRT captions with LLM-selected semantic boundaries.

    ``line_capacity`` (全角換算の1行容量) を渡すと、各字幕が ``max_lines`` 行以内に
    単位(文節/単語)境界で折り返せることを最後に保証する。

    ``hide_punctuation`` が真(既定)なら、表示から 、。(と文中の半角 , .)を除く。
    ?!…は残す。偽なら句読点をそのまま表示する。句読点は区切りの手がかりとして
    どちらの場合も使い、source wordの文字列・時刻は変えない。
    """
    mode = punctuation_mode(hide_punctuation)
    llm_requested = use_llm
    max_lines = validate_max_lines(max_lines)
    if line_capacity is not None:
        if isinstance(line_capacity, bool) or not isinstance(line_capacity, int) or line_capacity < 1:
            raise ValueError("字幕の1行容量は1以上の整数で指定してください。")
        max_chars = min(int(max_chars), line_capacity * max_lines)
        max_chars = max(MIN_MAX_CHARS, max_chars)
    max_chars = validate_max_chars(max_chars)
    with open(clean_transcript_json, "r", encoding="utf-8") as file:
        segments = json.load(file)

    words = _flatten_words(segments)
    diagnostics: list[dict] = []

    def add_diagnostic(code: str, message: str) -> None:
        if not any(item["code"] == code for item in diagnostics):
            diagnostics.append(_caption_fallback_diagnostic(code, message))

    def heuristic_subtitles() -> list[dict]:
        breaks = _heuristic_break_indices(words, max_chars=int(max_chars), hide_punctuation=hide_punctuation)
        return _build_subtitles(words, breaks)

    def failsafe_subtitles() -> list[dict]:
        add_diagnostic(
            "CAPTION_FAILSAFE_FALLBACK",
            "Caption quality processing failed; deterministic lossless segmentation was used.",
        )
        return _failsafe_subtitles(words, max_chars=int(max_chars), hide_punctuation=hide_punctuation)

    def finish(subtitles: list[dict], method: str, layout_split_count: int = 0) -> dict:
        caption_ai_runtime = None
        if llm_requested and (collect_runtime or processing_device == "gpu" or caption_llm.configured()):
            caption_ai_runtime = get_ollama_runtime_diagnostic(model)
            if processing_device == "gpu" and caption_ai_runtime.get("status") != "gpu":
                raise RuntimeError(
                    "GPU優先設定ですが、字幕AIがGPUへ配置されていません。"
                )

        os.makedirs(os.path.dirname(output_srt) or ".", exist_ok=True)
        with open(output_srt, "w", encoding="utf-8") as file:
            for index, subtitle in enumerate(subtitles, 1):
                file.write(f"{index}\n")
                file.write(
                    f"{format_srt_time(subtitle['start'])} --> "
                    f"{format_srt_time(subtitle['end'])}\n"
                )
                file.write(f"{subtitle['text']}\n\n")

        print(
            f"スマート字幕(SRT)生成完了: {len(subtitles)}ブロック "
            f"(区切り方式: {method})"
        )
        print(f" - 保存先: {output_srt}")
        result = {
            "blocks": len(subtitles),
            "method": method,
            "captions": subtitles,
            "caption_settings": {
                "max_chars": max_chars,
                "max_lines": max_lines,
                "hide_punctuation": bool(hide_punctuation),
                "max_lines_application": "renderer_owned",
                **({"line_capacity": line_capacity, "layout_split_count": layout_split_count} if line_capacity is not None else {}),
            },
            "diagnostics": diagnostics,
        }
        if caption_ai_runtime is not None:
            result["caption_ai_runtime"] = caption_ai_runtime
        return result

    if use_llm and caption_llm.configured():
        # LLMが区切りと本文(フィラー・句読点を消した文)を書く。合格した字幕は後段で書き換えない。
        def rule_ranges(batch: list[dict]) -> list[tuple[int, int]]:
            breaks = _heuristic_break_indices(batch, max_chars=int(max_chars), hide_punctuation=hide_punctuation)
            ranges, start = [], 0
            for end in breaks:
                ranges.append((start, end))
                start = end + 1
            return ranges

        # 思考ありは区切りとフィラーの判断がはっきり良い（2026-10-22の比較）。時間は長くかかる。
        thinking = caption_llm.writer_thinking()
        try:
            written, fallback_batches = write_captions(
                words,
                generate=lambda prompt, request_timeout: caption_llm.generate(prompt, model, max(timeout, request_timeout), thinking=thinking),
                batch_size=40 if thinking else 70,
                seconds_per_unit=40.0 if thinking else 6.0,
                progress=progress_callback,
                style=caption_llm.writer_style(),
                topic=topic,
                max_chars=int(max_chars),
                max_lines=max_lines,
                line_capacity=line_capacity,
                hide_punctuation=hide_punctuation,
                fallback=rule_ranges,
            )
            for caption in written:
                caption["text_status"] = writer_text_status(caption["source_text"], caption["text"])
            if fallback_batches:
                add_diagnostic(
                    "CAPTION_LLM_PARTIAL_FALLBACK",
                    f"The caption writer failed for {fallback_batches} batch(es); rule segmentation was used there.",
                )
            return finish(written, "llm" if not fallback_batches else "llm+fallback")
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
            print(f"字幕ライターが使えないため従来の方式へ切り替えます: {exc}")
            add_diagnostic(
                "CAPTION_LLM_FALLBACK",
                "The LLM caption writer was unavailable; heuristic segmentation was attempted.",
            )
            use_llm = False

    if use_llm:
        try:
            subtitles, method = _llm_subtitles(
                words,
                model=model,
                timeout=timeout,
                max_chars=int(max_chars),
                hide_punctuation=hide_punctuation,
            )
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as exc:
            # A malformed or internally inconsistent LLM boundary must not
            # block a real auto-caption run. The heuristic path is tried
            # before the deterministic failsafe.
            print(f"LLM字幕分割の整合性検証に失敗したためルール方式へ切り替えます: {exc}")
            add_diagnostic(
                "CAPTION_LLM_FALLBACK",
                "LLM semantic segmentation failed; heuristic segmentation was attempted.",
            )
            try:
                subtitles = heuristic_subtitles()
                add_diagnostic(
                    "CAPTION_HEURISTIC_FALLBACK",
                    "Heuristic segmentation was used after the LLM segmentation failure.",
                )
                method = "llm+fallback"
            except Exception as heuristic_error:
                print(f"ルール字幕分割にも失敗したため安全方式へ切り替えます: {heuristic_error}")
                add_diagnostic(
                    "CAPTION_HEURISTIC_FALLBACK",
                    "Heuristic segmentation failed; deterministic segmentation was attempted.",
                )
                subtitles = failsafe_subtitles()
                method = "llm+failsafe"
    else:
        try:
            subtitles = heuristic_subtitles()
            # 字幕ライターが使えずにルール方式へ切り替えた場合は、それと分かるようにする。
            method = "llm+fallback" if llm_requested else "heuristic"
        except Exception as heuristic_error:
            print(f"ルール字幕分割に失敗したため安全方式へ切り替えます: {heuristic_error}")
            add_diagnostic(
                "CAPTION_HEURISTIC_FALLBACK",
                "Heuristic segmentation failed; deterministic segmentation was used.",
            )
            subtitles = failsafe_subtitles()
            method = "failsafe"

    if method == "llm+fallback":
        add_diagnostic(
            "CAPTION_LLM_FALLBACK",
            "LLM semantic segmentation failed; heuristic segmentation was used.",
        )
        add_diagnostic(
            "CAPTION_HEURISTIC_FALLBACK",
            "Heuristic segmentation was used after the LLM segmentation failure.",
        )

    # LLM経路・ヒューリスティック経路の両方に同じ後処理を適用する。
    # これにより、モデルが提案した境界とルール境界で品質差が出ない。
    # ただし、LLMの不正な境界が後処理段階で判明する場合もあるため、
    # その場合は文字を捨てずに完全なルール分割へ戻す。
    try:
        subtitles = _clean_unnatural_subtitles(subtitles, max_chars=max_chars, hide_punctuation=hide_punctuation)
        captions = _split_obvious_topic_boundaries([subtitle["text"] for subtitle in subtitles])
        subtitles = _build_subtitles_from_caption_texts(words, captions, hide_punctuation, max_chars=int(max_chars))
    except Exception as exc:
        print(f"字幕品質後処理に失敗したため安全方式へ切り替えます: {exc}")
        add_diagnostic(
            "CAPTION_HEURISTIC_FALLBACK",
            "Caption quality post-processing failed; deterministic segmentation was used.",
        )
        subtitles = failsafe_subtitles()
        method = "llm+failsafe" if use_llm else "failsafe"

    def normalize_and_align(candidate_subtitles: list[dict], *, normalize_display: bool = True) -> list[dict]:
        # 表示文字列を後からword列へ推測で戻さず、最終captionへsource ownershipを
        # 明示的に固定する。ここで失敗した場合はword途中境界を黙って生成しない。
        if not normalize_display:
            return _align_exact_caption_word_ownership(candidate_subtitles, words)
        for subtitle in candidate_subtitles:
            subtitle["text"] = normalize_caption_display_text(subtitle["text"], mode)
        return align_caption_word_ownership(candidate_subtitles, words, mode)

    try:
        subtitles = normalize_and_align(subtitles, normalize_display=method not in {"failsafe", "llm+failsafe"})
    except Exception as exc:
        print(f"字幕のword ownership検証に失敗したため安全方式へ切り替えます: {exc}")
        add_diagnostic(
            "CAPTION_HEURISTIC_FALLBACK",
            "Caption ownership validation failed; deterministic segmentation was used.",
        )
        subtitles = failsafe_subtitles()
        subtitles = normalize_and_align(subtitles, normalize_display=False)
        method = "llm+failsafe" if use_llm else "failsafe"

    layout_split_count = 0
    if line_capacity is not None:
        try:
            laid_out, layout_split_count = _enforce_line_layout(subtitles, words, line_capacity, max_lines, hide_punctuation)
            if layout_split_count:
                subtitles = normalize_and_align(laid_out, normalize_display=method not in {"failsafe", "llm+failsafe"})
        except Exception as exc:
            print(f"字幕の行数制約の適用に失敗したため元の区切りを使います: {exc}")
            layout_split_count = 0
            add_diagnostic(
                "CAPTION_LAYOUT_SPLIT_FAILED",
                "Line-layout splitting failed; the original caption boundaries were kept.",
            )

    return finish(subtitles, method, layout_split_count)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="スマート字幕SRTを生成")
    parser.add_argument("clean_transcript_json")
    parser.add_argument("output_srt")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--model", default="qwen2.5:14b")
    parser.add_argument("--max-chars", type=int, default=HARD_MAX_CHARS)
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    args = parser.parse_args()
    generate_subtitles(
        args.clean_transcript_json,
        args.output_srt,
        use_llm=not args.no_llm,
        model=args.model,
        max_chars=args.max_chars,
        max_lines=args.max_lines,
    )
