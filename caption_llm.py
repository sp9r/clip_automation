"""Opt-in, process-local transport for a loopback OpenAI-compatible caption LLM."""
import ipaddress
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_TOKENS = 4096
MAX_THINKING_TOKENS = 24000
CONFIG_FILE = Path(__file__).with_name("caption_llm.local.json")
_ENV_KEYS = {"base_url": "SPECTRA_CAPTION_LLM_BASE_URL", "model": "SPECTRA_CAPTION_LLM_MODEL", "api_key_file": "SPECTRA_CAPTION_LLM_API_KEY_FILE", "writer_thinking": "SPECTRA_CAPTION_LLM_WRITER_THINKING", "writer_style": "SPECTRA_CAPTION_LLM_WRITER_STYLE"}
# Set when the model failed to load, so later batches fall back immediately.
_unavailable = False


class CaptionLlmError(RuntimeError):
    """Messages are deliberately independent of credentials and response bodies."""


def _setting(name: str) -> str:
    """Environment first, then the untracked local profile beside the worker.

    The profile lets every launcher of the app use the local LLM; it holds a
    key file path, never the key itself.
    """
    value = os.environ.get(_ENV_KEYS[name], "").strip()
    if value or os.environ.get("SPECTRA_CAPTION_LLM_DISABLE_LOCAL_CONFIG") == "1":
        return value
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        value = data.get(name) if isinstance(data, dict) else None
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError):
        return ""


def configured() -> bool:
    return bool(_setting("base_url"))


def default_model(fallback: str = "qwen2.5:14b") -> str:
    return _setting("model") or fallback


def _configuration(model: str) -> tuple[str, str, str]:
    try:
        url = urllib.parse.urlsplit(_setting("base_url"))
        host = url.hostname
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        if not loopback or url.scheme not in {"http", "https"} or url.username or url.password or url.query or url.fragment:
            raise ValueError
        # Validate port now, rather than letting urllib expose a malformed URL.
        _ = url.port
        selected_model = default_model(model)
        if not selected_model or len(selected_model) > 256 or any(ord(char) < 32 for char in selected_model):
            raise ValueError
        key_file = _setting("api_key_file")
        if not key_file:
            raise ValueError
        key = Path(key_file).read_text(encoding="utf-8-sig").strip()
        if not key or len(key) > 8192 or any(ord(char) < 32 or ord(char) > 126 for char in key):
            raise ValueError
        return urllib.parse.urlunsplit(url).rstrip("/") + "/", selected_model, key
    except Exception:
        raise CaptionLlmError("Local caption LLM configuration is invalid or unavailable") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CaptionLlmError("Local caption LLM redirects are not allowed")


