# Editor分割・移行計画

> **Current checkpoint (2026-08-15):** Phase 3.4 and Step 3.5A-1 are CLOSED. The subtitle-words bridge through `edit_project.v1` is accepted, and `video_editor` is the manual subtitle editing authority. Step 3.5A-1 only visualizes existing subtitle word timing and does not add split, drag, or ownership-edit interaction. Step 3.5A-2/3 remain future work.

## 1. 目的

`clip_automation`の解析・自動化機能と、汎用動画編集機能を分離する。今回の計画は安全な移行準備であり、旧editorの削除や大規模なコード移植は含まない。

正式な接続は次の一方向だけにする。

```text
clip_automation
    └─ write/validate edit_project.json (edit_project/v1)
          └─ video_editor open / preview / edit / render
```

`video_editor`は`edit_project.json`または動画ファイルだけで起動できる。`clip_automation`のrun directory、Python module、FastAPI endpoint、job manifestに依存しない。

## 2. 移行対象の分類

### clip_automationへ残す

- YouTube/local video取得
- Whisper文字起こし、word時刻、無音・フィラー解析
- 字幕生成、`caption_project.v2.json`、`transcript_words.json`
- ハイライト候補、AI評価、feedback、keep/delete区間
- 編集計画生成、`edit_project/v1`の出力
- job管理、再開、失敗回復、投稿処理

### video_editorへ新設・移管する

- 編集project/sequence/track/clipモデル
- video/audio/caption/textのtimeline操作
- Preview、手動修正、Undo/Redo
- transform、volume、enabled/disabled
- FFmpeg等の最終render、headless render、render report

### 移行完了までlegacyとして残す

- `editor_api.py`
- `editor_frontend/`
- `preview.py`、`ui/app.py`内の字幕編集導線
- `renderer_html.py`、`clip.py`の現行render経路
- `project_creation.py`のeditor向けupload/status導線

旧editorは「廃止予定」ではあるが、置換確認が完了するまではfallbackであり、今回削除しない。

## 3. 段階別実装計画

| Phase | 主な変更 | 受入条件 | rollback |
|---|---|---|---|
| 0 | 監査、branch、設計、contract、AGENTS | 既存test/buildがbaseline同等、既存run不変 | 新規文書commitだけをrevert |
| 1 | `edit_project/v1` validator/writer/fixture | 評価copyから決定的JSONを生成し、旧ファイルhash不変。14件のPhase 1テスト、全体unittest、frontend buildがPASS | adapterを使わず現行pipelineへ戻す |
| 2 | highlight→evaluation→decision→planのstage分離 | 候補・区間・計画の各JSONが独立検証可能。N候補→N独立project、manifest/hash追跡、Phase 1 mapping再利用。専用17件・全体108件・frontend buildがPASS | 旧highlight/regions経路を読む |
| 3 | video_editor standalone loader/CLI/最小UI | clip_automationなしでopen/preview/headless render | legacy editorを利用 |
| 4 | 手動編集、undo、render parity | 実動画で旧editorの主要シナリオを再現 | 新editor出力を採用せず旧editorへ戻す |
| 5 | jobからheadless render・投稿まで接続 | URL入力から投稿まで一方向追跡 | contractを保存してrenderのみ再試行 |
| 6 | 旧editor fallback化・縮小 | 新editorの長尺/再起動/失敗回復が確認済み | 旧editorをread/legacy入口として維持 |

## 4. Phase 1の最小作業単位

1. `edit_project/v1` schema validatorを追加する。
2. `caption_project.v2.json`と`transcript_words.json`を読むadapterを追加する。読み取り専用にする。
3. `edit_regions.json`からvideo clipのtimeline/source範囲を作る。
4. audio clipは元sourceを参照し、volume=1.0、enabled=trueを初期値にする。
5. captionsは字幕v2のdisplay text/styleを、編集project側のcaption clipへコピーする。以後の編集はvideo_editorのProject側で行う。
6. source時刻を失わないよう、jumpcut後のtimeline timeと元動画のsource timeを両方保持する。
7. fixture、schema validation、JSON round-trip、既存ファイルhash回帰を追加する。

Phase 1では上記を実装済みである。`edit_project.py`のmappingは、source time→jumpcut timeline→sequence timeを明示的に扱う。EDL境界をまたぐkeepは複数clipへ分割し、video/audioに同じmappingを適用する。字幕はword境界だけで切り、手動修正文字列を推測分割しない。生成は`EditProjectBuildResult`とdiagnosticsを返し、error/unsupportedが残る場合はcontractを公開しない。

### Phase 2実装状態

- `planning_artifacts.py`: 4つのversion付きartifactのvalidatorとatomic JSON IO。
- `planning_core.py`: 全timeline coverage chunking、overlap、deduplication、明示weight score、deterministic decision、per-clip region生成。
- `planning_providers.py`: deterministic fixture、fallback、failure、Ollama adapter。Ollamaはcoreへ直接依存しない。
- `planning_pipeline.py`: explicit planning directoryへartifactと`clips/<clip_id>/`を出力し、Phase 1 writerから独立`edit_project.v1`を生成。
- `planning_manifest.py`: 5 stageのstate/schema/path/SHA-256/input fingerprint/provider/errorを追跡。single-artifact stageはlegacy fieldsを維持し、clip planning/contract generationは全clipの`artifacts[]`（clip_id、path、sha256、schema_version、state）を追跡する。

