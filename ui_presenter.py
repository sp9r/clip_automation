def format_user_error(error):
    retry = "再試行可能" if error.get("retryable") else "設定修正が必要"
    return (
        f"[{error.get('error_code', 'ERROR')}] {error.get('message', '処理に失敗しました。')}\n"
        f"状態: {retry}\n次の操作: {error.get('user_action', '診断を確認してください。')}"
    )


def format_health(ollama, required_model=None):
    if not ollama.get("available"):
        return "🔴 Ollama: 未起動 — 高品質字幕分割とハイライト抽出に必要です。"
    models = ollama.get("models", [])
    if required_model and required_model not in models:
        return f"🟠 Ollama: 起動中 / モデル不足 `{required_model}` — `ollama pull {required_model}`"
    return f"🟢 Ollama: 正常 / モデル {len(models)}件"
