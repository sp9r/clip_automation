# Caption Quality Plan（Step 1 / Step 2）

## 目的と範囲

この文書は、横画面の自動字幕品質を測定可能にし、次の改善を比較できる状態を定義する。対象は `clip_automation` の字幕生成・成果物・設定伝播であり、`video_editor` のrendererや字幕アルゴリズムを今回書き換えるものではない。

Step 1 の優先方向は 16:9 / 1920x1080 である。Shorts向けの縦画面最適化、全文字起こしの再解析、LLMプロンプトの全面改修、phrase-specificな新しい例外規則は対象外とする。

水平字幕の目標は、fixtureで定義した許容境界の hit rate 80% 以上を一つの目安にする。ただし、hit rate 単独で合格にせず、原文完全一致、語順、時刻、文字数、保護境界を同時に確認する。80%は品質目標であり、既存の実データを改変して達成するための強制閾値ではない。

## 現在の意味と責務

### `max_chars`

既存の `max_chars` は、字幕ブロック全体の文字数上限である。1行のピクセル幅やrendererの折返し幅ではない。現行の `caption.py` では次の用途に使われる。

- heuristic boundaryのコスト・上限
- LLMへ渡す字幕ブロック長の指示と応答検証
- LLM失敗時のfallback
- 既存後処理で隣接字幕を結合できるかの判定

したがって、今回 `max_chars` の名称や意味を「1行文字数」へ変更しない。既存値も維持する。

| 画面プリセット | 既存 `max_chars` | Step 1の既定 `max_lines` |
|---|---:|---:|
| FHD landscape 1920x1080 | 32 | 2 |
| FHD portrait 1080x1920 | 20 | 2 |
| HD landscape 1280x720 | 32 | 2 |

`max_chars` は 8〜80、`max_lines` は 1〜16 の整数を受け付ける。boolは整数として扱わない。0以下、範囲外、極端な値は設定段階で拒否する。

### `max_lines`

`max_lines` は字幕成果物に保存する表示行数制約である。Step 1 の `caption.py` は字幕ブロックの意味境界と時刻を生成するだけで、表示行を改めて分割しない。

現時点の責務は次のとおり。

```text
user/project settings
  → job_manifest.settings.max_lines
  → caption generation metadata
  → caption_project.json.caption_settings
  → caption_project.v2.json.caption_settings
  → downstream renderer-owned layout constraint
```

最終的な視覚行数は ASS/libass または `video_editor` rendererの責務である。`clip_automation` と `video_editor` が同じ折返し処理を二重に持つこと、生成時に改行を追加して見かけ上の `max_lines` を満たすことは禁止する。現状では「renderer-owned / deferred」と成果物へ明示する。

## 変更した基盤

- `caption_settings.py`: `max_chars` / `max_lines` の共通検証と既定値。
- `job_manager.py`: `max_lines` を設定、request key、stage invalidation、pipeline呼出しへ追加。caption stage変更として扱う。
- `project_settings.py` / `ui/app.py`: project preset、開始、再実行、字幕編集後の再render、API返却値へ追加。
- `caption.py`: 生成APIへ `max_lines` を受け付け、caption metadataへ保存。字幕本文の折返し処理は追加していない。
- `subtitle_project.py` / `timeline_preparation.py`: caption v1互換成果物と caption v2成果物へ同じ設定を保存。

既存runの `max_lines` 欠落は、読み込み時に既定値2として互換扱いする。既存run、動画、transcript、字幕本文を上書きして移行しない。

## 評価fixture

`tests/fixtures/caption_quality_horizontal_v1.json` を横画面評価用の正本とする。

- 1920x1080 / 30fps
- 30 utterances
- 133 Whisper-like words
- 日本語、英語混在、数値、長い語、renderer用語を含む
- 全wordにstart/end timestamp
- utteranceごとの許容境界と保護境界を記録

fixtureは実runではなく評価用copyである。テストから本番runやユーザー動画を変更しない。

## 機械的メトリクス

`caption_quality.py` の `evaluate_caption_quality()` で次を測定する。

### Hard constraints

- `max_chars` 違反数
- `max_lines` 違反数（現在は明示改行の機械チェック。表示行適用はrenderer-owned）
- 空字幕数
- 原文の omission / duplication
- 字幕の語順・連結順序
- captionとsource wordの開始・終了時刻一致