### Phase 2.5 contract/planning境界の修正

- `source_media[].path`の基準を`edit_project.json`が存在するdirectoryへ固定した。JSONは`/`区切りのrelative pathを基本とし、異なるdrive等でportable pathを生成できない場合は明示的に失敗する。
- per-clipの正式経路は`clip_plan.v1`の`regions`から`edit_project.v1`を生成する。`regions`引数がある場合、legacy `edit_regions.json`はrequiredでもinput hash対象でもない。`regions=None`のPhase 1互換経路だけが読む。
- manifestは複数clipの成功・失敗を個別に保存し、contract generationの部分失敗からretry対象を特定できる形にした。
- Phase 3、video_editor本体、旧pipeline統合は未着手である。

旧`pipeline.py`のrender→highlight順はPhase 2で変更していない。実runのresume互換を壊さずに新planning pathを先に受入し、stage order変更は後続phaseの判断対象とする。

このPhaseでは、現行`editor_api.py`のexportをadapterへつなぎ替えない。新しいwriterの出力が実動画で検証されるまで、旧経路を変更しない。

## 5. 安全策

- 実runへ直接生成しない。まず一時fixture、次に`data/evaluations/<id>/`の評価copyを使う。
- 入力ファイルのSHA-256、ffprobe情報、contract schema version、writer versionをmanifestへ記録する。
- contractはtemporary file→fsync→atomic replaceで公開する。
- 未対応の編集表現は黙って捨てず、diagnosticsへ記録して生成を失敗させるか明示的fallbackにする。
- Phase 1のdiagnostic severityは`warning`、`unsupported`、`error`の3種類とし、手動修正字幕の安全でない分割は`unsupported`として停止する。
- 旧caption projectを`edit_project`へ移動・renameしない。両方を一定期間保持する。
- render結果は`.partial`で生成し、ffprobe検証後だけ正式名へ昇格する。
- 投稿処理はrender検証後だけ開始する。render失敗を投稿成功として扱わない。

## 6. 完了判定

次のすべてを満たすまで「editor分割完了」としない。

- `video_editor`が`clip_automation`なしでfixtureを開ける。
- 同じ`edit_project/v1`からUI previewとheadless renderが同一のtime/transform/captionを扱う。
- 実動画でvideo/audio/captionの同期が確認できる。
- 長尺動画、再起動、render失敗、未対応フィールドの回復を確認できる。
- 旧editorをfallbackとして残したまま、同等の作業を新editorで再現できる。
- `clip_automation`から`video_editor`への呼び出しがcontractと公開CLI/APIだけになっている。
- 既存`caption_project.v2.json`と`transcript_words.json`を破壊していない。

## 7. Phase 5実装境界

Phase 5の実装では、`clip_automation`側に公開CLI bridge（`video_editor_runner.py`）、planning成果物を順に処理するorchestrator（`render_pipeline.py`）、および`render_manifest.v1` IOを置く。これは`video_editor`の内部実装を再利用するものではなく、`edit_project.v1`と公開CLIだけを知る外部process adapterである。

1候補につき1つの`edit_project.json`と1つの独立renderを作る。複数clipを連結せず、renderは`clips/<clip_id>/automation/`だけへ出力する。投稿処理へ渡すのはatomic promoteと検証を完了した出力だけであり、部分失敗はmanifestへ残して後続clipと将来のretryを妨げない。

Phase 5はPhase 6の投稿接続・旧editor縮小・pipeline全体のstage順変更を含まない。

## 8. Phase 6 production cutover

新規production jobは`pipeline_version=v2`を既定とし、`acquire → transcribe → jumpcut → captions → planning → render`を実行する。既存manifest、旧run、明示的legacy jobは`pipeline_version=legacy`として旧stage順を維持する。pipeline versionを持たない既存manifestをv2へ昇格させない。

v2の正本はrun rootのruntime manifest、planning配下のplanning manifest、render manifest、clipごとの`edit_project.v1`と独立MP4である。旧`final_video_path`をv2の代表出力として作らず、UI/APIはplural `clips`、`completed_outputs`、`failed_outputs`を扱う。

旧`clip.py`、`highlight.py`、`editor_api.py`、`editor_frontend/`、legacy artifactは削除しない。新v2経路からlegacy rendererを隔離し、Phase 7の投稿処理はまだ実装しない。

## 9. Phase 7 publishing

Phase 7は`publishing/`を新しい外部side-effect境界とする。render完了から自動投稿せず、`publish_manifest.v1`をprepareし、ユーザーの明示操作でYouTube adapterを実行する。`clip_automation`はOAuth、metadata、thumbnail、remote processing status、retry/idempotencyを所有するが、video_editorは投稿APIを知らない。TikTok / Instagram direct publishingは未実装のまま将来adapter境界だけを残す。
