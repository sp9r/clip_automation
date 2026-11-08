# `edit_project` contract v1

## 1. 位置付け

`edit_project.json`は`clip_automation`と`video_editor`の正式な境界である。これは字幕専用の`caption_project.v2.json`を置き換えない。`caption_project.v2.json`と`transcript_words.json`から、必要な編集情報をコピーまたは参照情報へ変換して生成する。

最小の依存方向は次のとおり。

```text
clip_automation --write/validate--> edit_project.json --read/edit/render--> video_editor
```

`video_editor`はこの契約とsource mediaがあれば、`clip_automation`をインストールせずに動作できる。

## 2. JSON envelope

トップレベルは次の形にする。

```json
{
  "schema_version": "edit_project.v1",
  "project": {},
  "source_media": [],
  "sequence": {},
  "tracks": [],
  "captions": [],
  "metadata": {}
}
```

### `schema_version`

- 必須の文字列。
- v1の値は`edit_project.v1`。
- 後方互換性を壊す変更は`edit_project.v2`へ上げる。意味を変えたまま同じversionを使わない。
- readerは未知のmajor versionを編集・renderせず、明示的なunsupported errorを返す。

### `project` metadata

`project`はproject metadataを保持する。編集用のtimeline状態やundo historyはここへ混ぜない。

```json
{
  "id": "run_20260813_clip_001",
  "name": "sample clip",
  "created_at": "2026-08-13T00:00:00+09:00",
  "updated_at": "2026-08-13T00:00:00+09:00",
  "generator": {
    "name": "clip_automation",
    "version": "refactor-split-video-editor"
  }
}
```

`id`と`name`は必須。日時はISO 8601文字列。`generator`は任意だが、writerと入力runの識別に使う。

### `metadata`（writer監査情報）

`metadata`はv1 writerが生成する監査情報であり、次を必須とする。

- `source_run_id`: 入力runを識別する非空文字列。
- `writer_version`: writer実装を識別する非空文字列。
- `schema_version`: トップレベルと同じ`edit_project.v1`。
- `input_hashes`: 実際に使用した入力artifact名をkey、SHA-256 hex digestをvalueとする非空object。legacy経路では`edit_regions.json`、planning経路では`derived:planning_regions.json`を記録し、存在するだけで未使用のlegacy artifactは混ぜない。
- `source_media_probe`: source mediaの`duration`、`width`、`height`、`fps`を含むobject。各値は有限の正数とする。
- `diagnostics`: writerが生成したwarning/unsupported/errorの配列。errorまたはunsupportedが残るprojectは公開しない。

日時や実行時刻は決定性を損なうため、v1 writerは自動挿入しない。必要な日時は呼び出し側がproject metadataの任意fieldとして明示的に与える。

### `source_media`

source video/audioを参照する配列。

```json
[
  {
    "id": "media_main",
    "path": "source.mp4",
    "kind": "video",
    "sha256": "optional-sha256",
    "duration": 3600.25,
    "width": 1920,
    "height": 1080,
    "fps": 60.0,
    "sample_rate": 48000,
    "channels": 2
  }
]
```

- `id`、`path`、`kind`は必須。`kind`は`video`または`audio`。
- `path`のresolution baseは、常にその`edit_project.json`が存在するdirectoryとする。readerは`run_dir`を知っていることを前提にしない。
- 通常writerが出力する`path`は、resolution baseからsource mediaへ向かうrelative pathでなければならない。JSONでは`/`区切りへ正規化する。
- 通常writerはabsolute pathをportableな値として出力しない。source mediaとcontractの相対pathを安全に生成できない場合（Windowsの異なるdriveなど）は、`source_media_path_not_portable`等の明示的diagnostic/errorにして公開を中止する。silentなabsolute fallbackは禁止する。
- builder/writer呼び出し側は、`edit_project.json`の`contract_path`またはcontract directoryを明示してpathを生成する。build済みdictをwrite時に別baseへ暗黙書換えしない。
- `video_editor`は、project pathのparentと`source_media[].path`を結合してsourceを解決する。projectとsourceを共通の親directoryごと移動しても相対構造が保たれる限り解決できる。
- durationは秒。mediaのprobe値であり、timeline clipのsource範囲を超えてはならない。
- source media自体の実体をJSONへ埋め込まない。

### `sequence`