### Boundary quality

- `actual_boundary_count`: 実際に生成した境界数
- `acceptable_boundary_count`: 許容集合と一致した実境界数
- `boundary_precision`: `acceptable_boundary_count / actual_boundary_count`
- `allowed_boundary_recall`: 許容集合のうち実際に使った割合（記述値）
- 互換用の `allowed_boundary_hit_rate` は従来どおり保持するが、意味は precision-like な「実境界のうち許容された割合」である
- 許容集合外の bad boundary 数
- particle、補助動詞、数値、英単語などの保護境界違反数

許容境界は「使ってよい代替位置」の集合であり、すべてを使う必要はない。そのため recall は合否目標にしない。boundary precision、bad/protected violation、文字数分布、原文fidelityを併記する。

### 長さ・時間分布

平均・中央値・最小・最大文字数、平均字幕duration、短すぎる字幕数を記録する。boundary precisionだけを上げるために、字幕を長く結合することを合格とはしない。`max_chars=32`の違反ゼロと、分布のbefore/afterを同時に確認する。

### Fidelity

- `exact_source_fidelity`
- `order_preserved`
- `timestamp_preserved`

行数がrendererに委譲されている間は、line-countの視覚的PASSをこのfixtureだけで宣言しない。最終rendererのgeometry評価はrenderer側のcontract compliance testで追加する。

## Step 1 baseline

baselineは、現行のheuristic caption generator、`max_chars=32`、`max_lines=2`で生成した比較値である。結果は `tests/fixtures/caption_quality_horizontal_baseline.v1.json` に保存する。

| 指標 | baseline |
|---|---:|
| caption count | 43 |
| source word count | 133 |
| empty caption | 0 |
| max_chars violation | 0 |
| explicit max_lines violation | 0 |
| omitted / duplicated character | 0 / 0 |
| order / exact fidelity / timestamp | true / true / true |
| allowed boundary hit rate | 78.57% |
| bad boundary | 9 |
| protected boundary violation | 9 |
| max_lines application | renderer-owned |

Step 1 baselineの詳細は `tests/fixtures/caption_quality_horizontal_baseline.v1.json` に保存する。

## Step 2: Generalized Japanese Boundary Quality

### 目的と制約

Step 2では、既存のphrase-specific保護表へ新しいfixture固有文字列を追加せず、次の軽量token classと構造ヒントで境界を保護する。

- Japanese / Latin / numeric / punctuation / mixed
- 助詞suffix、prenominal-like token、verb/auxiliary-like token
- Latin token + Japanese particle
- `の` modifier + 次のnoun-like token
- predicate/auxiliary sequence、numeric unit

形態素解析器やLLMを追加しない。既存DPの完全経路を維持し、強く保護する候補でも、結合後に`max_chars`またはdurationを超える場合は無理に結合しない。これは `forced_due_to_max_chars` 相当のfallbackであり、原文fidelityと時刻を優先する。

### Boundary policy / diagnostics

`caption_boundary_v2.py` の `boundary_diagnostic()` は、各候補について次を返す。

- boundary index
- pause contribution
- base score
- syntax penalty
- protected penalty
- preferred bonus
- `neutral` / `soft_discouraged` / `preferred` / `hard_forbidden` category
- token class と次token class

既存DPのselection scoreは互換性のため維持し、一般則によるhard protectionは後段の候補修復で適用する。修復は隣接blockの結合が制約内の場合だけ行うため、hard dead endで空経路にならない。pauseは候補の一要素であり、pauseだけをbad boundaryと判定しない。

### Holdout fixture

`tests/fixtures/caption_quality_horizontal_holdout_v1.json` を、開発fixtureと異なる16 utterances / 61 wordsのholdout正本として追加した。daily、technical、proper noun、number、mixed、English、conversation、pause、self-correction、relative clause、auxiliary、long sentenceを含む。許容・保護境界はアルゴリズム調整前にfreezeし、チューニング中に書き換えていない。

Step 2前のholdout値は `caption_quality_horizontal_holdout_baseline.v1.json` に保存した。

### Step 2 before / after

