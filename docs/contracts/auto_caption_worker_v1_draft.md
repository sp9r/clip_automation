# Auto Caption Worker Contract Draft

Status: Stage 1 draft。永久schemaとして確定していない。実workerのrange実装、cancel/progress、長尺メモリ使用量、real acceptanceを確認してから正式版へ昇格する。

## 目的と境界

この契約は、standalone `video_editor` と `clip_automation` のauto-caption workerを結ぶlocal process boundaryである。

```text
video_editor
  → JSON request/result boundary
  → clip_automation worker
```

video_editorはPython moduleをimportせず、clip_automationはvideo_editorのProject Model、history、IPC、UIをimportしない。字幕品質アルゴリズムはworker側に置き、TypeScriptへ複製しない。

`docs/AI_EXTENSION_GUIDE.md`のWhisper providerはprovider実装の例であり、実Whisper providerの存在を意味しない。現在の`caption_bridge.py`（`spectra.caption_bridge.v1`）はこのdraftへ移行する前の互換bridgeである。

## Request: `spectra.auto-caption.request.v1`

JSON objectの必須フィールド:

```json
{
  "version": "spectra.auto-caption.request.v1",
  "jobId": "caption-job-001",
  "mediaPath": "C:/private/source.mp4",
  "sourceStartUs": 10000000,
  "sourceEndUs": 15000000,
  "language": "ja",
  "maxChars": 32,
  "maxLines": 2,
  "segmentationMode": "rule"
}
```

- `jobId`は呼出しごとの安定した識別子。
- `mediaPath`はworkerが読む入力であり、main processが選択assetから解決する。これはrequestに限り許可される。resultやbaselineには保存しない。
- `sourceStartUs`〜`sourceEndUs`はsource media上の整数microsecondsで、`sourceEndUs > sourceStartUs`。
- `maxChars`は既存caption algorithmが検証する字幕block文字数上限。`maxLines`は既存設定で受け取れるrenderer-ownedの表示行数制約であり、workerが表示幅を再実装する意味ではない。
- `segmentationMode`は`auto`、`rule`、`llm`のいずれか。LLMを使わない場合もfallbackを隠さずresultへ記録する。
- `mediaPath`以外のpath、project path、credentials、tokenはrequestにも追加しない。

## Result: `spectra.auto-caption.result.v1`

```json
{
  "version": "spectra.auto-caption.result.v1",
  "jobId": "caption-job-001",
  "sourceStartUs": 10000000,
  "sourceEndUs": 15000000,
  "language": "ja",
  "provider": "faster-whisper",
  "model": "large-v3",
  "rawWords": [
    {"id": "word-1", "text": "字幕", "startUs": 10100000, "endUs": 10300000, "confidence": 0.9}
  ],
  "captions": [
    {
      "id": "caption-1",
      "text": "字幕",
      "sourceWordIds": ["word-1"],
      "startUs": 10100000,
      "endUs": 10300000,
      "textStatus": "as_transcribed",
      "provenance": "caption_algorithm"
    }
  ],
  "diagnostics": [],
  "fallback": {"used": false}
}
```

### 時刻とownership

- resultの全時刻はsource media絶対時刻の整数microseconds。秒floatへ戻さない。
- `rawWords[].id`はworker run内で一意なstable ID。word textとsource timingの正本である。
- `captions[].sourceWordIds`はcaptionが所有するwordの順序付き参照で、unknown/重複IDを許可しない。
- `textStatus`は`as_transcribed`、`display_normalized`、`manually_corrected`、`provenance`は`caption_algorithm`、`rule_fallback`、`llm`、`unknown`の範囲で記録する。
- caption textは表示本文であり、将来のmanual correctionをworker resultへ混ぜない。初回生成のbaselineは別artifactとして保存する。

### 秘密・pathの禁止

resultにはabsolute media path、project path、source path、credentials、access/refresh token、client secretを含めない。diagnosticもpathや秘密をmessageへ埋め込まない。両側validatorは未知fieldと禁止fieldを拒否する。

## Source rangeとtimeline mapping

選択されたvideo clipの正本は次の3値である。

```text
clip.startUs
clip.sourceInUs
clip.durationUs
```

worker requestは、次の範囲だけを処理する。

```text
sourceInUs <= sourceUs < sourceInUs + durationUs
```

worker結果はsource media絶対時刻のまま返す。Studioへ配置するときのmappingは次で固定する。