```json
{
  "id": "sequence_main",
  "width": 1080,
  "height": 1920,
  "fps": 30.0,
  "timebase": {"numerator": 1, "denominator": 1000},
  "duration": 58.4
}
```

- `width`、`height`、`fps`、`timebase`は必須の正数。
- `duration`はsequence時間の秒数。track clipの終了時刻以上であること。
- timeline timeはsequenceの開始を0秒とした秒数。内部でframe/tickへ量子化する場合も、JSON上の秒値を保持する。

## 3. TrackとClip

### `tracks`

```json
[
  {
    "id": "V1",
    "kind": "video",
    "name": "Video 1",
    "enabled": true,
    "locked": false,
    "clips": []
  },
  {
    "id": "A1",
    "kind": "audio",
    "name": "Audio 1",
    "enabled": true,
    "locked": false,
    "clips": []
  }
]
```

- `id`はproject内で一意。
- `kind`は`video`、`audio`、`caption`、`text`のいずれか。
- `enabled`はtrackの有効状態、`locked`はUI編集ロック。render対象外にするclipの状態はclip自身の`enabled`で決める。
- `muted`、`solo`などのeditor再生状態はv1では任意のtrack propertyとし、renderの必須意味にしない。

### timeline/source time

すべてのmedia clipは次の2つの時間軸を持つ。

- `timeline`: sequence上の配置。`start`と`duration`を秒で持つ。
- `source`: source media内の範囲。`start`と`duration`を秒で持つ。

通常の速度1.0では、`timeline.duration == source.duration`。速度変更をv1で表現しない場合は1.0以外を拒否する。半開区間`[start, end)`として扱い、durationは正数とする。

### video clips

```json
{
  "id": "vclip_001",
  "type": "video_clip",
  "source_media_id": "media_main",
  "timeline": {"start": 0.0, "duration": 12.5},
  "source": {"start": 184.2, "duration": 12.5},
  "transform": {
    "x": 0.0,
    "y": 0.0,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "rotation": 0.0,
    "opacity": 1.0,
    "anchor_x": 0.5,
    "anchor_y": 0.5
  },
  "enabled": true
}
```

必須は`id`、`type`、`source_media_id`、`timeline`、`source`、`transform`、`enabled`。`type`は`video_clip`。

### audio clips

```json
{
  "id": "aclip_001",
  "type": "audio_clip",
  "source_media_id": "media_main",
  "timeline": {"start": 0.0, "duration": 12.5},
  "source": {"start": 184.2, "duration": 12.5},
  "volume": 1.0,
  "enabled": true
}
```

`volume`はlinear gainで、既定値1.0。0以上の有限値とする。mutedは`enabled=false`で表す。trackを維持したまま無効化できる。

## 4. Captions

字幕はtrackに置くclipを補助するトップレベル配列として定義する。v1では字幕の表示内容とタイミングをrender可能な形で固定する。

```json
[
  {
    "id": "caption_001",
    "track_id": "C1",
    "text": "表示する字幕",
    "timeline": {"start": 1.2, "duration": 2.8},
    "source": {"start": 184.2, "duration": 2.8},
    "words": [
      {"id": "source-word:123", "text": "表示", "timeline": {"start": 1.2, "duration": 0.4}},
      {"id": "source-word:124", "text": "する", "timeline": {"start": 1.6, "duration": 0.4}}
    ],
    "style": {
      "template_id": "standard",
      "font_name": "Noto Sans JP",
      "font_size": 64,
      "primary_color": "#FFFFFF",
      "outline_color": "#000000",
      "outline": 8,
      "alignment": 2,
      "position_x": null,
      "position_y": null
    },
    "enabled": true
  }
]
```

