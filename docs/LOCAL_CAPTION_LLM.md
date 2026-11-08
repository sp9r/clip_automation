# ローカル字幕LLMとWhisperの使い分け

## 設定

字幕workerは、次のどちらかで設定が見つかった場合に、OpenAI互換のローカルサーバー（GALLERIAのllama.cpp router、Qwen3.6-35B-A3B）へ字幕分割を依頼する。どちらも無い場合は、従来の`qwen2.5:14b`とOllama `http://localhost:11434/api/generate`を使う。

1. 環境変数（優先）

| 環境変数 | 設定内容 |
|---|---|
| `SPECTRA_CAPTION_LLM_BASE_URL` | `http://127.0.0.1:8089/v1` |
| `SPECTRA_CAPTION_LLM_MODEL` | `qwen3.6-35b` |
| `SPECTRA_CAPTION_LLM_API_KEY_FILE` | 認証情報正本のキーファイルへのパス |

2. worker横の`caption_llm.local.json`（Git対象外）。キーは書かず、キーファイルのパスだけを書く。アプリをどの起動方法で開いても同じ設定になる。

```json
{"base_url": "http://127.0.0.1:8089/v1", "model": "qwen3.6-35b", "api_key_file": "C:/.../lan_llm_api_key.txt"}
```

テストでは`SPECTRA_CAPTION_LLM_DISABLE_LOCAL_CONFIG=1`でローカル設定を無視する。

## WhisperとQwenはGPUを交代で使う（2026-10-21）

RTX 3060（12GB）では、Qwen3.6が読み込まれているとVRAMが約11.4GB埋まり、WhisperをGPUで動かせない。同時には使わず、次の順で交代させる。

1. 文字起こしの前に、routerへ`POST /models/unload`を送ってQwenをVRAMから降ろす（`caption_llm.release_gpu`）。router以外のサーバーや接続失敗では何もしない。
2. WhisperをGPU（float16）で実行する。
3. Whisperのモデルを解放する（model cacheのclearとgc）。
4. `POST /models/load`でQwenを読み込み、読み込み完了まで待つ（`caption_llm.ensure_loaded`、最大300秒）。読み込みの待ち時間は生成のタイムアウトとは別に数える。
5. 字幕分割を依頼する。読み込みに失敗した場合は、以後のバッチも待たずにルール方式へ切り替える。

1回の生成のタイムアウトは、ローカルQwenを使うときだけ300秒（従来120秒）。RAMの空きが少ないとプロンプト処理が遅くなるため。

## Whisperのモデル

日本語（`language=ja`）は`aikobin/whisper-ja-1.5B-ct2`、それ以外は`large-v3`。2026-10-21に実音声2本（解説音声188秒、対談120秒）でlarge-v3・large-v3-turbo・whisper-ja-1.5Bを同じ設定（beam 5、VAD、単語時刻）で比べ、whisper-ja-1.5Bが固有名詞（互助組織、コミュニティ等）を正しく認識し、語尾の欠落も少なかった。large-v3は「メンサ」を「面差」、「コミュニティ」を「アプリティ」と誤認した。単語時刻の逆行は3モデルともなかった。`--whisper-model`で上書きできる。

## 安全上の扱い

OpenAI互換通信はloopback URLだけを許可し、リダイレクトを拒否する。APIキーはリクエスト時にファイルから読み、Authorizationヘッダーだけへ設定する。例外へサーバー応答本文・認証ヘッダー・キーの保存パスを含めない。生成APIは`chat/completions`、`response_format=json_object`、`temperature=0.1`、`max_tokens=4096`、`chat_template_kwargs.enable_thinking=false`、非ストリーミング。途中打ち切りや不正応答では既存のfallbackへ進む。

`status=available`はAPI到達とモデル広告を表す。GPU配置やVRAM量はこのAPIで測定できないため、`acceleration=unmeasured`と記録する。

検証は`tests/test_caption_llm.py`（認証、payload、loopback制限、秘密を含む例外の抑止、旧Ollama互換、GPU交代、読み込み失敗時の即fallback、言語別Whisperモデル）。