```text
timelineWordUs = clip.startUs + (sourceWordUs - clip.sourceInUs)
```

source range外のwordはclipへ配置しない。`sourceInUs == 10s`、`clip.startUs == 2s`ならsource 12.5sはtimeline 4.5sである。これはjumpcut timelineやsequence timeをworker側で再計算する意味ではない。

## Immutable baseline

自動字幕生成直後のworker resultを、review logとは別のcopy-on-write artifactとして保存する。

```text
experimental.auto-caption-baseline.v1
```

保存先はvideo_editorのuserData配下（実装時にapp-owned subdirectoryを固定）とし、project/runへ直接書き込まない。最低限次を含める。

- `jobId`, `createdAt`
- `projectFingerprint`, `mediaFingerprint`
- `sourceClip`: `clipId`, `assetId`, `startUs`, `sourceInUs`, `durationUs`
- `timelineMapping.formula`, `clipStartUs`, `sourceInUs`
- `rawWords`, `captions`
- request由来の`settings`
- `provider`, `model`, `diagnostics`, `fallback`

baselineにもpath、credentials、tokenを含めない。人手reviewのrating・修正操作・current captionは別artifactであり、baselineを上書きしない。

## Validation status

- clip_automation: `auto_caption_contract.py`でrequest/result/baselineとmappingを検証する。
- video_editor: `src/shared/auto-caption-contract.ts`で同じversion、整数時刻、ownership、禁止field、path-free baselineを検証する。
- Stage 1では現行`caption_bridge.py`の実行経路をformal v1へcutoverしない。
- Stage 1ではUI、IPCのrequest形状、Whisper range decode、baseline persistence、Project Model adapterを実装しない。

## Stage 2 worker implementation

`spectra_caption_worker.py`は、このdraft contractを入出力とするstandalone workerである。
CLIは次の形で起動する。

```text
python spectra_caption_worker.py --request request.json --output result.json --progress progress.jsonl
```

workerの責務は、選択されたsource rangeの一時WAV抽出、既存`transcribe.py`による
`word_timestamps=True`のWhisper実行、既存`caption.py`／`caption_boundary_v2.py`
経路によるcaption segmentation、resultのvalidationとatomic writeだけである。
`jumpcut`、filler削除、planning、highlight、render、publish、旧pipelineはimport・実行しない。

- FFmpegはargv配列を`subprocess.Popen(..., shell=False)`へ渡し、`sourceStartUs`と
  `sourceEndUs`の差分だけをmono 16 kHz WAVへdecodeする。worker終了時は成功・失敗・cancelを問わず一時directoryをcleanupする。
- Whisperのword text、start/end、confidenceはsource range相対結果へsourceStartUsを加算し、
  `source-word:<index>` IDを付ける。wordを後処理で書き換えたり、存在しないcharacter-level
  acoustic alignmentを推測したりしない。result diagnosticsへ`NO_FINE_ACOUSTIC_ALIGNMENT`を記録する。
- `segmentationMode=auto|llm`は既存LLM経路を使用し、`llm+fallback`は
  `fallback.used=true`と`LLM_SEGMENTATION_FALLBACK`を記録する。`rule`は明示的なrule経路として
  `RULE_SEGMENTATION`を記録し、LLM fallbackとは区別する。
- stdoutはJSONL progress専用、既存transcribe/captionの人間向けログと失敗理由はstderrへ分離する。
  progress stateは`preparing`、`transcribing`、`segmenting`、`completed`で、失敗・cancelは
  `failed`とerror codeを出す。result JSONは処理完了・contract validation成功後にだけatomic公開する。
- result／diagnosticsへabsolute media path、project path、credentials、tokenを含めない。

Stage 2ではvideo_editorへのIPC接続、Project Modelへのsubtitle adapter、baseline永続化、
実Whisper常時受入をまだ行わない。fake transcriberによるcontract/range/ownership/fallback/cancel/
cleanupテストを先に固定し、実Whisperは環境依存の短いsmokeとして別途扱う。

## 次の実装候補

1. request生成をselected clipに接続し、source rangeをworkerへ渡す。
2. workerがbounded source rangeを処理し、正式resultを出力する。
3. resultをProject Modelのsubtitle clipへadapter変換し、mappingとword ownershipを検証する。
4. generation直後にpath-free baselineをatomic保存する。
5. cancel/progress、failure retry、generated-media real acceptanceを追加する。
