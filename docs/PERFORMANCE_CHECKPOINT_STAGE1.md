# Performance Checkpoint: Long-Run Queue Stage 1

更新日: 2026-08-15  
対象: 実runtimeのCPU/GPU監査（最適化・大規模architecture変更は未実施）

## Scope

生成QA media `stage8-ja.mp4`（103.240998秒、1280x720、H.264/AAC、22.05kHz mono）を、Dedicated Spectra Auto Caption Workerへ1回だけ入力した。個人動画、既存production run、credentialは使用していない。workerの入力は全区間 `[0, 103240998)` microseconds である。

## Measured wall time

| Stage | Wall time | Runtime evidence |
|---|---:|---|
| source audio extraction | 169.9 ms | workerのbounded FFmpeg argv。`-ss`/`-t`、mono、16kHz PCM WAV。全source decode後の切り出しではない |
| faster-whisper model load + inference | 31,508.4 ms | `large-v3`、`device="cuda"`、`compute_type="float16"`。現実装の計測境界ではloadとinferenceを分離していない |
| caption segmentation total | 42,402.2 ms | 既存`caption.py`のLLM segmentation、method=`llm`、fallbackなし |
| Ollama | 27,643.5 ms + 14,504.5 ms = 42,148.0 ms | 2回の実HTTP call。`qwen2.5:14b`、実行中`ollama ps`は`100% GPU` |
| worker total | 74,114.5 ms | result: 311 raw words、34 captions、fallback=false |

既存worker経路にsilence analysis / jumpcutは含まれない。Whisperの`vad_filter=True`は文字起こし内部のVADであり、無音カットstageの実行とは扱わない。

## GPU / runtime evidence

- GPU: NVIDIA GeForce RTX 3060、driver 610.47、NVIDIA-SMI CUDA UMD Version 13.3。
- CTranslate2: 4.8.1、`cuda_device_count=1`。CUDA supported compute typesに`float16`を含む。
- faster-whisper: 1.2.1。実workerは`WhisperModel(..., device="cuda", compute_type="float16")`で成功した。
- Whisper区間のruntime sample: GPU utilizationは概ね93–100%、GPU memoryは約6.6–6.9 GiB。GPU fallbackを示す挙動は観測しなかった。
- Ollama: 0.32.11。caption segmentation中に`qwen2.5:14b`が`100% GPU`、size 9.5 GBとして確認された。
- CUDA libraries: venvにCTranslate2の`cudnn64_9.dll`、NVIDIA cuDNN 9 DLL群、cuBLAS 12 DLL群が存在し、CUDA worker実行に成功した。cuDNN/cuBLASの個別kernel選択までは今回の監査では分離計測していない。

WDDM環境の`nvidia-smi` process memory attributionは権限制約で限定的だったため、process名による個別GPU memory帰属ではなく、workerのCUDA設定、CTranslate2 device count、実推論成功、実行中のGPU utilization/memoryを組み合わせて判定した。

## FFmpeg / preview / final render

- FFmpeg: `N-111510-gbe7ac511a6 (2023-07-19)`。
- 利用可能なencoder: `h264_nvenc`、`hevc_nvenc`、`h264_amf`、`hevc_amf`、`h264_qsv`、`hevc_qsv`。利用可能なhwaccel: `cuda`、`dxva2`、`qsv`、`d3d11va`、`opencl`、`vulkan`。
- Accurate Preview smoke: command wall time 13,979 ms（production buildを含む）。Accurate subtitle overlay cold render 118.289 ms、cache hit 0.119 ms、cue switch 172.568 ms。これは既存のgenerated smoke fixtureであり、103秒QA media全体をPreview decodeした時間ではない。
- Final render: QA mediaと34字幕を含む一時`edit_project.v1`を公開headless CLIでrender。wall time 34,648 ms、選択encoder `libx264`、出力はH.264/AAC、1920x1080、30fps、103.241000秒、exit 0、ffprobe PASS。

## CPU load classification

| Class | 判定 | 根拠 |
|---|---|---|
| A GPU fallback | 観測なし | Whisper/OllamaともGPU runtime evidenceあり |
| B CPU preprocessing | あり（限定的） | source audio extractionはFFmpegのsoftware audio decode/resample経路 |
| C software FFmpeg filter/encode | 確認 | final renderで`libx264`が選択され、字幕ASS/filterを含むrenderをCPU encoderで実行 |
| D Ollama CPU offload | 観測なし | `ollama ps`が`100% GPU` |
| E model load | あり得る | 31.508秒はmodel load＋inferenceの合算で、現状は分離不能 |
| F other | 未分類 | 今回の測定値だけから追加原因を断定しない |