| 指標 | 開発 Step 1 | 開発 Step 2 | holdout Step 1 | holdout Step 2 |
|---|---:|---:|---:|---:|
| caption count | 43 | 33 | 19 | 18 |
| average chars | 14.26 | 18.58 | 14.79 | 15.61 |
| median chars | 14.0 | 17.0 | 15.0 | 15.5 |
| max chars | 31 | 32 | 19 | 21 |
| boundary precision | 78.57% | 100.00% | 94.44% | 100.00% |
| bad boundary | 9 | 0 | 1 | 0 |
| protected violation | 9 | 0 | 1 | 0 |
| exact fidelity / timestamp | true / true | true / true | true / true | true / true |

Step 2はprecisionを改善した一方、保護結合により字幕数が減り平均文字数が増えた。この分布を隠さず結果fixtureへ保存した。`max_chars`違反、短字幕、omission、duplication、timestamp mismatchは発生していない。次段ではこの分布を悪化させずに意味単位を増やすことが課題である。

詳細なStep 2結果は `tests/fixtures/caption_quality_horizontal_step2_result.v1.json` と `tests/fixtures/caption_quality_horizontal_holdout_step2_result.v1.json` に保存する。

### Step 2で残る失敗

開発fixtureとholdoutの最終評価では、許容集合外・保護境界違反は0件だった。したがって「失敗ゼロ」はこの2つの評価copyに対する結果であり、実Whisper tokenization全般の保証ではない。実データでは、形態素解析なしのsuffix heuristicが長い字幕を作る可能性と、`max_lines`の視覚適用がrenderer-ownedである点が残る。

## Step 3: Real Whisper Evaluation + Human Review Dataset

### 方針

Step 2のsynthetic development/holdout 100%はproduction品質100%を意味しない。Step 3では、既存runの`transcript.json` / `clean_transcript.json`をread-onlyで読み、現在の`use_llm=False` heuristic Step 2を評価する。production boundary algorithm、LLM prompt/default、renderer、`video_editor`は変更しない。

実発話本文を含むartifactは `data/evaluations/` 配下だけに保存し、`.gitignore`でGit外へ隔離する。tracked側には評価tool、schema、tests、匿名化された集計結果だけを置く。source pathや実動画名はartifact schemaへ保存しない。

### Human review schema / tool

`docs/contracts/caption_review_v1.md` と `caption_real_evaluation.py` を正本とする。artifactの`human_review`は次を分離する。

- `rating`: `good` / `acceptable` / `bad`
- `bad_boundaries` / `preferred_boundaries` / `reasons`
- `segmentation_bad`
- `transcription_bad`
- `exclude_from_boundary_evaluation`
- heuristic / LLM / tie等の人間選択

文字起こし自体の誤りと字幕境界の誤りを混同しない。LLM候補はgolden labelではなく、比較候補としてのみ扱う。人手ratingは自動入力しない。

reviewはCLIでpending順に再開できる。

```powershell
.venv\Scripts\python.exe caption_real_evaluation.py review data\evaluations\caption_quality_real_20260814\caption_review.v1.json
```

### 初回real corpus結果

2026-08-14時点で、重複source hashを除いた5 runから100 utterancesをstratified samplingし、実Whisper token 1,325件を評価artifactへ保存した。全100件が`pending`であり、human-ratedは0件である。評価前後のsource artifact SHA-256は5/5 runで一致し、source mutationは0件だった。

自動pre-analysisはnaturalness scoreとは呼ばない。

| 指標 | real Step 3 |
|---|---:|
| sampled runs / utterances / tokens | 5 / 100 / 1,325 |
| generation error | 1 |
| average / median chars | 10.52 / 12.0 |
| P75 / P90 / max chars | 14.0 / 16.0 / 30 |
| exact source fidelity | 74 / 100 |
| timestamp preserved | 99 / 100 |
| generic protected boundary | 0 |
| forced constraint boundary | 5 |
| human-rated / pending | 0 / 100 |

length bucketsは`8-12: 52`, `13-16: 59`, `17-20: 11`, `21-24: 1`, `25-28: 0`, `29-32: 1`。duration bucketsは`under_1s: 52`, `1-2s: 89`, `2-3s: 20`, `3-4s: 7`, `4-5s: 2`, `over_5s: 1`である。bucketはcaption単位のため合計はsample utterance数とは一致しない。

