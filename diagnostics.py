import shutil
from pathlib import Path

from caption import get_ollama_runtime_diagnostic
from clip import nvenc_diagnostic
from ollama_service import check_ollama
from processing_device import resolve_processing_policy, validate_processing_device
from transcribe import _cuda_device_count


def normalize_local_path(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return Path(text).expanduser().resolve()


def _error(code, message, action, *, stage="preflight", retryable=False):
    return {
        "error_code": code, "message": message, "stage": stage,
        "retryable": retryable, "user_action": action,
    }


def run_preflight(source, *, is_local, require_ollama, required_model=None):
    errors = []
    checks = {}
    normalized_source = str(source or "")
    if is_local:
        path = normalize_local_path(source)
        normalized_source = str(path)
        checks["input"] = path.is_file()
        if not path.is_file():
            errors.append(_error("INPUT_NOT_FOUND", f"入力動画が見つかりません: {path}", "引用符を外した正しいファイルパスを指定してください。"))
    for command in ("ffmpeg", "ffprobe"):
        available = shutil.which(command) is not None
        checks[command] = available
        if not available:
            errors.append(_error(f"{command.upper()}_NOT_FOUND", f"{command}が見つかりません。", f"{command}をインストールしてPATHを確認してください。"))
    ollama = check_ollama()
    checks["ollama"] = ollama
    if require_ollama:
        if not ollama["available"]:
            errors.append(_error("OLLAMA_UNAVAILABLE", "Ollamaが起動していません。", "Ollamaを起動して再確認するか、ルール方式を選択してください。", retryable=True))
        elif required_model and required_model not in ollama["models"]:
            errors.append(_error("OLLAMA_MODEL_MISSING", f"必要なモデルがありません: {required_model}", f"ollama pull {required_model} を実行してください。"))
    return {"ok": not errors, "source": normalized_source, "checks": checks, "errors": errors}


def get_acceleration_status(processing_device="auto", caption_model="qwen2.5:14b"):
    """Return path-free, user-facing runtime capability and selection status."""

    device = validate_processing_device(processing_device)
    policy = resolve_processing_policy(device)
    cuda_available = _cuda_device_count() > 0
    nvenc = nvenc_diagnostic()
    caption_ai = get_ollama_runtime_diagnostic(str(caption_model))
    gpu_available = bool(cuda_available and nvenc.get("available"))
    errors = []
    if device == "gpu" and not gpu_available:
        errors.append("GPU優先を利用できません。CUDAまたはNVENCが利用不可です。")
    if device == "gpu" and caption_ai.get("status") not in {"gpu", "unavailable"}:
        errors.append("字幕AIがGPUへ完全配置されていません。")
    return {
        "processing_device": device,
        "gpu_available": gpu_available,
        "whisper": "CPU" if device == "cpu" or not cuda_available else "CUDA",
        "caption_ai": caption_ai.get("status", "unavailable"),
        "render": "CPU" if device == "cpu" or not nvenc.get("available") else "NVENC",
        "caption_ai_runtime": caption_ai,
        "nvenc": nvenc,
        "policy": policy,
        "errors": errors,
    }