- `id`、`track_id`、`text`、`timeline`、`enabled`は必須。
- `source`は元transcriptや字幕v2との対応を保持するための任意情報。存在する場合はsource media範囲内であること。
- `words`はeditorの単語境界編集用optional metadataであり、render必須ではない。存在する場合、各要素は`id`（non-empty string）、`text`（string）、`timeline`（sequence timeline上の秒、start >= 0、duration > 0）を持つ。wordの終了はcaptionの終了以内、word列はmonotonic・non-overlap・caption内ID uniqueで、`"".join(words[].text) == caption.text`を満たす必要がある。`words`がない既存captionは従来どおりvalidである。
- serialized timeline secondsのword ordering／caption containment validationは、`start + duration`のbinary float再加算による偽overlapを避けるため、Decimalのvalue semantics（`Decimal(str(value))`相当）で比較する。これはtimestamp補正、microsecond量子化、epsilon toleranceによるvalidation緩和ではなく、JSONに保存された十進値を変更しないためのvalidator内部表現である。真のoverlap、reversal、caption外れは引き続きrejectする。
- `words[].id`は`transcript_words.json`の`word_id`を決定的に`source-word:<word_id>`へ変換した値で、random UUIDを使わない。`source_word_ids`は既存のfirst/last source word ID range metadataとして互換保持し、wordsが併存する場合は先頭・末尾identityと整合させる。
- word timingはsource/original timeではなく最終sequence timelineである。jumpcut後のsequence時刻を記録し、source mappingを推測して追加しない。
- display textがtranscript wordsの連結と一致しないcaptionは、caption自体を生成・render可能な状態で保持し、wordsだけを省略してwarning diagnosticを残す。
- wordがkeep/delete境界を部分的に跨ぐ場合は、安全なword timingを作れないためbuild error `word_crosses_delete_boundary`とする。captionを不完全なwords付きで生成したり、比例推定で補正したりしない。
- `words`の追加は`schema_version: edit_project.v1`のまま行うadditive optional metadataである。writer監査用`metadata.writer_version`は`clip_automation.edit_project_v1/3`へ更新する。
- Phase 1 writerは`source_caption_id`、`source_word_ids`、`source_text`をcaptionの対応情報として保持する。`source`を保持できない不連続区間では、sequence時間を正本とし、diagnosticで理由を残す。
- `style`の意味はvideo_editorが所有する。未知のstyle keyはreaderが保持できる限り保持し、renderできない場合はdiagnosticを出す。
- `enabled=false`のcaptionはrender対象外だが、編集projectから自動削除しない。

### caption_project.v2からのword ownership

字幕生成の最終出力は、表示文字列からword範囲を再推測せず、次の4項目を明示する。

- `start_word_id` / `end_word_id`: `transcript_words.json`の連続したinclusive範囲。単語途中の境界は禁止する。
- `source_text`: 範囲内のsource word本文をそのまま連結した値。word正本との不一致は生成エラーとする。
- `text_status`: `as_transcribed`、`display_normalized`、`manually_corrected`のいずれか。
- `display_text`（edit_projectでは`text`）: editorへ表示する値。

共通のdisplay normalizationは`、。！？!?…・〜～`の連続だけを除去し、空白やその他の文字は変更しない。`as_transcribed`はsourceと同一、`display_normalized`はsourceへこのnormalizationを適用した値でなければならない。`manually_corrected`は保存済み表示を使うが、transcript word列から安全に再構成できないためoptional editor wordsを省略する。

### zero-duration wordとplanning/adapter境界

`transcript_words.json`のwordは`timeline_start == timeline_end`のpoint eventを取り得る。`end < start`だけを不正とし、同時刻のwordは入力順を保持する。planningではpointをその時刻を含むwindowへ所属させる（overlap windowでは重複し得る）。provenanceは元wordの安定ordinalを使い、同じ時刻・同じ本文でも別identityを統合しない。

edit_project adapterはpoint wordをpositive-duration interval mapperへ渡さない。keep/delete regionsは半開区間 `[start, end)` として判定し、keepの直後にdeleteが始まる境界のpointはdelete、deleteの直後にkeepが始まる境界のpointはkeepとする。timeline末尾のpointだけは最終regionに所属させる。keepされたpoint wordはcaptionのsource ownershipと表示本文へ残すが、単語単位のsequence durationを作らない。

point wordを含むcaptionはrender可能性を優先し、optionalなcaption `words` metadata全体を省略してwarning `caption_word_metadata_omitted_zero_duration_word`（source word IDと件数）を記録する。pointだけで構成されるcaption runは安全なtimeline durationを持たないため、`caption_zero_duration_only_run`でfail closedする。deleteされたpointは表示本文の再構成対象から除外する。`display_normalized`は各runのsource word本文へ同じnormalizationを適用し、`manually_corrected`が複数runへ分割される場合は文字単位の推定をせず`caption_split_unsafe`としてunsupportedにする。

