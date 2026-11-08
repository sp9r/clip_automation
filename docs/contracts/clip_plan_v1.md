# `clip_plan.v1`

1つの採用candidateから1本の独立動画を作るためのAI編集判断の正本。`edit_project.v1`より上位の計画であり、複数candidateを1本へ連結する総集編モデルではない。

## Envelope

```json
{
  "schema_version": "clip_plan.v1",
  "generator": {"name": "clip_automation.planning.clip_planning", "version": "1"},
  "input_hashes": {"clip_decision.v1.json": "<sha256>"},
  "status": "normal",
  "diagnostics": [],
  "clip_id": "clip_000001",
  "candidate_id": "candidate_<stable-hash>",
  "candidate_timeline": {"start": 184.2, "end": 207.8},
  "timeline_duration": 3600.0,
  "regions": [],
  "caption_policy": {"mode": "preserve_word_boundaries", "unsafe_split": "unsupported"},
  "target_output": {"width": 1080, "height": 1920, "fps": 30},
  "output_preset": {},
  "source": {"candidate_id": "...", "chunk_id": "..."}
}
```

`regions`はanalysis timeline全体をkeep/deleteで隙間なくcoverする。1つのkeep区間がEDL境界をまたいでも、Phase 1 mappingによって複数source clipを使うことができる。字幕はword境界で処理し、character比例推定をしない。

各planは`planning/clips/<clip_id>/`へ保存し、同じdirectoryに対応する`edit_project.json`を1つだけ置く。3候補採用なら3つのplanと3つのprojectを生成する。
