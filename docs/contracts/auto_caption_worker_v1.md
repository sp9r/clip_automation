# Spectra Auto Caption Worker Contract v1

## Status

`auto_caption_worker_v1_draft.md`を基に実装・受入したlocal process contractです。将来の互換性を壊す変更はversionを上げます。これは`edit_project.v1`ではなく、video_editorとclip_automation workerの実行時境界です。

2026-08-16の実人間日本語QA Aでは、全source range 188.600秒を`large-v3 / cuda / float16`で処理し、666 raw words、85 captions、3 signal silencesを確認した。高品質区切りのtimeout時はrule fallbackをdiagnosticsへ記録する。workerはportable配布物ではなく、fine acoustic alignmentも提供しない。

## Request

```json
{
  "version": "spectra.auto-caption.request.v1",
  "jobId": "job:example",
  "mediaPath": "runtime-only",
  "sourceStartUs": 20000000,
  "sourceEndUs": 35000000,
  "language": "ja",
  "maxChars": 32,
  "maxLines": 2,
  "segmentationMode": "auto",
  "silenceCut": "youtube",
  "silenceMinUs": 150000
}
```

`mediaPath`はmain processが解決するruntime-only inputです。source rangeは選択clipの`sourceInUs`から`sourceInUs + durationUs`で、時刻はinteger microsecondsです。

`silenceMinUs`は任意の整数µsで、`0`〜`2,000,000`の範囲です。指定時は選択したsilence profileの推奨最小無音時間を上書きします。未指定時はprofileの推奨値を使用します。`0`は「FFmpegの検出結果からraw Whisper word保護区間以外を候補にする」ための明示設定であり、raw wordを削除してよいという意味ではありません。Stage 31.1時点の推奨値は、`natural=600,000`、`strong=400,000`、`youtube=150,000`です。`youtube`は保持pause `0`、`noiseThresholdDb=-35`、speech protection `80,000`、filler removal falseです。raw Whisper wordは引き続きspeech protection authorityであり、無音のauthorityはFFmpeg audio signalです。

## Result

result versionは`spectra.auto-caption.result.v1`です。`jobId`、source range、language、provider/model、`rawWords[]`、`captions[]`、`diagnostics[]`、`fallback`を返します。raw wordはstable id、text、startUs、endUs、optional confidenceを持ち、captionはid、text、sourceWordIds、startUs、endUs、textStatus、provenanceを持ちます。

result、progress、baselineにabsolute media path、project path、credential、tokenを含めません。consumer側でunknown field、range、ownership、size、malformed JSONを拒否します。

`subtitleTemplate`は組み込みIDまたはmain processが検証したsafeなuser template IDを表します。workerはtemplateの保存pathを解決・保存せず、字幕生成設定の識別子としてのみ扱います。templateのstyle/maxChars/maxLinesの永続化はvideo_editorのuserData側で行い、edit_project.v1にはtemplate pathを追加しません。

## Processing

workerはFFmpeg argvで要求範囲だけをtemporary audioへ抽出し、faster-whisper `word_timestamps=True`を実行します。字幕区切りは既存`caption.py`を再利用し、LLM unavailable時はrule fallbackとdiagnosticsを記録します。jumpcut、planning、highlight、render、publishは非対象です。

### Caption segmentation safety fallback

raw Whisper wordsが有効な場合、字幕品質処理の失敗だけでworkerを失敗扱いにしません。区切りは次の順で試行します。

1. LLM semantic segmentation
2. 既存heuristic / boundary optimizer
3. LLM・heuristic・post-processingを使わないdeterministic greedy failsafe

Tier 3はraw wordを時系列順に一度だけ消費し、最後のwordまでcaption coverageを保証します。`maxChars`または5秒のhard durationを次word追加前に適用しますが、単一wordが制約を超える場合はそのwordを単独captionとして保持します。source wordのtext・timingは書き換えず、LLM不正JSON、timeout、heuristic例外、品質post-processing例外では`CAPTION_LLM_FALLBACK`、`CAPTION_HEURISTIC_FALLBACK`、`CAPTION_FAILSAFE_FALLBACK`等のpath-free diagnosticsをresultへ記録します。raw wordsが空・timing不正・source mapping不能・worker fatal errorは従来どおりfail closedです。

## Whisper acceleration policy

