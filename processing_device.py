"""User-scoped processing-device preference and backend policy mapping."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Literal


ProcessingDevice = Literal["auto", "gpu", "cpu"]
DEFAULT_PROCESSING_DEVICE: ProcessingDevice = "auto"
PREFERENCES_SCHEMA_VERSION = 1
PREFERENCES_ENV = "SPECTRA_USER_DATA_DIR"


def validate_processing_device(value: str | None) -> ProcessingDevice:
    normalized = str(value or DEFAULT_PROCESSING_DEVICE).strip().lower()
    if normalized not in {"auto", "gpu", "cpu"}:
        raise ValueError("processing_device must be auto, gpu, or cpu")
    return normalized  # type: ignore[return-value]


def user_data_dir(base: str | os.PathLike | None = None) -> Path:
    """Return an app-owned userData directory, never the project directory."""

    if base is not None:
        return Path(base).expanduser().resolve()
    configured = os.environ.get(PREFERENCES_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (Path(root) / "Spectra").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Spectra").resolve()
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return (Path(root) / "spectra").resolve()


def preferences_path(base: str | os.PathLike | None = None) -> Path:
    return user_data_dir(base) / "processing_preferences.json"


def load_processing_device(path: str | os.PathLike | None = None) -> ProcessingDevice:
    target = Path(path) if path is not None else preferences_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != PREFERENCES_SCHEMA_VERSION:
            return DEFAULT_PROCESSING_DEVICE
        return validate_processing_device(data.get("processing_device"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_PROCESSING_DEVICE


def save_processing_device(
    value: str,
    path: str | os.PathLike | None = None,
) -> ProcessingDevice:
    normalized = validate_processing_device(value)
    target = Path(path) if path is not None else preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": PREFERENCES_SCHEMA_VERSION,
                "processing_device": normalized,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return normalized


def resolve_processing_policy(value: str | None) -> dict[str, object]:
    """Map the user policy to backend requests without storing machine backends."""

    device = validate_processing_device(value)
    if device == "gpu":
        return {
            "processing_device": device,
            "whisper_mode": "gpu",
            "render_encoder": "h264_nvenc",
            "decode_mode": "cuda",
            "requires_gpu": True,
        }
    if device == "cpu":
        return {
            "processing_device": device,
            "whisper_mode": "cpu",
            "render_encoder": "libx264",
            "decode_mode": "software",
            "requires_gpu": False,
        }
    return {
        "processing_device": device,
        "whisper_mode": "auto",
        "render_encoder": "auto",
        "decode_mode": "auto",
        "requires_gpu": False,
    }
