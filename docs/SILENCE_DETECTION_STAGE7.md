# Stage 7: High-Precision Silence Detection

2026-08-16時点のproduction pipelineでは、旧`jumpcut.py`のfiller判定を無音カットの正本として使用しない。`silence_detection.py`が音声信号を正本としてFFmpeg `silencedetect`を実行し、フィラー分類・フィラー削除は行わない。

## 入力と判定

- `ffprobe`で取得したメディア長とFFmpegの`silencedetect`イベントを整数µsへ変換する。
- Naturalは最小無音600,000µs、保持する無音余白180,000µs。
- Strongは最小無音400,000µs、保持する無音余白100,000µs。
- YouTubeジャンプカットは最小無音300,000µs、保持する無音余白80,000µs。強く詰める用途だが、音声信号とraw word保護の境界は他profileと同じである。
- デフォルトのノイズ閾値は`-35dB`。
- `transcript_words` / raw Whisper word intervalは無音の発見には使わず、検出された削除候補がword intervalへ食い込まないための保護区間としてだけ使う。
- フィラーらしい文字列でも、音声信号上の発話区間にあるwordは削除しない。フィラー削除を示す処理や診断は生成しない。

## 出力

`silence_analysis.json`に次を保存する。

- `silences[]`: 音声信号で検出した区間、保持余白後の`removedRanges[]`、保護の有無
- `keepRanges[]`: source時間の保持区間
- `removedDurationUs`: 実際の削除候補の合計
- `settings`: profile、閾値、ノイズレベル、word保護値、`fillerRemoval=false`
- `diagnostics`: `AUDIO_SIGNAL_AUTHORITY`、`WHISPER_WORD_PROTECTION`、`FILLER_REMOVAL_DISABLED`

上記の時間値は整数µsである。既存のEDL consumerとの互換性のため、`edl.json`は秒の数値で出力するが、正本の判定結果は`silence_analysis.json`の整数µsである。`clean_transcript.json`はwordを削除せず、keep range後の時間へ写像する。

## pipeline境界

legacy stage IDの`jumpcut`はmanifest互換のため残すが、通常の`pipeline.py` / `production_pipeline.py`は`generate_silence_cut_data()`を呼ぶ。`production_pipeline.generate_jumpcut_data`は既存テスト・外部統合が明示的に注入した場合だけ互換hookとして使われ、通常値は`None`である。旧`jumpcut.py`をproductionの無音判定へ直接接続しない。

## 実QA

生成済みの安全な日本語QA media（約103.241秒）を読み取り専用で検証した。

- Natural: 28 silence intervals、29 keep ranges
- removed candidate: 21,024,959µs
- `fillerRemoval=false`
- signal authority / word protection / filler disabled diagnosticsを確認
- absolute media pathの結果漏洩なし

これは安全側の初期profile確認であり、話者・録音環境ごとの完全な品質保証ではない。false speech cutを優先して避けるため、今後の調整も音声fixtureと実QAで行う。

### Night 24 real-video smoke

corpus A（188.600秒）をvideo_editorの統合自動処理から実行し、Natural profileが完走した。3件のsilence候補、3.839660秒の削除候補、keep rangeと字幕・AV mappingの生成を確認した。Whisper raw wordとのspeech overlapで発話を削除する候補はなく、filler removalも発生していない。これはAに対する受入結果であり、録音条件の異なる全動画での品質保証ではない。background noiseが強い媒体では安全側no-opになる可能性がある。

## 非対象

- filler removal
- Whisper word gapだけによる無音確定
- jumpcut.pyの全面削除・既存legacy runの書換え
- planning、highlight、render、publish
