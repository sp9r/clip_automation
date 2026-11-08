# Phase 7: Publishing Foundation + YouTube Upload

## Scope and boundary

Phase 7はrender済みclipを投稿可能なdurable jobへ変換し、明示的な操作でYouTubeへ送る。TikTok / Instagram、AIタイトル・説明・サムネイル、予約公開、analytics、comments、playlist、monetization、Phase 8は対象外である。

```text
planning/edit_project.v1
  → video_editor
  → planning/clips/<clip_id>/automation/rendered.mp4
  → publishing/publish_manifest.v1.json
  → explicit prepare / dry-run / publish
  → YouTube adapter
```

`render_planning_outputs()`、v2のrender stage、video_editorはYouTube APIを呼ばない。通常JobManager jobはrender completedで終了し、投稿は`prepare_publish()` / `publish()`またはCLIの明示操作だけで開始する。

## Files and manifest

```text
run_dir/
  planning/render_manifest.v1.json
  planning/clips/<clip_id>/automation/rendered.mp4
  publishing/publish_manifest.v1.json
  publishing/<clip_id>/thumbnail.jpg  # optional derivative
```

各completed renderだけがprepare対象になる。render manifestのoutput SHA-256と実ファイルを比較し、不一致ならuploadを禁止する。failed / skipped renderはskipし、他clipを止めない。`manual.spectra`やhuman exportは自動探索しない。

`publish_manifest.v1`には`schema_version`、`platform`、render manifest path/hash、clipごとのstate、video path/hash、metadata、thumbnail、remote video ID/url/processing status、error、summaryを保存する。manifestは各状態遷移でtemporary file、flush、fsync、atomic replaceする。token、refresh token、client secret、authorization code、Authorization headerは保存しない。

## State machine and idempotency

```text
prepared → uploading → uploaded → thumbnail_setting → processing → completed
```

失敗は`failed`、キャンセルは`canceled`、processing timeoutは`processing_pending`として保存する。thumbnailだけが失敗した場合は`uploaded_with_thumbnail_error`とし、remote video IDを保持する。

`remote.video_id`が保存済みなら通常retryは`videos.insert`を再実行しない。明示的な`force_reupload=True` / `--force-reupload`だけが例外である。partial failureでも後続clipを継続する。

再prepareはlocal preparationだけを再生成し、同一`clip_id`・同一video SHA-256・同一platformのremote identityをmergeして保持する。upload済み後のmetadata変更は`metadata_changed_after_upload`として診断記録するだけで、Phase 7.1ではremote metadataを自動更新しない。render SHA-256が変わった場合は旧`remote.video_id`を新renderへ継承せず、旧remoteは`previous_remote`へ監査用に保持し、`needs_republish_confirmation`で停止する。新renderの投稿には`--confirm-changed-render`が必要である。Local preparation may be recreated; remote identity must never be silently discarded.

## Metadata and privacy

AI生成は行わない。CLI/UI入力またはclip_idからのdeterministic defaultを使う。既定privacyは`private`で、`private` / `unlisted` / `public`だけを受け付ける。title、description、tags、category_id、made_for_kidsをlocal validationする。

