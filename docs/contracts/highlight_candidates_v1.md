# `highlight_candidates.v1`

候補抽出stageの正本。時間軸はjumpcut後のanalysis timelineであり、source timeやfinal sequence timeではない。

## Envelope

```json
{
  "schema_version": "highlight_candidates.v1",
  "generator": {"name": "clip_automation.planning.candidate_generation", "version": "1"},
  "input_hashes": {"clean_transcript.json": "<sha256>"},
  "status": "normal",
  "diagnostics": [],
  "timeline_duration": 3600.0,
  "chunking": {"max_chars": 4000, "max_duration": 120.0, "overlap_duration": 10.0},
  "chunks": [],
  "candidates": []
}
```

`status`は`normal`、`degraded`、`fallback`、`failed`のいずれか。AI失敗を`normal`へ変換しない。`diagnostics`のseverityは`warning`、`unsupported`、`error`。

## Chunk

各chunkは`chunk_id`、`timeline.start/end`、`text`、`record_count`、`provenance`を持つ。chunkは決定的な順序で並び、先頭0秒から末尾durationまでcoverする。overlapは許可するが、chunk intervalは設定した`max_duration`を超えない。

## Candidate

```json
{
  "candidate_id": "candidate_<stable-hash>",
  "timeline": {"start": 184.2, "end": 207.8},
  "duration": 23.6,
  "chunk_id": "chunk_<stable-hash>",
  "reason": "独立して成立する盛り上がり",
  "provenance": {"status": "normal", "provider": "ollama", "provider_version": "..."}
}
```

candidate IDは入力の正規化した範囲・理由から安定生成する。範囲外、逆転、NaN/Infinityはclampせずstageを`failed`にする。overlap chunkから同一候補が複数出た場合は決定的にdeduplicateする。