worker CLIは`--acceleration-mode auto|gpu|cpu`を受け取ります。省略時は`auto`です。

- `auto`: CTranslate2のCUDA device availabilityを確認し、利用可能なら`device=cuda`、`compute_type=float16`でmodelをloadします。CUDAが利用できない、またはCUDA model loadに失敗した場合だけ、理由を記録してCPU `int8`へfallbackします。
- `gpu`: CUDA health checkまたはCUDA model loadが失敗した場合はerrorです。CPU fallbackを成功扱いにしません。
- `cpu`: CUDA availabilityに関係なく、明示的に`device=cpu`、`compute_type=int8`でloadします。

model construction自体をhealth checkとします。faster-whisper modelを実際にloadできた場合だけ、そのdeviceを成功とします。result/progressのdiagnosticsには、path-freeな`device`、`computeType`、`model`、`cudaAvailable`、`fallbackReason`を、`WHISPER_*` diagnostic entriesとして記録します。GPU指定失敗時もprogressへerror codeとruntime diagnosticsを残します。

## Truth and limitations

raw Whisper wordsは不変source truthです。character-level fine acoustic alignmentは実装していません。video_editorのSubtitleEditUnitは編集用derived metadataであり、推定時刻を音響truthとは扱いません。

## Failure and cleanup

missing media、cancel、timeout、nonzero exit、malformed resultではpartial outputを成功扱いにしません。temporary audio/resultはsuccess、failure、cancelの各経路でcleanupします。stdoutはmachine-readable progress JSONL、diagnostic logはstderrです。

## Worker lifecycle (Stage 3)

通常のone-shot CLIは従来どおり利用できます。複数jobを連続処理する場合は、同じプロセス内だけで保持する`WhisperModelCache`を使う`python spectra_caption_worker_server.py`を利用できます。serverのイベント境界は`worker-event.v1`、cancel/health controlは`worker-control.v1`で、request/result contractのversionは変更しません。

- cache keyはmodel、acceleration mode、path-freeなCUDA device fingerprintで、modelが同じでもGPU構成が変われば再初期化します。
- `cache=cold`／`cache=warm`はresult/progressのpath-free diagnosticsで確認できます。
- idle timeoutでmodel参照を破棄し、`gc.collect()`を実行してVRAM解放を促します。stdin close、timeout、worker crash時もcacheを破棄します。
- cancelはactive jobのcancel eventへ転送します。timeoutは安全なerrorを返してserverを終了し、親プロセスがserverを再起動できるdisposable/crash-isolated設計です。任意の例外文・media pathはイベントへ出しません。
- serverはproject path、media path、credential、tokenを保存しません。temporary audioは各jobの終了時に削除されます。

### Stage 3 benchmark evidence

生成QA mediaの先頭5秒を、同一プロセス・同一`large-v3` CUDA cacheで連続処理した実測は、cold約9.1秒、warm約1.9秒、約4.7倍でした。cold/warmのdiagnosticsはそれぞれ`cache=cold`、`cache=warm`、`device=cuda`、`computeType=float16`です。この差からmodel loadは無視できないため、one-shot reloadを強制せず、serverまたはprocess-local cacheを利用します。

## Caption LLM runtime diagnostics (Stage 4)

高品質字幕区切りのOllama runtimeは、設定値ではなく推論後に公式`GET /api/ps`を読み、対象modelの`size`と`size_vram`から分類します。`size_vram == 0`は`cpu`、全量がVRAMにある場合は`gpu`、一部だけなら`mixed`、API接続不能・model未ロード・不正応答は`unavailable`です。未確認のOllama server optionは設定しません。

formal worker CLI/serverでruntime collectionを有効にすると、result/progressへ次のpath-free diagnosticsを追加します。

- `CAPTION_AI_RUNTIME`: `status=gpu|cpu|mixed|unavailable`
- `CAPTION_AI_MODEL`: model identifier
- `CAPTION_AI_MODEL_SIZE_BYTES` / `CAPTION_AI_VRAM_BYTES`: official runtime memory values

Ollamaの失敗時も既存のrule fallbackを維持し、runtime診断の失敗だけで字幕生成を失敗扱いにしません。公式仕様は`https://docs.ollama.com/api/ps`、GPU placementの解釈は`https://docs.ollama.com/faq`を参照します。