## Conclusion / deferred work

このcheckpointは最適化の決定ではない。現時点で最も明確なCPU要因は、video final renderの`libx264`選択とsoftware filter pathである。一方、Auto CaptionのWhisperとOllamaは実行時にGPUを使用している。次の最適化判断では、model load/inferenceの分離計測、FFmpeg filter/encoder別CPU計測、NVENC実probeの成功条件、長尺Previewのsource decode範囲を追加で測る。

Stage 1ではarchitecture変更、backend設定変更、production pipeline変更を行わない。

## 2026-08-16 Stage 17: Real GPU performance acceptance

Stage 17の詳細は[`STAGE17_REAL_GPU_PERFORMANCE_ACCEPTANCE.md`](STAGE17_REAL_GPU_PERFORMANCE_ACCEPTANCE.md)へ分離した。生成日本語QA mediaの先頭60秒をGPU cold/warmとexplicit CPUで実測し、Whisper実backend、GPU process visibility、Ollama placement、NVENC render、video_editor headless renderを確認した。

- GPU Whisper: `cuda/float16`、cold 24,573.5ms、warm 22,594.8ms。CPU explicit: `cpu/int8`、102,152.9ms。GPU指定時のsilent CPU fallbackなし。
- 全体wall time: GPU cold 207,124.9ms、GPU warm 204,979.3ms、CPU 284,684.0ms。caption LLMが約182秒を占めるため、全体の絶対速度目標は設定しない。
- GPU processは実行中`nvidia-smi`へ表示。Ollama `qwen2.5:14b`は`100% GPU`。
- legacy/acceleration renderの60秒実renderは`h264_nvenc`＋CUDA decodeで6,082.4ms、ffprobe PASS。video_editor headless CLIは`libx264`で36,738.9ms、ffprobe PASS。両者のencoder selection差は残課題。
- caption segmentationはLLM call後の`llm+fallback`だった。性能受入は完了したが、高品質字幕品質受入とは混同しない。

## 2026-08-16 Stage 7: High-Precision Silence Detection

Stage 7では、旧`jumpcut.py`を無音判定へ直接利用せず、`silence_detection.py`のFFmpeg `silencedetect`を音声信号authorityとして使う経路を追加した。Whisper raw word intervalは、検出済み削除候補を保護するためだけに使い、word gapから無音を推定しない。フィラー分類・フィラー削除は行わない。

生成済みの安全な日本語QA media（約103.241秒）の読み取り専用実測では、Natural設定で28 silence intervals、29 keep ranges、削除候補21,024,959µsを得た。結果の`silence_analysis.json`は整数µsで、`AUDIO_SIGNAL_AUTHORITY`、`WHISPER_WORD_PROTECTION`、`FILLER_REMOVAL_DISABLED`を記録した。出力にmedia absolute pathは含めない。

Stage 7の対象はsilence cutだけであり、filler removal、planning、highlight、publishへ拡張していない。既存manifest互換のstage ID `jumpcut`とlegacy `jumpcut.py`は残すが、通常production pipelineは新解析器を使う。

## Stage 3: Whisper worker lifecycle benchmark (2026-08-15)

Stage 1で計測した31.508秒の`model load + inference`境界を分離するため、生成QA media `stage8-ja.mp4`の先頭5秒を、同一process内の`WhisperModelCache`へ2回投入した。実行は`large-v3`、`device=cuda`、`compute_type=float16`、`segmentationMode=rule`である。

| Job | Wall time | Cache/runtime evidence |
|---|---:|---|
| cold | 9,063.4 ms | `cache=cold`, `device=cuda`, `computeType=float16` |
| warm | 1,923.0 ms | `cache=warm`, `device=cuda`, `computeType=float16` |

warmはcoldの約4.7倍高速で、model loadは無視できない主要bottleneckと判断した。実装はone-shot contractを変更せず、必要時に`python spectra_caption_worker_server.py`を起動してprocess-local cacheを共有する方式とした。serverの実GPU単発smokeではstarted→progress→result→stopped、`cache=cold`、GPU runtimeを確認した。fake runnerのserver回帰では2job目が`cache=warm`となること、cancel、timeout境界、crash isolation、idle release、GPU fingerprint変更を確認した。

### Lifecycle safety decision

