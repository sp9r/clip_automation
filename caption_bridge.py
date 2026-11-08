"""External caption bridge for standalone editors.

This is a deliberately narrow process boundary.  It runs the existing
Whisper and caption-boundary code, then emits only a validated transcript
payload.  It does not write a run, edit_project, planning artifact, or
production output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from caption import generate_subtitles
from caption_text import normalize_caption_display_text, resolve_display_mode
from transcribe import transcribe_video


BRIDGE_SCHEMA_VERSION = "spectra.caption_bridge.v1"


def _microseconds(seconds: object) -> int:
    value = float(seconds)
    if value < 0:
        raise ValueError("caption timing cannot be negative")
    return int(round(value * 1_000_000))


def _segment_words(caption: dict, words: list[dict]) -> list[dict]:
    start_id = caption.get("start_word_id")
    end_id = caption.get("end_word_id")
    if not isinstance(start_id, int) or not isinstance(end_id, int):
        return []
    result = []
    owned = words[start_id : end_id + 1]
    mode = resolve_display_mode(
        "".join(str(word.get("word", "")) for word in owned),
        caption.get("text", caption.get("display_text", "")),
    )
    for word in owned:
        text = normalize_caption_display_text(word.get("word", ""), mode)
        if not text:
            continue
        result.append(
            {
                "id": f"source-word:{int(word['word_id'])}",
                "text": text,
                "startUs": _microseconds(word.get("start", 0.0)),
                "endUs": _microseconds(word.get("end", word.get("start", 0.0))),
            }
        )
    return result


def build_transcript_output(transcript: list[dict], caption_result: dict, *, language: str = "ja") -> dict:
    """Convert existing algorithm output into the editor transcript boundary."""

    words = []
    for segment in transcript:
        for word in segment.get("words") or []:
            words.append(
                {
                    **word,
                    "word_id": len(words),
                    "word": str(word.get("word", "")),
                }
            )

    segments = []
    for index, caption in enumerate(caption_result.get("captions") or []):
        text = str(caption.get("text", ""))
        if not text:
            continue
        start_us = _microseconds(caption.get("start", 0.0))
        end_us = _microseconds(caption.get("end", 0.0))
        if end_us <= start_us:
            continue
        segment = {
            "startUs": start_us,
            "endUs": end_us,
            "text": text,
        }
        segment_words = _segment_words(caption, words)
        if segment_words and "".join(word["text"] for word in segment_words) == text:
            segment["words"] = segment_words
        segments.append(segment)

    if not segments:
        raise ValueError("caption algorithm produced no usable segments")
    return {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "kind": "transcript",
        "language": language,
        "text": "".join(segment["text"] for segment in segments),
        "segments": segments,
    }


def run_bridge(
    source: str,
    output: str,
    *,
    model_size: str = "large-v3",
    language: str = "ja",
    use_llm: bool = False,
    caption_model: str = "qwen2.5:14b",
    max_chars: int = 32,
    max_lines: int = 2,
) -> dict:
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"source media was not found: {source_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path = output_path.with_name("transcript.json")
    transcript = transcribe_video(
        str(source_path),
        model_size=model_size,
        output_json=str(transcript_path),
        language=language,
    )
    caption_result = generate_subtitles(
        str(transcript_path),
        str(output_path.with_suffix(".srt")),
        use_llm=use_llm,
        model=caption_model,
        max_chars=max_chars,
        max_lines=max_lines,
    )
    result = build_transcript_output(transcript, caption_result, language=language)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Whisper and the existing Spectra caption algorithm.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-size", default="large-v3")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--caption-model", default="qwen2.5:14b")
    parser.add_argument("--max-chars", type=int, default=32)
    parser.add_argument("--max-lines", type=int, default=2)
    parser.add_argument("--use-llm", action="store_true")
    args = parser.parse_args()
    run_bridge(
        args.source,
        args.output,
        model_size=args.model_size,
        language=args.language,
        use_llm=args.use_llm,
        caption_model=args.caption_model,
        max_chars=args.max_chars,
        max_lines=args.max_lines,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
