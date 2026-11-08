import os
import json
import gc
import subprocess
import time
from typing import Callable, Literal

from runtime import configure_cuda_dll_paths

configure_cuda_dll_paths()

from faster_whisper import WhisperModel

from job_control import raise_if_cancelled


AccelerationMode = Literal["auto", "gpu", "cpu"]
CUDA_COMPUTE_TYPE = "float16"
CPU_COMPUTE_TYPE = "int8"


class WhisperAccelerationError(RuntimeError):
    """Raised when the requested Whisper acceleration policy cannot be met."""

    def __init__(self, message: str, *, code: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


def _validate_acceleration_mode(mode: str) -> AccelerationMode:
    if mode not in {"auto", "gpu", "cpu"}:
        raise ValueError("acceleration_mode must be auto, gpu, or cpu")
    return mode  # type: ignore[return-value]


def _cuda_device_count() -> int:
    try:
        import ctranslate2

        return max(0, int(ctranslate2.get_cuda_device_count()))
    except Exception:
        return 0


def _runtime_diagnostic(
    *,
    device: str,
    compute_type: str,
    model: str,
    cuda_available: bool,
    fallback_reason: str | None,
) -> dict:
    return {
        "device": device,
        "computeType": compute_type,
        "model": model,
        "cudaAvailable": bool(cuda_available),
        "fallbackReason": fallback_reason or "none",
    }


def _cuda_fingerprint() -> str:
    """Return a path-free identity for the currently visible CUDA devices."""

    count = _cuda_device_count()
    visible = os.environ.get("CUDA_VISIBLE_DEVICES", "<default>")
    uuids = "<unavailable>"
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
        if completed.returncode == 0:
            uuids = ",".join(line.strip() for line in completed.stdout.splitlines() if line.strip()) or "<none>"
    except (OSError, subprocess.SubprocessError):
        pass
    return f"count={count};visible={visible[:128]};uuids={uuids[:512]}"


class WhisperModelCache:
    """Process-local model cache with bounded lifetime and GPU invalidation."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        gpu_fingerprint: Callable[[], str] = _cuda_fingerprint,
        loader: Callable[..., tuple[object, dict]] | None = None,
    ):
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))
        self._clock = clock
        self._gpu_fingerprint = gpu_fingerprint
        self._loader = loader or load_whisper_model
        self._model: object | None = None
        self._key: tuple[str, str, str] | None = None
        self._diagnostics: dict | None = None
        self._last_used = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def last_used(self) -> float:
        return self._last_used

    def _current_key(self, model_size: str, acceleration_mode: AccelerationMode) -> tuple[str, str, str]:
        fingerprint = "cpu"
        if acceleration_mode != "cpu":
            fingerprint = self._gpu_fingerprint()
        return model_size, acceleration_mode, fingerprint

    def get_or_load(self, model_size: str, acceleration_mode: AccelerationMode) -> tuple[object, dict]:
        now = self._clock()
        self.release_if_idle(now=now)
        requested_key = self._current_key(model_size, acceleration_mode)
        if self._model is not None and self._key == requested_key and self._diagnostics is not None:
            self._last_used = now
            diagnostics = dict(self._diagnostics)
            diagnostics["cacheHit"] = True
            return self._model, diagnostics

        self.clear()
        model, diagnostics = self._loader(model_size, acceleration_mode=acceleration_mode)
        actual_device = str(diagnostics.get("device", "unknown"))
        actual_fingerprint = self._gpu_fingerprint() if actual_device == "cuda" else "cpu"
        self._model = model
        self._key = (model_size, acceleration_mode, actual_fingerprint)
        self._diagnostics = dict(diagnostics)
        self._last_used = now
        diagnostics = dict(diagnostics)
        diagnostics["cacheHit"] = False
        return model, diagnostics

    def release_if_idle(self, *, now: float | None = None) -> bool:
        if self._model is None:
            return False
        current = self._clock() if now is None else now
        if self.idle_timeout_seconds > 0 and current - self._last_used < self.idle_timeout_seconds:
            return False
        self.clear()
        return True

    def clear(self) -> None:
        self._model = None
        self._key = None
        self._diagnostics = None
        self._last_used = 0.0
        gc.collect()


def _load_cpu_model(
    model_size: str,
    model_factory: Callable[..., object],
    *,
    cuda_available: bool | None = None,
) -> tuple[object, dict]:
    try:
        model = model_factory(model_size, device="cpu", compute_type=CPU_COMPUTE_TYPE)
    except Exception as exc:
        raise WhisperAccelerationError(
            "CPU Whisper model could not be loaded.", code="CPU_MODEL_LOAD_FAILED"
        ) from exc
    return model, _runtime_diagnostic(
        device="cpu",
        compute_type=CPU_COMPUTE_TYPE,
        model=model_size,
        cuda_available=_cuda_device_count() > 0 if cuda_available is None else cuda_available,
        fallback_reason="explicit_cpu",
    )


def load_whisper_model(
    model_size: str = "large-v3",
    *,
    acceleration_mode: AccelerationMode = "auto",
    model_factory: Callable[..., object] = WhisperModel,
    cuda_device_count: Callable[[], int] = _cuda_device_count,
) -> tuple[object, dict]:
    """Run a real CUDA health check and load the model under an explicit policy.

    Model construction is the health check: faster-whisper loads the model at
    construction time, so a successful CUDA construction is stronger evidence
    than a configuration flag alone.  The factory and device counter are
    injectable for unit tests without invoking a real model.
    """

    mode = _validate_acceleration_mode(acceleration_mode)
    if mode == "cpu":
        return _load_cpu_model(model_size, model_factory)

    try:
        available = int(cuda_device_count()) > 0
    except Exception:
        available = False
    if not available:
        if mode == "gpu":
            raise WhisperAccelerationError(
                "GPU acceleration is unavailable; explicit gpu mode forbids CPU fallback.",
                code="GPU_UNAVAILABLE",
                diagnostics=_runtime_diagnostic(
                    device="unavailable",
                    compute_type=CUDA_COMPUTE_TYPE,
                    model=model_size,
                    cuda_available=False,
                    fallback_reason="cuda_unavailable",
                ),
            )
        model, diagnostics = _load_cpu_model(model_size, model_factory, cuda_available=False)
        diagnostics["fallbackReason"] = "cuda_unavailable"
        return model, diagnostics

    try:
        model = model_factory(model_size, device="cuda", compute_type=CUDA_COMPUTE_TYPE)
    except Exception as exc:
        if mode == "gpu":
            raise WhisperAccelerationError(
                "GPU Whisper model could not be loaded; explicit gpu mode forbids CPU fallback.",
                code="GPU_MODEL_LOAD_FAILED",
                diagnostics=_runtime_diagnostic(
                    device="cuda",
                    compute_type=CUDA_COMPUTE_TYPE,
                    model=model_size,
                    cuda_available=True,
                    fallback_reason="cuda_model_load_failed",
                ),
            ) from exc
        model, diagnostics = _load_cpu_model(model_size, model_factory, cuda_available=True)
        diagnostics["cudaAvailable"] = True
        diagnostics["fallbackReason"] = "cuda_model_load_failed"
        return model, diagnostics
    return model, _runtime_diagnostic(
        device="cuda",
        compute_type=CUDA_COMPUTE_TYPE,
        model=model_size,
        cuda_available=True,
        fallback_reason=None,
    )


def transcribe_video(
    video_path: str,
    model_size: str = "large-v3",
    output_json: str = None,
    *,
    language: str = "ja",
    progress_callback=None,
    cancel_event=None,
    acceleration_mode: AccelerationMode = "auto",
    diagnostics_callback: Callable[[dict], None] | None = None,
    model_cache: WhisperModelCache | None = None,
) -> list:
    """
    Transcribes the video using faster-whisper.
    Returns a list of segment data with word-level timestamps and saves to JSON if path provided.
    """
    print(f"Loading faster-whisper model '{model_size}' with acceleration '{acceleration_mode}'...")
    if model_cache is None:
        model, runtime_diagnostics = load_whisper_model(model_size, acceleration_mode=acceleration_mode)
    else:
        model, runtime_diagnostics = model_cache.get_or_load(model_size, acceleration_mode)
    if diagnostics_callback:
        diagnostics_callback(runtime_diagnostics)
    
    print(f"Transcribing {video_path}...")
    # Set word_timestamps=True to get exact timings for jumpcuts and captions
    # Set vad_filter=True to ignore long silences internally
    segments, info = model.transcribe(
        video_path, 
        beam_size=5, 
        word_timestamps=True, 
        language=language,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500)
    )
    
    print(f"Detected language '{info.language}' with probability {info.language_probability}")
    
    result_segments = []
    for segment in segments:
        raise_if_cancelled(cancel_event)
        words = []
        if segment.words:
            for word in segment.words:
                words.append({
                    "start": word.start,
                    "end": word.end,
                    "word": word.word,
                    "probability": word.probability
                })
        
        result_segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
            "words": words
        })
        if progress_callback:
            duration = max(float(getattr(info, "duration", 0) or 0), 0.001)
            fraction = min(1.0, float(segment.end) / duration)
            progress_callback(fraction, f"文字起こし {fraction * 100:.1f}%")

    if not any(segment["words"] for segment in result_segments):
        raise ValueError(
            "発話を検出できませんでした。発話のある区間を選ぶか、入力動画を確認してください。"
        )
        
    if output_json:
        os.makedirs(os.path.dirname(output_json) or '.', exist_ok=True)
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(result_segments, f, ensure_ascii=False, indent=2)
        print(f"Saved transcription to {output_json}")
    if progress_callback:
        progress_callback(1.0, "文字起こし完了")
        
    return result_segments

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        video_path = sys.argv[1]
        out_json = sys.argv[2] if len(sys.argv) > 2 else "data/temp/transcript.json"
        transcribe_video(video_path, output_json=out_json)
    else:
        print("Usage: python transcribe.py <video_path> [output_json]")
