# caption_review.v1

`caption_review.v1` は、実際のfaster-whisper tokenizationを対象にした字幕評価と人手レビューのためのローカル専用artifactである。実発話本文、実run由来の字幕、HTMLはGitへ追加しない。

## 保存場所とprivacy

- 保存先: `data/evaluations/caption_quality_real_<timestamp>/caption_review.v1.json`
- `data/evaluations/` は`.gitignore`対象
- artifactにはsource pathを保存せず、匿名`run_index`、segment index、SHA-256 fingerprintだけを保存する
- `source_words` / `source_text`は人手レビュー用にartifact内へ保存するが、tracked fixtureやdocsへコピーしない
- source `transcript.json` / `clean_transcript.json` はread-onlyで開き、評価前後のSHA-256一致を確認する

## Top-level

```json
{
  "schema_version": 1,
  "artifact_type": "caption_review.v1",
  "corpus_id": "caption_quality_real_step3",
  "orientation": "horizontal",
  "settings": {
    "max_chars": 32,
    "max_lines": 2,
    "max_lines_application": "renderer_owned",
    "visual_max_lines_verified": false,
    "primary_algorithm": "heuristic_step2",
    "llm_comparison": "not_run"
  },
  "source_policy": {},
  "source_integrity": [],
  "sampled_run_count": 0,
  "sampled_utterance_count": 0,
  "samples": [],
  "aggregate": {}
}
```

## Sample

各sampleは次を持つ。

- `sample_id`: `real_0001`のような匿名ID
- `source_ref`: run index、segment index、clean artifact SHA-256
- `categories`: stratified sampling用の特徴
- `source_words`: `word` / `start` / `end`
- `source_text`: source wordsの連結
- `step2.captions`: 現行heuristicの字幕と時刻
- `step2.boundaries`: boundary index、pause、`boundary_diagnostic()`、constraint forced flag
- `llm_assisted`: optional comparison。未実行時は`available=false`, `status=not_run`
- `metrics`: fidelity、caption length/duration、bucket、generic protected count等
- `human_review`: 人間だけが入力する正本

## Human review

```json
{
  "status": "pending",
  "rating": null,
  "segmentation_bad": false,
  "transcription_bad": false,
  "exclude_from_boundary_evaluation": false,
  "bad_boundaries": [],
  "preferred_boundaries": [],
  "reasons": [],
  "preferred_system": null,
  "notes": ""
}
```

`rating`の意味は固定する。

- `good`: そのまま投稿可能
- `acceptable`: 違和感はあるが手修正不要
- `bad`: 人間なら修正する

`segmentation_bad`と`transcription_bad`は別フラグである。文字起こし自体が悪いsampleをboundary algorithmの評価から除外する場合は、`exclude_from_boundary_evaluation=true`とし、除外数をaggregateで報告する。LLM候補はhuman goldenではなく比較候補であり、`preferred_system`は人間が入力する。

`reasons`は次の選択肢から複数指定できる。

`particle`, `auxiliary/predicate`, `modifier`, `compound word`, `English/Japanese`, `numeric/unit`, `semantic chunk`, `too long`, `too short`, `timing/pause`, `transcription error`, `other`

## Review command

```powershell
.venv\Scripts\python.exe caption_real_evaluation.py review data\evaluations\caption_quality_real_<timestamp>\caption_review.v1.json
```

CLIは最初の`pending` sampleから表示し、保存後もpending順で再開する。`quit`しても未レビューsampleはpendingのまま残るため、次回同じコマンドで続きから再開できる。

## Strict-clean human review

`caption_diagnostics.v1`の`strict_review_eligible`を使うevaluation-only filterを利用できる。

```powershell
.venv\Scripts\python.exe caption_real_evaluation.py review data\evaluations\caption_quality_real_20260814\caption_review.v1.json --diagnostics data\evaluations\caption_quality_real_20260814\caption_diagnostics.v1.json --filter strict-clean
```

strict-cleanはgeneration成功、raw source fidelity exact、timestamp validのsampleだけを表示する。punctuation-only、generation-error、その他の除外sampleはartifactから削除せず、既存の`human_review`も変更しない。途中終了後は同じcommandでstrict対象の次のpendingから再開する。

操作は画面表示の`A/B/C/D/0`で、コンソールでは入力後Enterする。`C`の場合だけ区切り番号、理由等を追加入力する。ratingは人間だけが入力する。

画面表示は日本語で行う。表示上の評価は`A=このままで良い`、`B=少し違和感はあるが修正不要`、`C=修正したい`、`D=今回は判断を飛ばす`、`0=中断して保存`とし、保存時は従来の`good` / `acceptable` / `bad` / `pending` schema値へ変換する。旧CLIの`g/a/b/s/q`入力も互換で受け付ける。`segmentation_bad`等の内部field名は画面へ直接表示しない。

review後のstrict対象集計は、同じdirectoryのignored `caption_human_review_summary.v1.json`へ保存する。summaryにはreviewed/pending、good/acceptable/bad、automatic acceptance rate、bad boundary/reason、caption length bucket × rating、duration bucket × ratingを含める。caption lengthはsample内の最大caption長、durationはsample内の最大caption durationで分類する。

## 禁止事項

- LLMにgolden boundaryを自動生成させない
- Luna/自動処理がhuman ratingを入力しない
- 実発話本文を`tests/fixtures`、docs、tracked JSONへコピーしない
- `max_lines`をvisual verifiedと表示しない