- persistent stateはmodel objectのみで、project/media path、credentials、tokenは保存しない。
- idle timeout、stdin close、timeout、crash時はcacheを破棄する。timeoutはsafe error後にserverを終了し、親processが再起動できる。
- cancelはjobのtemporary FFmpeg processとWhisper loopへ伝播する。partial resultはsuccessにしない。
- GPU変更はpath-free fingerprintでcache keyを無効化し、再loadする。

Stage 3ではFFmpeg encoder/filter、Preview、production pipelineの大規模最適化は行っていない。

## Stage 4: Caption LLM GPU runtime audit (2026-08-15)

### Actual fixture

`data/runs/20260808_181039/clean_transcript.json`を既存`caption.py`へ投入し、`qwen2.5:14b`、LLM segmentation、`max_chars=22`、`max_lines=2`で実行した。出力は65字幕、`method=llm`、fallbackなし、wall timeは81,901.4msだった。

### Runtime evidence

| Evidence | Result |
|---|---|
| Ollama version | 0.32.11 |
| Model | `qwen2.5:14b` / 14.8B / Q4_K_M |
| `ollama ps` during inference | `100% GPU` |
| Official `/api/ps` `size` | 9,470,098,799 bytes |
| Official `/api/ps` `size_vram` | 9,470,098,799 bytes |
| NVIDIA sample | up to 100% utilization, 11,381 / 12,288 MiB VRAM |
| Caption wall time | 81,901.4ms / 65 captions |

The official `/api/ps` endpoint reports the loaded model and VRAM size; the official `ollama ps` processor column reports the placement. Both agree that this model is fully GPU-resident on the audited machine. The status interpretation follows Ollama's documented `100% GPU` / `100% CPU` / mixed processor semantics. No undocumented option or model replacement was introduced.

### Diagnostic implementation

Formal worker CLI/server paths can collect a path-free caption runtime diagnostic after LLM segmentation:

```text
CAPTION_AI_RUNTIME=status=gpu|cpu|mixed|unavailable
CAPTION_AI_MODEL=model=qwen2.5:14b
CAPTION_AI_MODEL_SIZE_BYTES=modelBytes=9470098799
CAPTION_AI_VRAM_BYTES=vramBytes=9470098799
```

If the official endpoint is unavailable, the diagnostic becomes `unavailable`; caption generation is not failed solely because telemetry failed. Existing LLM failure → rule fallback semantics remain unchanged. The audit does not claim that wall time is all GPU compute time; it includes the existing LLM request and response/validation overhead.

## 2026-08-16 Stage 5: FFmpeg GPU acceleration verification

Stage 1で`libx264`が選択されたfinal renderは、公開headless CLIから呼ばれる別repoの`video_editor` renderer経路だった。今回の変更対象はこのrepoのlegacy `clip.py` render経路であり、video_editorの実装は変更していない。

`clip.py`の`encoder="auto"`は、1フレームの実FFmpeg `h264_nvenc` probeを行ってから選択する。progressとrender resultへ、`encoder_class`、`encoder_fallback`、`encoder_fallback_reason`、`decode_mode`、`decode_fallback`、`decode_fallback_reason`、software filter policyをpath-free diagnosticsとして返す。明示`h264_nvenc`のprobe失敗は`libx264`へ黙ってfallbackせず、autoだけが理由付きCPU fallbackを許可する。

実機RTX 3060 / 12GB、FFmpeg `N-111510-gbe7ac511a6`、Stage 8生成QA mediaで以下を確認した。

| Check | Result |
|---|---|
| actual NVENC probe | PASS、`h264_nvenc`を実encode |
| auto encoder selection | `h264_nvenc`, `encoder_class=gpu`, fallbackなし |
| actual NVDEC probe | PASS、`-hwaccel cuda`でsource frame decode |
| 5秒 render | PASS、`h264_nvenc`、640x360、H.264/AAC、約4.0秒 |
| ASS subtitle render | PASS、NVENC + CUDA decode + software `subtitles` filter |
| 30秒 render | PASS、`h264_nvenc`、CUDA decode、約5.1秒 |
| output validation | ffprobeでH.264 video / AAC audioを確認 |

`hardware_decode="auto"`はCUDA/NVDECの1フレームprobe成功時だけ入力へ`-hwaccel cuda`を指定する。`-hwaccel_output_format cuda`は使わず、select/scale/pad/fps/overlay/subtitles/libassのfilter graphはsoftware側に残した。これによりsoftware-only filterを無理にCUDA化せず、Accurate Preview / Final parityを維持する。実render中はdecoder utilization最大79%、GPU memory約1.8--2.0GiBを観測したが、encoder utilization counterはsample上0だったため単独の成功判定には使わず、実NVENC probe、FFmpeg成功、出力codec、render resultをacceptance evidenceとした。

