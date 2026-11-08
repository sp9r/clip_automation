# Phase 6: production pipeline cutover

## Scope

Phase 6は新規production jobの正規経路を、旧単一動画renderから次へ切り替える。

```text
source
  → acquire
  → transcribe
  → jumpcut
  → captions
  → planning
  → edit_project.v1 × N
  → external video_editor CLI × N
  → independent rendered.mp4 × N
```

Phase 7の投稿、YouTube/TikTok/Instagram、OAuth、thumbnail、旧editor削除は対象外である。

## Pipeline version policy

新規`JobManager.create_job()`は`pipeline_version="v2"`が既定値である。v2のstageは次の6つ。

| stage | weight | 正本 / 完了条件 |
|---|---:|---|
| acquire | 0.10 | source media |
| transcribe | 0.25 | `transcript.json` |
| jumpcut | 0.05 | `edl.json`, `clean_transcript.json`, `silence_analysis.json` |
| captions | 0.10 | `captions.srt`, `caption_project.json`, `caption_project.v2.json`, `transcript_words.json`, `migration_report.json` |
| planning | 0.15 | planning manifest、candidate/evaluation/decision、全clip plan/project |
| render | 0.35 | `render_manifest.v1.json`とclip出力 |

旧stageは`acquire → transcribe → jumpcut → captions → render → highlight`のまま保持する。旧manifestに`pipeline_version`がない場合、既存runは`legacy`として扱う。schema 3への変更と`pipeline_version=legacy`の補完はatomicなsafe writeで行うが、run内の成果物は削除・変換しない。既存runを自動v2化しない。

## Manifest hierarchy

同名でも役割が異なるmanifestを混同しない。

```text
run_dir/job_manifest.json                 # JobManager runtime/status, schema 3
run_dir/planning/job_manifest.json        # planning_manifest.v1
run_dir/planning/render_manifest.v1.json  # render execution
```

run root manifestには`pipeline_version`、stage status/progress、settings、runtime resultを保存する。planning manifestにはcandidateから`edit_project.v1`までを保存する。render manifestにはclipごとのrender state/hash/log/error/provenanceを保存する。

## v2 artifacts and result shape

```text
run_dir/
  transcript.json
  clean_transcript.json
  edl.json
  caption_project.json
  caption_project.v2.json
  transcript_words.json
  migration_report.json
  captions.srt
  captions.ass                 # compatibility artifact; not final authority
  planning/
    job_manifest.json
    highlight_candidates.v1.json
    highlight_evaluations.v1.json
    clip_decision.v1.json
    clips/<clip_id>/clip_plan.v1.json
    clips/<clip_id>/edit_project.json
    clips/<clip_id>/automation/rendered.mp4
    render_manifest.v1.json
```

v2 resultは単一`final_video_path`を持たず、次のplural shapeを返す。

```json
{
  "pipeline_version": "v2",
  "status": "completed",
  "run_dir": "...",
  "planning_dir": "...",
  "planning_manifest_path": "...",
  "render_manifest_path": "...",
  "clips": [
    {
      "clip_id": "clip_000001",
      "state": "completed",
      "edit_project_path": ".../edit_project.json",
      "rendered_video_path": ".../automation/rendered.mp4",
      "output_sha256": "..."
    }
  ],
  "completed_outputs": [".../rendered.mp4"],
  "failed_outputs": []
}
```

## Source probe and settings

`probe_source_media()`はffprobeでduration、width、height、fps、codecを取得し、SHA-256は1 MiB chunkのstreamingで計算する。巨大動画を`read_bytes()`しない。

v2 invalidation:

- `max_chars`、`use_llm_captions`、`caption_model`、`default_template_id` → captions以降
- `output_width`、`output_height`、`output_fps` → planning以降
- `video_encoder` → legacy-only。v2 rendererへ渡さない

planning以降をinvalidateするとclip plan/projectとautomation outputを再生成対象にするが、human-owned `*.spectra`は触らない。

v2のoutput settings（`output_width`、`output_height`、`output_fps`）のauthorityは、JobManagerが現在のrunへ渡す値である。`caption_project.json`と`caption_project.v2.json`は字幕stageのcheckpointであり、resume/replan時に保存済みの古いoutput値をcurrent settingsへ復元しない。したがって、output settingsを変更した場合もcaptionsは保持し、planningが現在値の`target_output`を受け取って新しい`edit_project.v1`を生成する。

## Resume, retry, cancellation

- captionsまで完了しplanning未完了なら、resumeはplanningから続ける。
- planningが完了してrenderが未完了なら、planningを再実行せずrenderから続ける。
- 完全なrender manifestと出力がある場合、通常resumeは既存renderを再利用する。
- render-only retryはplanning manifestと各`edit_project.json`を変更せず、render manifestのexecution stateだけを無効化する。既存の正常な`planning/clips/*/automation/rendered.mp4`、`*.spectra`、human exportは事前削除しない。新出力はvideo_editor runnerのstaging→検証→`os.replace`でatomic promoteし、失敗・キャンセル時は旧MP4を保持する。
- partial renderは後続clipを継続し、top-level statusを`failed`にする。成功MP4は保持する。
- cancelはstage境界、planning substage境界、clip境界で確認する。外部video_editor中は対象processだけをterminateし、grace period後に必要ならkillする。stagingだけを削除し、既存valid outputを保持する。

候補が0件で`clip_decision`が正常完了した場合は、v2では「出力0件のcompleted」を正当なno-op結果として扱う。`planning`のcontract artifact配列と`render`のsummary.totalは0件になり、単一の代表動画を捏造しない。候補選択を必須とするproduction policyへ変更する場合は、別途readiness条件とテストを更新する。

## Legacy isolation

v2 production pathは`clip.create_clip`、`highlight.analyze_highlights`、`final_jumpcut_video.mp4`、`preview_proxy.mp4`、`highlight.json`、`highlights.json`、通常の`edit_regions.json`を生成しない。legacy implementation、旧editor/frontend、既存runのlegacy artifactは削除しない。

## UI

旧UIはlocal compatibility UIとして残す。v2では完成動画を単一Video componentへ代表表示せず、`clip_id`、state、rendered path、output hashを複数clip一覧で表示する。caption editorは既存caption projectを利用できるが、最終render authorityは外部video_editorである。

## Acceptance

- new job default v2、old run legacy
- v2 stage order and invalidation
- v2でcreate_clip/analyze_highlights未呼出
- 2〜3 independent edit_project/render outputs、concatなし
- partial failure、resume、render-only retry、cancel
- generated source、deterministic providers、実video_editor、FFmpeg、ffprobe
- legacy tests、full unittest、compileall、frontend build