句読点だけのsource wordはownership範囲へ保持するが、display projectionで空のeditor wordを生成しない。adapterはwarningを残してwords全体を省略する。

fresh native v2生成はこの明示ownershipを正本として使い、表示文字列からの範囲推測を行わない。ownershipがない旧fixture・旧呼び出しだけは`migrate_v1_project()`のlegacy互換経路を使う。旧migrationでstatus意味を安全に確定できない場合、statusを付与せず既存projectを壊さない。

## 5. 最小の完全例

```json
{
  "schema_version": "edit_project.v1",
  "project": {"id": "sample", "name": "sample clip"},
  "source_media": [
    {"id": "media_main", "path": "source.mp4", "kind": "video", "duration": 10.0, "width": 1920, "height": 1080, "fps": 30.0}
  ],
  "sequence": {"id": "sequence_main", "width": 1080, "height": 1920, "fps": 30.0, "timebase": {"numerator": 1, "denominator": 1000}, "duration": 4.0},
  "tracks": [
    {"id": "V1", "kind": "video", "name": "Video 1", "enabled": true, "locked": false, "clips": [
      {"id": "v1", "type": "video_clip", "source_media_id": "media_main", "timeline": {"start": 0.0, "duration": 4.0}, "source": {"start": 2.0, "duration": 4.0}, "transform": {"x": 0, "y": 0, "scale_x": 1, "scale_y": 1, "rotation": 0, "opacity": 1, "anchor_x": 0.5, "anchor_y": 0.5}, "enabled": true}
    ]},
    {"id": "A1", "kind": "audio", "name": "Audio 1", "enabled": true, "locked": false, "clips": [
      {"id": "a1", "type": "audio_clip", "source_media_id": "media_main", "timeline": {"start": 0.0, "duration": 4.0}, "source": {"start": 2.0, "duration": 4.0}, "volume": 1.0, "enabled": true}
    ]},
    {"id": "C1", "kind": "caption", "name": "Captions", "enabled": true, "locked": false, "clips": []}
  ],
  "captions": [],
  "metadata": {
    "source_run_id": "sample-run",
    "writer_version": "clip_automation.edit_project_v1/1",
    "schema_version": "edit_project.v1",
    "input_hashes": {"source.mp4": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
    "source_media_probe": {"duration": 10.0, "width": 1920, "height": 1080, "fps": 30.0},
    "diagnostics": []
  }
}
```

## 6. Validation invariants

1. `schema_version`は`edit_project.v1`である。
2. IDは各配列およびtrack内で一意である。
3. すべての参照`source_media_id`と`track_id`が存在する。
4. `timeline.start >= 0`、`timeline.duration > 0`、`source.start >= 0`、`source.duration > 0`。
5. sourceの終了時刻はsource mediaのduration以下である。
6. `sequence.duration`は有効clip/captionのtimeline終了時刻以上である。
7. `enabled`は必ずboolean。無効化されたものもJSON上は保持する。
8. `transform`の数値は有限値。`opacity`は0〜1、scaleは正数。
9. `volume`は0以上の有限値。
10. timebase、fps、sequence width/heightは正数。
11. source時間とtimeline時間を、同じ値として省略しない。jumpcut/trimがあっても両方を保持する。
12. 未知フィールドは、readerが安全に保存できる場合に限り保持し、意味を推測して変更しない。

## 7. 既存資産からの生成規則

- `transcript_words.json`の`source_start/source_end`と`timeline_start/timeline_end`は字幕対応の入力として読む。
- `caption_project.v2.json`の`display_text`、word範囲、template/style、deletedをcaptionへ写像する。
- `edit_regions.json`のkeep区間をvideo/audio clipのsource/timeline範囲へ変換する。変換不能なgap/overlapは黙って補正しない。
- `edl.json`はsource→jumpcut timelineの基礎情報として使う。source時間を失わない。
- 生成時に元ファイルを移動・削除・上書きしない。
- 生成したcontractには、入力ファイルのhash、run ID、writer versionを`metadata`へ記録する。

## 8. v1で定義しないもの

速度変更、トランジション、エフェクトgraph、keyframe曲線、複雑なaudio routing、投稿metadataはv1の必須contractに含めない。必要になった場合は後方互換なoptional fieldまたは新しいschema versionとして提案し、暗黙に追加しない。
