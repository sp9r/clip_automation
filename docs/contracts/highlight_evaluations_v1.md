# `highlight_evaluations.v1`

候補生成とは独立した再評価stageの正本。候補IDを入力との照合キーにし、total scoreはproviderに決めさせずコードで計算する。

## Envelope

```json
{
  "schema_version": "highlight_evaluations.v1",
  "generator": {"name": "clip_automation.planning.candidate_evaluation", "version": "1"},
  "input_hashes": {"highlight_candidates.v1.json": "<sha256>"},
  "status": "normal",
  "diagnostics": [],
  "candidate_ids": [],
  "weights": {
    "hook": 0.20,
    "standalone": 0.20,
    "interest": 0.20,
    "coherence": 0.15,
    "novelty": 0.10,
    "confidence": 0.15
  },
  "evaluations": []
}
```

各scoreは0〜100の有限値。totalは次で計算する。

```text
total_score = Σ(score[axis] × weight[axis]) / Σ(weight)
```

evaluationの`candidate_id`が入力candidateと一致しない、scoreが範囲外、NaN/Infinity、provider failureの場合は`failed`とし、正常評価成功として扱わない。

## Evaluation

各entryは`candidate_id`、6軸の`scores`、`total_score`、`reason`、provider name/versionを持つ。promptへ渡すtranscriptはuntrusted dataとして区切り、transcript内命令をsystem instructionとして扱わない。
