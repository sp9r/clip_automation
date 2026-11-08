# NIGHT 04 Whisper Truth / Freshness / GPU Verification

更新日: 2026-08-16

## 判定

生成QA media `stage8-ja.mp4`を、既存のproduction worker経路へ新規job IDで投入した。速度だけで成功判定せず、selected source range、bounded audio、result identity、Whisper runtime、時間coverageを突き合わせた。

```text
FRESH FULL-RANGE WHISPER: PASS
ACTUAL CUDA INFERENCE: PASS
STALE RESULT REUSE: NOT OBSERVED
SELECTED RANGE TRUNCATION: NOT OBSERVED
```

## Fresh full-range run

| 項目 | 実測値 |
|---|---:|
| media duration | 103.240998 sec |
| sourceStartUs | 0 |
| sourceEndUs | 103240998 |
| bounded extracted audio | 103.241000 sec |
| extraction wall time | 148.5 ms |
| worker wall time | 77,124.4 ms |
| rawWords | 311 |
| captions | 34 |
| detected language | `ja` (probability 0.99951171875) |
| contract language | `auto` (requested auto-detection sentinel) |
| segmentation | completed |

The extracted audio duration matches the selected media range within 2 microseconds. The worker uses bounded FFmpeg extraction with the requested range; it does not transcribe an accidental short prefix.

## Runtime evidence

The result diagnostics from this fresh job were:

```text
WHISPER_MODEL=large-v3
WHISPER_DEVICE=device=cuda
WHISPER_COMPUTE_TYPE=computeType=float16
WHISPER_CUDA_AVAILABILITY=cudaAvailable=true
WHISPER_FALLBACK=fallbackReason=none
WHISPER_CACHE=cache=cold
```

During the run, `nvidia-smi` sampling observed a Python worker compute process, maximum GPU utilization of 100%, and maximum reported GPU memory of 11,141 MiB. WDDM process-memory attribution is limited, so the acceptance conclusion combines this observation with CTranslate2/faster-whisper runtime diagnostics and successful CUDA inference; it does not rely on a single memory counter.

## Speech coverage

Raw word and caption counts were present in every third of the selected range:

| range | raw words | captions | sample |
|---|---:|---:|---|
| early | 105 | 13 | `こんにちは` / `これはスペクトゥーラービ…` |
| middle | 102 | 10 | `日本語の連続発話では` / `字幕の区切り単語の所有…` |
| late | 104 | 11 | `長い文章ではユーザーが動画…` / `の最初から最後までを通して…` |

This is a generated Japanese TTS QA media with known speech content. The beginning, middle, and end all produced non-empty transcript/caption coverage; there was no evidence that only a few seconds were processed.

## Freshness and cache semantics

- The output path was a new temporary path and did not exist before the run.
- The result `jobId` exactly matched the newly generated request `jobId`.
- The result was published once after Whisper, segmentation, and silence analysis completed.
- `WHISPER_CACHE=cache=cold` confirms this one-shot run did not accidentally reuse a warm model result.
- No transcription-result cache exists in the one-shot worker; the process-local warm cache belongs only to the disposable worker server and stores the model object, not project/media paths or result payloads.

## Warm worker reference

The existing Stage 3 same-process lifecycle acceptance remains the warm-worker evidence. On the same generated QA media and GPU backend, a 5-second job measured:

| job | wall time | cache | backend |
|---|---:|---|---|
| cold | 9,063.4 ms | cold | CUDA / float16 |
| warm | 1,923.0 ms | warm | CUDA / float16 |

The warm path is therefore a model-load optimization, not a result cache. The one-shot production contract remains disposable and fresh by default.

## Failure / regression result

No skip, stale-result, range truncation, or GPU-to-CPU fallback bug was found in this acceptance run. No production code change was necessary. Targeted worker, acceleration, lifecycle, auto-processing tests and compileall passed after the run.

The contract still reports the requested `language` sentinel (`auto`) while the actual detected language is emitted by faster-whisper diagnostics/stderr as `ja`. A future additive diagnostic may expose the detected language in machine-readable result fields; this NIGHT 04 verification does not change the existing contract.

Stage 5, planning, highlight, publishing, and mass Human Review remain out of scope.
