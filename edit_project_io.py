"""Atomic JSON IO for the edit_project/v1 contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath

from edit_project import require_valid_edit_project


def _validate_portable_source_paths(project: dict) -> None:
    for index, media in enumerate(project.get("source_media", [])):
        path = media.get("path") if isinstance(media, dict) else None
        if not isinstance(path, str) or not path.strip():
            continue
        if "\\" in path or PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ValueError(
                f"source_media[{index}].path must be a normalized relative path when writing edit_project"
            )


def write_edit_project(path: str | os.PathLike, project: dict) -> Path:
    """Validate and atomically publish a deterministic JSON project."""

    require_valid_edit_project(project)
    _validate_portable_source_paths(project)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
                project,
                file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_edit_project(path: str | os.PathLike) -> dict:
    """Load and validate an edit_project/v1 JSON file."""

    target = Path(path)
    with target.open("r", encoding="utf-8") as file:
        project = json.load(file)
    return require_valid_edit_project(project)
