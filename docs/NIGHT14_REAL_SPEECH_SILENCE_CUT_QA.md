# NIGHT 14 — Real Human Speech Silence-Cut Quality QA

## 判定

- real video silence plan完走: **PASS**
- speech false-positive cut: **0件（測定範囲）**
- filler removal: **未実施**
- default閾値変更: **なし**
- Bの背景ノイズ下pause判定: **HUMAN_JUDGMENT_NEEDED**

## Corpus

NIGHT 05のrepo外QA corpus `spectra-real-japanese-corpus-20260816`を使用した。対象はA（188.6秒、比較的クリアなsingle-narrator）とB（270.022秒、会話型・速度変化）で、動画本体はGitへ追加していない。

## 実測結果

### A

実Whisperは`large-v3 / cuda / float16`で651 raw words、先頭・中盤・後半すべてにword coverageがあった。

| profile | signal silence | signal候補合計 | 保護後removed | raw word overlap |
|---|---:|---:|---:|---:|
| Natural | 3 | 3.414 sec | 3.414 sec | 0 |
| Strong | 3 | 3.654 sec | 3.654 sec | 0 |

主要候補は5.941–6.643秒、19.908–20.563秒、末尾186.003–188.600秒。最初のpauseは波形上も前後発話に挟まれた低振幅区間で、平均音量は前後の約-22.5 / -30.1 dBに対し候補は-59.9 dBだった。Naturalはpause 180msを残して521.5ms、Strongは601.5msを短縮した。

### B

実Whisperは`large-v3 / cuda / float16`で733 raw words、先頭・中盤・後半すべてにword coverageがあった。

既定`noise=-35 dB`ではNatural/Strongともsignal silence 0件、Projectを変更しないno-opになった。8–14秒窓では「ぐらい。」が9.48秒で終わり、「もう」が10.92秒で始まる発話間隔があり、波形も低振幅区間を示すが、候補区間の平均音量は-32.5 dB、前後発話は-25.9 / -21.7 dBで、背景ノイズを含む。

参考として`noise=-20 dB`へ一時的に上げた診断では、Natural 26件 / 17.390秒、Strong 65件 / 27.773秒まで候補が増えた。しかし、実Whisper word protection後のremoved rangeとraw wordのoverlapは0件だった。候補がspeech境界へ近づくため、これを既定設定へ採用しない。

## 設計判断

- 音声信号がsilence authority。Whisper gapだけで無音確定しない。
- raw Whisper wordsはspeech protectionとして使用し、raw source truthを書き換えない。
- false-positive speech cutをfalse-negative silenceより重く扱う。
- Bのpauseを自動短縮するためnoise floorを上げるには、実音声の聞き取りを含む人間判定が必要。現段階ではsafe no-opを維持する。
- Natural/Strongの既定値（600/180ms、400/100ms）は変更しない。

## 再現・検証

- `silence_detection.analyze_silences()`をA/BのNatural/Strongで実行。
- `transcribe_video()`をA/B full rangeで実行し、実Whisper word timingを取得。
- signal-only候補、word protection後removed range、overlapを比較。
- FFmpeg `volumedetect`と`showwavespic`で候補前後の音量・波形を確認。
- 既存silence/worker testsは変更せず、real mediaはQA workspaceのみで使用。