generation error 1件は、実runの長いsegmentで現行heuristicが`max_chars=32`の完全なbreak経路を作れなかったもの。sourceを変更せず、error sampleとしてhuman review artifactへ残した。fidelity 74/100は、実token列での字幕連結とproduction後処理の差分を含むため、Step 2 synthetic結果と同一視しない。

LLM-assisted comparisonは今回未実行。prompt、model、production defaultを変更していない。`max_lines=2`は引き続きrenderer-ownedで、visual enforcementは未検証である。

次の操作は、まず上記review commandを実行し、`real_0001`からratingとboundary annotationを人間入力すること。人手評価完了前に自然さの合否やproduction algorithm変更を行わない。

代表的な9件は baseline JSON に word index、出力文字列、分類を含める。現在のfixtureから機械的に再現できるものだけを記録し、未計測の失敗を追加で捏造しない。

## Step 3.1: Real Fidelity / Generation Failure Diagnosis

Step 3の100sampleをread-onlyで再診断し、元の`caption_review.v1.json`を変更せず、ignoredな`caption_diagnostics.v1.json`へsample別diffと原因を保存した。診断schemaは `docs/contracts/caption_diagnostics_v1.md` と `caption_fidelity_diagnostics.py` を正本とする。実発話本文をtracked docsへコピーせず、ここでは集計値だけを記録する。production字幕アルゴリズム、LLM、renderer、human ratingは変更していない。

### Fidelity aggregate

| category | count |
|---|---:|
| exact (raw) | 74 |
| punctuation-only | 25 |
| whitespace-only | 0 |
| unicode-normalization-only | 0 |
| omission | 0 |
| duplication | 0 |
| replacement | 0 |
| reordering | 0 |
| mixed changes | 0 |
| generation error | 1 |
| total | 100 |

したがってraw exactは74/100、raw mismatchは26/100であり、その内訳は句読点差25件とgeneration error 1件である。句読点・空白を診断上除いたnormalized fidelityは99/100だが、raw exactを緩和した合格値として扱わない。差分spanの集計は omitted 94文字、duplicated 0文字、replaced 0文字である。omitted 94は句読点のみのdelete spanを含む診断定義であり、本文の意味文字が94文字失われたという意味ではない。

### Earliest divergence / timestamp / generation failure

- earliest divergenceは、正常sample 74件が差分なし、句読点差25件が`F_final_output`、generation error 1件が`B_boundary_generation`だった。A word flatten、C caption text construction、D cleanup、E forced mergeで新たな意味文字差分を発生させたとは判定されなかった。
- timestamp mismatchは1件のみで、生成errorによりgenerated caption timestampsが空になったことが原因である。source first/last word timestamp、generated timestamp列、boundary ranges、text fidelityはignored diagnostic artifactへ保存した。
- generation error 1件は、単一tokenが32文字超過でも5秒超過でもない。evaluation-only DPでは`max_chars=32` / `max_duration=5.0`を満たすpartitionが存在した。一方、production optimizerはbreak index 52だけを返し、66文字の単一captionとなってvalidationで失敗した。よって`algorithm_failed_despite_feasible`であり、`constraint_impossible`ではない。timestampがほぼ同一に潰れたword列の影響を含むoptimizer pathの診断候補として、Step 3.1時点の未修正状態を記録した。

### Overnight run: feasible path fallback correction

合成した66個の1文字wordを0.001秒間隔へ圧縮することで、上記の状態を実データなしで再現した。`feasible_partition()`は`max_chars=32` / `max_duration=5.0`を満たす経路を返す一方、production `optimize_boundaries()`は各候補のdurationが`min_duration=0.35`未満だったため遷移をhard pruneし、終端到達不能時に`[count - 1]`を返していた。このfallbackは最大文字数違反を含むため不正である。

修正では、`min_duration`を品質上のsoft penaltyへ変更し、hard constraintを`max_chars`と`max_duration`へ限定した。これにより圧縮timestampでも完全経路を選択できる。hard constraint自体を満たす経路がない場合は、oversized tokenまたはduration制約を示す`ValueError`でfail closedし、不正な単一字幕を公開しない。合成回帰では66文字が3字幕（26/27/13文字）へ分割され、原文fidelityを維持した。既存の日本語development/holdout gate、point/zero-duration関連テスト、全体回帰を通過した。

