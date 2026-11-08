# Phase 5: external video_editor headless render

## 目的と境界

Phase 5は、`clip_automation`が作成したversion付き`edit_project.v1`を、兄弟リポジトリ`video_editor`の公開standalone CLIへ渡してclip単位でrenderするための連携である。

```text
clip_automation
  planning/clips/<clip_id>/edit_project.json
      -> external process: video_editor CLI
      -> clips/<clip_id>/automation/rendered.mp4
```

clip_automationはvideo_editorのTypeScriptを直接importせず、`PYTHONPATH`、内部HTTP、renderer/UI、FFmpeg filter graph、concatにも依存しない。今回のPhase 5では投稿処理の接続やPhase 6の統合は実施しない。

## CLI契約

video_editorの公開CLIは次の形式である。

```powershell
node <video_editor_root>/scripts/video-editor.mjs render <edit_project.json> -o <staging.mp4>
```

`--no-hash`などのproduction用でないoptionは付けない。stdoutの最終JSONは保存し、progressや診断はstderrログへ保存する。runnerはexit code、stdout JSON、staging outputが全て妥当な場合だけ成功とする。

## command解決とpreflight

解決優先順位は次のとおり。

1. API/CLIへ渡した明示argv配列
2. `VIDEO_EDITOR_COMMAND_JSON`（JSON文字列配列）
3. `VIDEO_EDITOR_ROOT`（またはAPIのroot指定）から`node`とlauncherを組み立てる
4. `clip_automation`の兄弟`video_editor`ディレクトリ

パスに空白、日本語、括弧があってもargvの各要素を保持する。実行は`shell=False`、`stdin=DEVNULL`、stdout/stderrファイル分離で行う。

preflightはcommand source/typeごとに分かれる。

- root/sibling discovery: clip_automationが構築した既知の`node + scripts/video-editor.mjs`をstrictに検証し、Node、launcher、通常運用で必要な`out/cli/video-editor.js`を確認する。
- explicit command / `VIDEO_EDITOR_COMMAND_JSON`: arbitrary external argv prefixとして扱い、commandが空でないことと`command[0]`のexecutable解決だけを必須にする。`["video-editor"]`、`["C:\\Tools\\video-editor.exe"]`、`["custom-wrapper", "--profile", "spectra"]`のようなcommandを許容し、`command[1]`をscript pathとは解釈しない。
- explicit/envでも`node .../scripts/video-editor.mjs`という既知wrapper形式を検出できる場合だけ、launcherとbuild artifactの追加検証を行う。

build成果物がない既知wrapperの場合は、`video_editorをbuildしてください`に相当する明示的なエラーにする。generic external CLIではpackage version、git commit、wrapper SHA-256などが取得できなくてもnullを許容する。provenanceの`node_version`は既存manifest互換のため維持し、実際にはcommand executableのversionを意味する。

## outputと再実行

各clipの出力先は次のとおり。

```text
planning/
  render_manifest.v1.json
  clips/<clip_id>/automation/
    rendered.mp4
    render.stdout.log
    render.stderr.log
```

実際のCLI出力は`.render-<uuid>.mp4`へ書き、存在・regular file・0 byteでないことを確認してから`os.replace`で`rendered.mp4`へ昇格する。失敗時はstagingだけを削除し、既存の正常な`rendered.mp4`は保持する。project、clip plan、`manual.spectra`などplanning外の編集資産は変更しない。

clipは独立して処理する。2件目が失敗しても3件目へ進み、全処理後にmanifestのfailed件数に応じてCLIはnonzeroを返す。clip間のconcatや単一の全体動画生成はPhase 5の責務ではない。

## render_manifest.v1

`render_manifest.v1.json`はplanning manifestとは別のautomation成果物で、各clipを次の情報で追跡する。

- `clip_id`
- `state`: `running`、`completed`、`failed`、`skipped`
- `edit_project.path`とSHA-256
- `output.path`、SHA-256、size
- stdout/stderr log path
- rendererのargv prefix、package version、git commit、wrapper SHA-256、Node version（取得不能ならnull）
- exit code、renderer stdout JSON、error

manifestは初期化時と各clip処理後に一時JSON、fsync、atomic replaceで保存する。したがって途中停止後も完了済みclip、失敗clip、未処理のcontract generation failureを識別できる。planning manifestのcontract artifact順を処理順の正本として使い、contract generation未完了のclipは`skipped`として残す。

## API/CLI

Python API:

```python
from render_pipeline import render_planning_outputs

result = render_planning_outputs(
    planning_dir,
    video_editor_root=r"C:\Storage\30_Software\90_Self_Made\Apps\video_editor",
)
```

CLI:

```powershell
python render_planning.py <planning_dir> --video-editor-root <video_editor_root>
python render_planning.py <planning_dir> --video-editor-command-json '["node",".../video-editor.mjs"]'
```

`render_pipeline.run_phase5()`は既存のplanningを先に完了させ、そのplanning outputを外部renderへ渡す薄い統合入口である。既存の旧pipeline stage順は変更しない。

## 検証

fake runnerで、3 clip独立処理、途中失敗継続、missing/zero output、invalid stdout、missing executable、空白・日本語・括弧を含むpath、`shell=False`、failed rerun時の旧output保持を検証する。

real E2Eではdeterministicな生成mediaを用い、planning→2件のedit_project→実video_editor CLI→2個のMP4→ffprobeを検証する。出力ごとにvideo geometry、duration、audio streamを検査し、`concat*`成果物がないことも検査する。

## Phase 5で未実施

- clip_automationの旧render経路の削除・統合
- pipeline全体のstage順変更
- 投稿APIへの接続
- editor UI、手動編集、linked editing
- Phase 6のfallback縮小
