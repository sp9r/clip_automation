# NIGHT 15 — Real AV + Caption Mapping Acceptance

## 判定

- 実動画Aのsilence plan完走: **PASS**
- linked video/audioの同一cut plan: **PASS**
- source→post-cut mapperによる字幕同期: **PASS**
- raw wordがremoved rangeへ入るplanのreject: **PASS**
- Undo/Redo、Save/reopen、最終render: **PASS**（video_editor一時acceptance harness）

## 実測

NIGHT 05 corpusのAを使用した。30秒のbounded source rangeでは、実Whisper `large-v3 / cuda / float16`により94 raw words、10 captions、2 silence ranges、removed duration 1.365660秒を得た。full 188.6秒rangeでもworkerを完走し、666 raw words、82 captions、3 silence ranges、removed duration 3.839660秒となった。

実測中、同時刻のWhisper point wordsを含むcaptionがpositive durationを持たず、worker result validationで停止する再現バグを検出した。raw wordの`startUs/endUs`は変更せず、隣接する実時間の範囲だけをdisplay captionへ分配する修正を追加した。分配できない場合はfail-closedし、`POINT_WORD_DISPLAY_TIMING` diagnosticを付与する。

## Mapping acceptance

video_editorの一時harnessで30秒成果物をProject Modelへ適用した。

- source absolute `keepRanges`からvideo/audioを同じsegment境界で生成。
- 各segmentの`sourceInUs`、`durationUs`、`timeline startUs`が一致し、timeline gapはない。
- raw Whisper words、caption start/end、`SubtitleWord`、`SubtitleEditUnit`は同じ`createSilenceCutTimeMapper`を通過。
- source wordがremoved rangeへ重なる場合は、Project apply前に`CUT PLAN INVALID`としてrejectする既存経路を確認。
- 冒頭・中盤・後半のmapped timelineが単調増加し、後半へ向かうdriftは確認されなかった。
- source media fileは変更されない。

## Transaction / render

- silence cutと字幕track適用後のProjectを1回のhistory applyとして扱い、Undo 1回でcut前、Redo 1回でcut後へ復元。
- JSON保存→validateによるreopenでAV segmentとsubtitle timingが一致。
- software FFmpeg final renderを実動画30秒rangeで実行し、ffprobeでvideo/audio streamとcut後durationを確認。Render Planのaudio contributionは独立audio clipのみで、video由来audioの二重登録はなかった。

## 変更

- `spectra_caption_worker.py`: point-word captionのderived display intervalを追加。raw timingは不変。
- `tests/test_spectra_caption_worker.py`: point-word runのpositive display timingとraw timing保持を回帰化。

このQAは実動画の一時copy/artifactで行い、動画本体・絶対path・render生成物はGitへ保存していない。