### Review eligibility and next review subset

- `strict_review_eligible`: 74件（generation成功、raw fidelity一致、timestamp valid）
- `normalized_review_eligible`: 99件（diagnostic normalized fidelity、timestamp valid）
- content-mutating subset: 0件
- generation-error subset: 1件

字幕の区切り自然さを人手で確認する最初の候補は、元artifactを削除せず、strict clean 74件から開始する。句読点のみ25件はraw/normalized方針を決定していないため別subsetとして扱い、generation error 1件は自然さreviewの母集団へ混ぜない。既存review CLIのdefault挙動と`human_review` statusは維持する。

## Step 3.2: Strict-Clean Human Boundary Review

Step 3.1のdiagnostic artifactを使い、`strict_review_eligible=74`のsampleだけを人手review対象にする。punctuation-only 25件とgeneration-error 1件は表示対象から除外するが、元の100sample artifactから削除せず、既存`human_review`も変更しない。今回の作業ではratingを入力せず、production字幕アルゴリズム、optimizer、LLM、renderer、`video_editor`を変更しない。

review CLIは次で起動する。`g/a/b/s/q`の1文字入力（コンソールではEnter確定）に対応し、`bad`の場合だけboundary indexとreasonを追加で聞く。tokens、current captions、boundary index、caption start/end、文字数、duration、pause、category、generic protected、forced constraintを表示する。strict対象のpendingだけを順に処理し、quit後は同じcommandで次のpendingから再開する。

```powershell
.venv\Scripts\python.exe caption_real_evaluation.py review data\evaluations\caption_quality_real_20260814\caption_review.v1.json --diagnostics data\evaluations\caption_quality_real_20260814\caption_diagnostics.v1.json --filter strict-clean
```

review結果はignored `caption_human_review_summary.v1.json`へ保存する。summaryの正本項目はstrict clean total、reviewed、pending、good、acceptable、bad、`(good + acceptable) / reviewed`、bad boundary count、bad reason、length bucket × rating、duration bucket × ratingである。length bucketは`<=12`、`13-16`、`17-20`、`21-24`、`25-28`、`29-32`、duration bucketは`<1s`、`1-2s`、`2-3s`、`3-4s`、`4-5s`を使う。threshold変更やStep 4移行の判断はこのreview完了後に別途行う。

ユーザー向け表示は日本語化し、評価キーは`A/B/C/D/0`へ整理した。内部artifact/schemaの英語値は維持し、旧`g/a/b/s/q`入力も後方互換で受け付ける。`segmentation_bad`などの内部field名を画面へ直接表示せず、bad時の追加質問も日本語で表示する。

## Step 1で解決していない問題

- `max_lines=2` の視覚結果は、現時点では `clip_automation` のSRT本文だけでは保証しない。
- ASS/libassでの日本語・英単語の最終折返し位置は、renderer側の実測が必要。
- LLM経路とheuristic経路の意味境界品質は同一fixtureで別々に測定する必要がある。
- 既存 `caption.py` には過去に追加された phrase-specific な後処理規則が残る。Step 1では削除・追加・拡張せず、次Stepの比較対象として扱う。
- 句読点除去後のsource fidelityをどう定義するかは、実Whisperデータでの方針決定が必要。
- font、pixel幅、ASS/libassの視覚line countは `video_editor` のrenderer acceptanceで評価する。

## 次の候補（Step 4以降）

Step 2以降は、baselineとの差分を先に確認してから、次の候補を小さく比較する。

1. 単語・形態素境界を使った保護境界の一般化。特定フレーズの追加表ではなく、語・助詞・補助動詞・数値の構造を対象にする。
2. 日本語・英語混在と長い英単語の boundary policy。
3. LLM提案境界の検証と、許容境界へ戻す失敗診断。
4. renderer-owned `max_lines` の実装責務を `video_editor` contractと合意し、同一fixtureで視覚line-countを測る。
5. 実データから匿名化した評価copyを追加し、fixture overfitを避ける。

Step 4以降で、human review結果に基づく改善候補、LLM-assisted比較、renderer-owned `max_lines` の視覚評価を検討する。phrase-specificな新しい例外規則、字幕本文の書換え、rendererの二重実装は引き続き行わない。
