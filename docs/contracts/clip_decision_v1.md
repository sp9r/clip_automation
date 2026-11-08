# `clip_decision.v1`

評価済み候補から、実際に独立出力する候補を決定するartifact。LLMへ最終採否を丸投げせず、決定はコードの設定と評価値から行う。

## Envelope

```json
{
  "schema_version": "clip_decision.v1",
  "generator": {"name": "clip_automation.planning.clip_decision", "version": "1"},
  "input_hashes": {"highlight_evaluations.v1.json": "<sha256>"},
  "status": "normal",
  "diagnostics": [],
  "decision_status": "selected",
  "selected_candidate_ids": [],
  "rejected_candidate_ids": [],
  "rejection_reasons": {},
  "ordering": [],
  "settings": {
    "minimum_score": 70.0,
    "max_selected_clips": 3,
    "min_duration": 5.0,
    "max_duration": 60.0,
    "overlap_suppression": 0.50,
    "near_duplicate_suppression": 0.80
  },
  "decision_version": "1"
}
```

`decision_status`は少なくとも`selected`、`no_candidates`、`no_candidates_above_threshold`、`no_candidates_after_constraints`を区別する。同一candidate/evaluation/settingsでは、score降順、timeline start/end、candidate IDの安定順で同じ結果になる。

重複抑制はIoUと短い方の区間に対する重複率を使う。最大採用数、score threshold、clip長制約、overlap/near-duplicate抑制の理由を`rejection_reasons`へ保存する。