現在の公式YouTube Data API仕様に合わせ、titleは100文字、descriptionは5000 UTF-8 bytes、tags合計は500 bytes以下とする。`<` / `>`は拒否する。[Videos resource](https://developers.google.com/youtube/v3/docs/videos)を仕様参照とする。

## OAuth and credentials

desktop installed-app flowを使う。

```text
system browser → http://127.0.0.1:<random-port> loopback
  → authorization code → PKCE token exchange → data/tokens/youtube.json
```

OOB/manual copy-pasteは使わない。client secretsは`data/credentials/youtube_client.json`または`SPECTRA_YOUTUBE_CLIENT_SECRETS`、tokenは`data/tokens/youtube.json`または`SPECTRA_YOUTUBE_TOKEN_FILE`で指定する。両方とも`.gitignore`で除外する。

scopeは最小化する。

- Phase 7標準auth: `https://www.googleapis.com/auth/youtube.upload` + `https://www.googleapis.com/auth/youtube.readonly`
- `youtube.upload`は動画管理、`youtube.readonly`はアカウント参照に使用する。processingDetailsは所有者向け`videos.list`で取得するため、statusを標準機能として提供するPhase 7では最初から両方を要求する。
- installed appではincremental authorizationを利用しない。既存tokenのgranted scopeが不足する場合は`InsufficientOAuthScopeError`でAPI実行前に停止し、tokenは削除しない。

OAuth方針は[OAuth 2.0 for Mobile & Desktop Apps](https://developers.google.com/youtube/v3/guides/auth/installed-apps)、upload/resumable uploadは[Upload a Video](https://developers.google.com/youtube/v3/guides/uploading_a_video)と[Resumable Uploads](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)を参照する。

## YouTube adapter and thumbnail

`publishing.youtube.Publisher`をplatform boundaryとし、Phase 7では`YouTubePublisher`だけを実装する。動画はGoogle clientの`MediaFileUpload(..., resumable=True)`で全fileをmemoryへ読まず送信し、temporary HTTP errorだけ最大回数付き指数backoffでretryする。

upload成功条件はAPI responseのvideo ID取得である。processingはbounded pollingで、timeout時は`processing_pending`を保存し、`status` commandで再確認する。thumbnailはexplicit user imageまたはdeterministic FFmpeg frameだけを使い、元ファイルを上書きせずpublishing-owned derivativeへ保存する。JPEG/PNG、2 MiB以下をlocal validationし、公式条件は[Thumbnails: set](https://developers.google.com/youtube/v3/docs/thumbnails/set)を参照する。

## CLI and dry-run

```powershell
python upload.py auth
python upload.py prepare <run_dir> --title "..." --privacy private
python upload.py publish <run_dir> --dry-run
python upload.py publish <publish_manifest.v1.json>
python upload.py publish <publish_manifest.v1.json> --confirm-changed-render
python upload.py status <publish_manifest.v1.json>
```

`dry-run`はrender output/hash、metadata、thumbnail、credential file availability、planned clipsを表示するが、`videos.insert` / `thumbnails.set` / status network callを0件にする。live YouTube testは`SPECTRA_YOUTUBE_LIVE_TEST=1`などのexplicit opt-inを別途要求し、private固定とする。通常unit testはfake publisherだけを使う。

## Phase 7.2 private acceptance

実アカウント受入では、FFmpeg生成の640x360 / 30fps / 4秒 / generated video + 440Hz AAC audioを1 clipだけ使用した。`Spectra Phase 7.2 Private Upload Test`、privacy `private`、dry-runのremote call 0件、render hash検証、1280x720未満のJPEG thumbnail設定を確認した。実YouTubeの`videos.insert`でvideo IDを取得し、remote `privacyStatus=private`、processing `succeeded`、thumbnail `completed`を確認した。

同一manifestの再publish、同一runのreprepare後publishともremote video identityを保持し、重複uploadを発生させなかった。acceptance中にWindowsの不要なmanifest再書込みで一時的なPermissionErrorが出たため、remote IDが保存済みで状態が`uploaded`のidempotent no-opではmanifestを書き換えない最小修正を追加し、Phase 7回帰テストを再実行した。test videoは自動削除していない。

JobManagerの通常stageはPhase 6のまま維持し、explicit `prepare_publish(job_id)` / `publish(job_id, ...)`だけを追加する。UIはタイトル・説明・tags・privacyを確認してから投稿準備、dry-run、YouTube投稿、processing status更新を個別に押す。publicへの暗黙切替は行わない。

## Security and acceptance

fake token `VERY_SECRET_ACCESS_TOKEN`を使ったfailureで、manifest、diagnostic、callback logにtokenが出ないことをテストする。TikTok / Instagramのmoduleやnetwork callはPhase 7へ追加しない。live YouTube uploadは未実施でもPhase 7 failureとはしない。
