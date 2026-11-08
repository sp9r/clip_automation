"""Segmentation-only caption worker.

Re-splits already transcribed words into captions with new layout limits
(max chars / max lines / per-line capacity / mode). It reads no audio and never
loads Whisper, and it uses the same ``generate_subtitles`` implementation as the
full auto-caption worker, so there is a single segmentation implementation.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

import caption_llm
from auto_caption_contract import RESEGMENT_RESULT_VERSION, AutoCaptionContractError, parse_resegment_request
from caption import generate_subtitles

DEFAULT_CAPTION_MODEL = caption_llm.default_model("qwen2.5:14b")
DEFAULT_CAPTION_TIMEOUT_SECONDS = 300 if caption_llm.configured() else 120
# Words further apart than this start a new transcript segment (keeps the
# "segment end" boundary signal the full worker gets from Whisper segments).
SEGMENT_GAP_SECONDS = 1.0


def _transcript(words: list[dict]) -> list[dict]:
    segments: list[list[dict]] = []
    for word in words:
        start = word["startUs"] / 1_000_000
        end = word["endUs"] / 1_000_000
        if not segments or start - segments[-1][-1]["end"] >= SEGMENT_GAP_SECONDS:
            segments.append([])
        segments[-1].append({"word": word["text"], "start": start, "end": end})
    return [{
        "text": "".join(item["word"] for item in group),
        "start": group[0]["start"],
        "end": group[-1]["end"],
        "words": group,
    } for group in segments]


def resegment(request_input: object, *, caption_model: str = DEFAULT_CAPTION_MODEL, caption_timeout: int = DEFAULT_CAPTION_TIMEOUT_SECONDS) -> dict:
    request = parse_resegment_request(request_input)
    words = request["words"]
    use_llm = request["segmentationMode"] in {"auto", "llm"}
    with tempfile.TemporaryDirectory(prefix="spectra-caption-resegment-") as directory:
        transcript_path = Path(directory) / "transcript.json"
        transcript_path.write_text(json.dumps(_transcript(words), ensure_ascii=False), encoding="utf-8")
        if use_llm and caption_llm.configured():
            try:
                caption_llm.ensure_loaded(caption_model)
            except caption_llm.CaptionLlmError:
                pass  # generate_subtitles falls back to rule segmentation
        options = {"line_capacity": request["lineCapacity"]} if "lineCapacity" in request else {}
        with contextlib.redirect_stdout(sys.stderr):
            generated = generate_subtitles(
                str(transcript_path),
                str(Path(directory) / "captions.srt"),
                use_llm=use_llm,
                model=caption_model,
                timeout=caption_timeout,
                max_chars=request["maxChars"],
                max_lines=request["maxLines"],
                hide_punctuation=request.get("hidePunctuation", True),
                **options,
            )
    captions = []
    cursor = 0
    for index, subtitle in enumerate(generated["captions"]):
        start_id = subtitle.get("start_word_id")
        end_id = subtitle.get("end_word_id")
        if not isinstance(start_id, int) or not isinstance(end_id, int) or start_id != cursor or end_id < start_id or end_id >= len(words):
            raise AutoCaptionContractError("re-segmented captions do not own the input words in order")
        owned = words[start_id : end_id + 1]
        cursor = end_id + 1
        captions.append({
            "id": f"{request['jobId']}-caption-{index + 1}",
            "text": subtitle["text"],
            "startUs": owned[0]["startUs"],
            "endUs": max(owned[-1]["endUs"], owned[0]["startUs"] + 1),
            "sourceWordIds": [word["id"] for word in owned],
            "textStatus": subtitle.get("text_status", "as_transcribed"),
        })
    if cursor != len(words):
        raise AutoCaptionContractError("re-segmented captions did not consume every input word")
    return {
        "version": RESEGMENT_RESULT_VERSION,
        "jobId": request["jobId"],
        "method": generated.get("method", "unknown"),
        "captions": captions,
        "diagnostics": generated.get("diagnostics", []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spectra segmentation-only caption worker (no audio, no Whisper)")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--caption-model", default=DEFAULT_CAPTION_MODEL)
    parser.add_argument("--caption-timeout", type=int, default=DEFAULT_CAPTION_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = resegment(payload, caption_model=args.caption_model, caption_timeout=args.caption_timeout)
    except (OSError, json.JSONDecodeError, AutoCaptionContractError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
