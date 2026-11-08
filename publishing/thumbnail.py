"""Local thumbnail validation and deterministic frame extraction."""

from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .errors import PublishPreparationError


MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
SUPPORTED_MIME = {"image/jpeg", "image/png"}


def validate_thumbnail(path: str | Path) -> dict:
    target = Path(path).resolve()
    if not target.is_file() or target.stat().st_size <= 0:
        raise PublishPreparationError(f"thumbnail file is missing or empty: {target}")
    if target.stat().st_size > MAX_THUMBNAIL_BYTES:
        raise PublishPreparationError("thumbnail must be at most 2 MiB")
    mime = mimetypes.guess_type(target.name)[0]
    if mime not in SUPPORTED_MIME:
        raise PublishPreparationError("thumbnail must be JPEG or PNG")
    try:
        from PIL import Image
        with Image.open(target) as image:
            image.verify()
            width, height = image.size
    except Exception as error:
        raise PublishPreparationError("thumbnail image is invalid") from error
    return {"mime_type": mime, "width": width, "height": height, "size": target.stat().st_size}


def copy_thumbnail(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source).resolve()
    validate_thumbnail(source_path)
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source_path, temporary)
    os.replace(temporary, target)
    validate_thumbnail(target)
    return target


def extract_thumbnail(video_path: str | Path, timestamp: float, destination: str | Path) -> Path:
    if timestamp < 0:
        raise PublishPreparationError("thumbnail timestamp must be non-negative")
    target = Path(destination).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp.jpg")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(float(timestamp)),
        "-i", str(Path(video_path).resolve()), "-frames:v", "1", "-q:v", "2", str(temporary),
    ]
    completed = subprocess.run(command, shell=False, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise PublishPreparationError("FFmpeg thumbnail extraction failed")
    os.replace(temporary, target)
    validate_thumbnail(target)
    return target