def _open_request(request: urllib.request.Request, timeout: float):
    # Redirects must never forward the local endpoint's bearer token elsewhere.
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _request_json(base: str, key: str, route: str, timeout: float, payload: dict | None = None) -> dict:
    try:
        request = urllib.request.Request(
            base + route,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        with _open_request(request, timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except Exception:
        # HTTP errors may include echoed headers, credential paths or server text.
        raise CaptionLlmError("Local caption LLM request failed") from None


def writer_style() -> str:
    """字幕ライターの本文の作り方。correct(既定、聞き間違いを直す) か clean(消すだけ)。"""
    value = (_setting("writer_style") if "writer_style" in _ENV_KEYS else "").strip().lower()
    return value if value in {"correct", "clean"} else "correct"


def writer_thinking() -> bool:
    """字幕ライターで思考ありを使うか（既定: 使う）。"""
    value = _setting("writer_thinking") if "writer_thinking" in _ENV_KEYS else ""
    return value.lower() not in {"0", "false", "no", "off"}


def generate(prompt: str, model: str, timeout: int, *, thinking: bool = False) -> str:
    if _unavailable:
        raise CaptionLlmError("Local caption LLM could not be loaded") from None
    base, selected_model, key = _configuration(model)
    if thinking:
        # 思考ありは考える分だけ出力が長い。JSONの文法制約は推論と相性が悪いので付けない。
        payload = {
            "model": selected_model,
            "stream": False,
            "temperature": 0.6,
            "top_p": 0.95,
            "max_tokens": MAX_THINKING_TOKENS,
            "chat_template_kwargs": {"enable_thinking": True},
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        payload = {
            "model": selected_model,
            "stream": False,
            "temperature": 0.1,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": prompt}],
        }
    result = _request_json(base, key, "chat/completions", timeout, payload)
    try:
        choice = result["choices"][0]
        content = choice["message"]["content"]
        if choice.get("finish_reason") == "length" or not isinstance(content, str) or not content.strip():
            raise ValueError
        return content
    except (KeyError, IndexError, TypeError, ValueError):
        raise CaptionLlmError("Local caption LLM returned an invalid or truncated completion") from None


def _router_root(base: str) -> str:
    # llama.cpp router management routes live beside /v1, not under it.
    return base[: -len("v1/")] if base.endswith("/v1/") else base


def _router_status(base: str, key: str, model: str, timeout: float) -> str | None:
    """Return the router's load state for model, or None for a non-router server."""
    payload = _request_json(_router_root(base), key, "models", timeout)
    for item in payload.get("data") or []:
        if isinstance(item, dict) and item.get("id") == model:
            status = item.get("status")
            value = status.get("value") if isinstance(status, dict) else None
            if isinstance(value, str):
                return "failed" if status.get("failed") is True and value == "unloaded" else value
            return None
    return None


def release_gpu(model: str, timeout: float = 60.0, *, sleep=time.sleep, clock=time.monotonic) -> str:
    """Unload the caption model so Whisper can use the whole GPU first.

    Returns "unloaded", "not-loaded", "not-router" or "unavailable". Never raises:
    a failed unload only means Whisper may fall back to CPU.
    """
    try:
        base, selected_model, key = _configuration(model)
        status = _router_status(base, key, selected_model, 5.0)
        if status is None:
            return "not-router"
        if status in {"unloaded", "failed"}:
            return "not-loaded"
        _request_json(_router_root(base), key, "models/unload", 10.0, {"model": selected_model})
        deadline = clock() + timeout
        while clock() < deadline:
            if _router_status(base, key, selected_model, 5.0) in {"unloaded", "failed"}:
                return "unloaded"
            sleep(1.0)
    except CaptionLlmError:
        pass
    return "unavailable"


def ensure_loaded(model: str, timeout: float = 300.0, *, sleep=time.sleep, clock=time.monotonic) -> None:
    """Load the caption model before the first prompt.

    Loading Qwen3.6 takes tens of seconds; waiting here keeps that out of the
    per-request generation timeout.
    """
    global _unavailable
    _unavailable = False
    try:
        base, selected_model, key = _configuration(model)
        status = _router_status(base, key, selected_model, 5.0)
        if status is None or status == "loaded":
            return
        if status != "loading":
            try:
                _request_json(_router_root(base), key, "models/load", 10.0, {"model": selected_model})
            except CaptionLlmError:
                # A concurrent request may have started the load already.
                pass
        requested = clock()
        deadline = requested + timeout
        while clock() < deadline:
            status = _router_status(base, key, selected_model, 5.0)
            if status == "loaded":
                return
            # The router may report the previous state until the child starts.
            if status in {"failed", "unloaded"} and clock() - requested > 15.0:
                break
            sleep(2.0)
    except CaptionLlmError:
        pass
    _unavailable = True
    raise CaptionLlmError("Local caption LLM could not be loaded") from None


def runtime_diagnostic(model: str, timeout: float = 2.0) -> dict:
    selected_model = default_model(model)
    result = {"status": "unavailable", "model": selected_model, "backend": "openai-compatible", "source": "openai_models", "acceleration": "unmeasured"}
    try:
        base, selected_model, key = _configuration(model)
        payload = _request_json(base, key, "models", timeout)
        if not isinstance(payload.get("data"), list) or not any(isinstance(item, dict) and item.get("id") == selected_model for item in payload["data"]):
            result["reason"] = "configured_model_not_advertised"
            return result
        result.update(status="available", model=selected_model, reason="gpu_state_unmeasured")
    except CaptionLlmError:
        result["reason"] = "local_caption_api_unavailable"
    return result
