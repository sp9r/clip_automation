# Real Japanese QA Corpus

更新日: 2026-08-16

実人間の日本語発話を使うacceptance用corpus。動画本体はGit管理せず、repo外のQA workspaceへ保存する。詳細なsource URL、作者、license、SHA-256、ffprobe結果は外部manifest `spectra.real-japanese-qa-corpus.v1`に記録する。

## Corpus

| ID | Role | 内容 | 選択duration | License |
|---|---|---|---:|---|
| `real-ja-a-job-interview` | A | 比較的クリアな日本語解説・single-narrator style | 188.600 sec | CC BY-SA 4.0 |
| `real-ja-b-horiguchi-interview` | B | 会話型インタビュー。速度変化・短いpauseを含む区間 | 270.022 sec | CC BY 3.0 |
| `real-ja-c-mcdc-onboarding` | C | 長時間の日本語オンラインQ&A。multi-speaker / meeting条件 | 1389.928 sec | CC BY-SA 4.0 |

## Provenance

- A: [Wikimedia Commons — Job Interview](https://commons.wikimedia.org/wiki/File:Job_Interview.webm), author `Simpleshow Japan`.
- B: [Wikimedia Commons — Kyoji Horiguchi Rizin Media Day Interview](https://commons.wikimedia.org/wiki/File:Kyoji_Horiguchi_Rizin_Media_Day_Interview_-_MMA_PLUS_-_Gazeta_Esportiva.webm), author `Bruno Massami`. Originalから先頭270秒を切り出したderived QA file。
- C: [Wikimedia Commons — MCDC Onboarding Japanese](https://commons.wikimedia.org/wiki/File:MCDC_Onboarding_%22Ask_me_Anything%22_session_-_Japanese_language.webm), author `TCappelletto (WMF)`.

## Validation

- 3本ともffprobeでvideo/audio streamとpositive durationを確認。
- ダウンロード後SHA-256をmanifestと照合。
- A/B/CはTTS、sine wave、数秒fixtureではない。
- 個人folder、random copyrighted YouTube rip、repoへの大容量media追加は行っていない。
- final E2Eの最終判定前に、実音声の聞き取り、Whisper coverage、silence/caption品質を各corpusで別途確認する。corpus準備完了はacceptance PASSを意味しない。

## Usage policy

今後のreal-video acceptanceでは、少なくともA/B/Cのいずれかを使い、TTS/generated mediaだけのPASSを`REAL VIDEO PASS`へ昇格させない。派生ファイルを作る場合は、元source ID、range、変換、SHA-256をmanifestへ追記する。