Stage 5ではCPU使用率0%を目標にせず、heavy video decode/encodeをGPUへ逃がせることとfilterの正しさを両立させた。video_editor v2 production renderer、UI redesign、Stage 6以降のpipeline変更は対象外である。

## 2026-08-16 Stage 6: Unified Acceleration UX

- legacy Spectra UIへ`auto`（推奨） / `gpu`（GPU優先） / `cpu`の処理デバイス設定を追加した。設定はplatform userDataの`processing_preferences.json`へatomic保存し、ProjectFileへmachine-specific backendを保存しない。
- GPU、Whisper、字幕AI、Renderのruntime statusをUIへ表示する。GPUはCTranslate2 CUDA＋実NVENC probe、字幕AIはOllama公式runtime endpointを根拠にする。
- `gpu`ではWhisperのCPU fallback、legacy renderのlibx264 fallback、GPU未配置の字幕AIを成功扱いにしない。`auto`のみ既存の安全fallbackを許可する。`cpu`ではWhisperとlegacy FFmpegを明示CPUへ固定する。
- v2は外部video_editor process境界を維持し、`edit_project.v1`へbackendを追加していない。v2 renderer内部のencoder policyはvideo_editor側の責務として残る。

## 2026-08-16 Stage 18: Long-run stability

詳細は[`STAGE18_LONG_RUN_STABILITY.md`](STAGE18_LONG_RUN_STABILITY.md)へ分離した。同一GPU direct worker processで5秒区間を3件連続処理し、Whisper cold 9,728.3ms → warm 1,791.7ms / 1,695.4ms、cancel → warm recovery 1,811.1msを確認した。GPU指定時のsilent CPU fallbackはなく、NVENC probe / CUDA decode probeもPASS。

- GPU memory最大5,884 / 12,288MiB、process終了後約1,883MiB。persistent VRAM leakは観測なし。
- `PipelineCancelled`を`WorkerCancelled`へ正規化し、cancel後のpartial result非公開とtemporary cleanupを維持。full unittest 291件、compileall PASS。
- Ollama daemonは利用可能だが、今回active modelは保持していない。Stage 17の`qwen2.5:14b` 100% GPU実測は維持し、loaded-model同時VRAM stressは未実施。

## 2026-08-16 NIGHT 22: Full regression / performance acceptance

実人間日本語QA corpus A（188.600秒、video+audio）を新しいjobId・一時artifactで全範囲処理した。最初のauto+Natural実行は、LLM fallback後の字幕後処理例外でresultを公開できない不具合を再現した。`caption.py`がimmutable Whisper wordsから最終rule fallbackを再構築するよう修正後、同じ範囲がcompletedし、result/silence artifactをatomic生成した。

| Stage | Evidence |
|---|---|
| audio preparation | FFmpeg bounded extract 0.839s、188.6s mono 16kHz WAV |
| Whisper cold | 50.898s、666 raw words / 47 segments |
| Whisper warm | 38.472s、666 raw words / 47 segments、same-process cache hit |
| Whisper runtime | `large-v3`, `cuda`, `float16`, `cudaAvailable=true`, fallback none |
| silence analysis | 0.363s、3 signal silence ranges、removed 3,839,660us |
| caption segmentation | 124.149s、85 captions、LLM timeout後に`llm+fallback`を明示 |
| integrated worker | 97.563s、auto + Natural、completed、666/85、silence 3件 |
| render | FFmpeg実機 `h264_nvenc` 8.721s、188.600s H.264/AAC、ffprobe PASS |

Whisperの「爆速」は短いrange・stale result・skipではなかった。full 188.6秒を対象にsource rangeを抽出し、raw wordsが666件、冒頭・中盤・後半coverageがあり、runtime diagnosticsがCUDA/float16、jobIdとresultがfreshで一致したため、GPUによる正常な高速化と結論する。字幕LLMはtimeout/fallbackを含むため、caption stageはWhisperより支配的になり得る。

`apply`はworker外のElectron main-process内でProjectを一回のtransactionへ変換するため、今回CLI段階では独立wall timeを観測していない。全量apply/undo/redoの正しさは既存video_editor regressionで確認し、速度値を捏造しない。詳細なStage 22 reportはObsidianへ記録した。
