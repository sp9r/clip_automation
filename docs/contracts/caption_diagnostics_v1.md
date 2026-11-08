# caption_diagnostics.v1

`caption_diagnostics.v1` は、既存の `caption_review.v1` を変更せずに、実字幕の文字忠実度・時刻差分・生成失敗を調査する評価専用artifactである。実発話本文とdiff本文は `data/evaluations/` 配下のignored JSONにだけ保存し、tracked docsには集計値だけを書く。

## 入力と不変条件

- 入力は `caption_review.v1` のread-only copy。
- `source_artifact_sha256` で入力artifactのhashを記録する。
- `human_review.status`、rating、boundary annotationは変更しない。
- 本診断は `caption.py` のdefault runtime、production boundary algorithm、LLM、rendererを変更しない。

## Top-level

```json
{
  "schema_version": 1,
  "artifact_type": "caption_diagnostics.v1",
  "source_artifact_sha256": "...",
  "samples": [],
  "aggregate": {}
}
```

## Fidelity categories

各sampleは、source wordを連結したraw sourceと、生成字幕を連結したraw finalを比較する。

- `exact`: raw文字列が完全一致。
- `punctuation_only`: rawでは不一致だが、句読点と空白を除くと一致。
- `whitespace_only`: 空白だけが異なる。
- `unicode_normalization_only`: NFCまたはNFKCで一致する。
- `omission` / `duplication` / `replacement` / `reordering` / `mixed_changes`: `difflib` opcodeに基づく文字差分。
- `generation_error`: 字幕生成が例外で完了しなかったsample。

`raw_exact` と `normalized_exact` は別々に保存する。句読点を除去してraw exactへ昇格させない。replacementのcharacter countは置換されたsource/final spanのうち短い方、omitted/duplicatedはdelete/insert spanの長さとして集計するため、同じreplace spanが両方のspan合計とreplaced合計に現れ得る。この定義はdiagnostic専用であり、production acceptance policyではない。

## Diff fields

不一致sampleには少なくとも次を保存する。

- `sample_id`
- source / final length
- first differing position
- deleted spans
- inserted spans
- replaced spans

本文を含むこれらのspanはignored artifact内だけに置く。

## Earliest divergence

source concatenationが初めて変化した段階を次の固定名で記録する。

`A_word_flatten` → `B_boundary_generation` → `C_caption_text_construction` → `D_cleanup` → `E_forced_merge_topic_rebuild` → `F_final_output`

boundary segmentationによる分割はsource文字列を変えないため、cleanupやfinal outputでの句読点除去などのnormalizationとは別に報告する。

## Generation failure feasibility

evaluation-only DP helperで `max_chars=32`、`max_duration=5.0` を満たすword-aligned partitionの存在を判定する。

- `constraint_impossible`: 単一token上限超過など、理論上partitionを作れない。
- `algorithm_failed_despite_feasible`: partitionは存在するが、production optimizer結果がvalidationで失敗した。

この分類は修正を意味しない。失敗の具体的なbreak indices、単一token上限、production validation結果、推定root causeをdiagnosticへ保存する。

## Review eligibility

既存human reviewとは別に、次を保存する。

- `strict_review_eligible`: generation成功、raw fidelity一致、timestamp alignment valid。
- `normalized_review_eligible`: generation成功、diagnostic normalized fidelity一致、timestamp alignment valid。

normalized eligibilityは今回のdiagnostic用の候補集合であり、句読点の正本やproduction acceptanceを決定しない。
