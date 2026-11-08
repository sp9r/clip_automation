"""Safe external-process bridge to the standalone video_editor CLI.

This module intentionally knows only the public CLI contract.  It does not
import video_editor code or inspect its internal TypeScript modules.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from job_control import PipelineCancelled


class VideoEditorRunnerError(RuntimeError):
    """Base class for renderer discovery and execution errors."""


class VideoEditorNotFoundError(VideoEditorRunnerError):
    pass


class VideoEditorNotBuiltError(VideoEditorRunnerError):
    pass


class VideoEditorRenderError(VideoEditorRunnerError):
    pass


class RenderOutputValidationError(VideoEditorRunnerError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(path: Path, limit: int = 16 * 1024) -> str:
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - limit))
            return file.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _executable_available(value: str) -> bool:
    candidate = Path(value)
    return candidate.is_file() or shutil.which(value) is not None


def _node_binary(explicit: str | None, env: Mapping[str, str]) -> str:
    candidate = explicit or env.get("NODE_BINARY") or env.get("NODE_EXE") or shutil.which("node")
    if not candidate:
        raise VideoEditorNotFoundError("Node executable was not found; set NODE_BINARY or install Node.js")
    return candidate


def _as_command(value: Sequence[str], source: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not value:
        raise VideoEditorRunnerError(f"{source} must be a non-empty JSON/argv array")
    command = tuple(str(item) for item in value)
    if any(not item for item in command):
        raise VideoEditorRunnerError(f"{source} contains an empty argv item")
    return command


def _infer_root(command: Sequence[str]) -> Path | None:
    if len(command) < 2:
        return None
    script = Path(command[1])
    if script.name != "video-editor.mjs" or script.parent.name != "scripts":
        return None
    return script.resolve().parent.parent


@dataclass(frozen=True)
class VideoEditorConfig:
    command: tuple[str, ...]
    source: str
    video_editor_root: Path | None = None
    require_built_artifact: bool = True

    @property
    def provenance(self) -> dict[str, Any]:
        root = self.video_editor_root
        package_version: str | None = None
        git_commit: str | None = None
        wrapper_sha256: str | None = None
        node_version: str | None = None
        if root:
            package_path = root / "package.json"
            try:
                version = json.loads(package_path.read_text(encoding="utf-8")).get("version")
                package_version = version if isinstance(version, str) else None
            except (OSError, ValueError, TypeError):
                package_version = None
            wrapper = root / "scripts" / "video-editor.mjs"
            try:
                wrapper_sha256 = _sha256_file(wrapper)
            except OSError:
                wrapper_sha256 = None
            try:
                completed = subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    check=False,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=5,
                )
                if completed.returncode == 0:
                    git_commit = completed.stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                git_commit = None
        try:
            completed = subprocess.run(
                [self.command[0], "--version"],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                timeout=5,
            )
            if completed.returncode == 0:
                node_version = (completed.stdout or completed.stderr).strip() or None
        except (OSError, subprocess.SubprocessError):
            node_version = None
        return {
            "source": self.source,
            "argv_prefix": list(self.command),
            "package_version": package_version,
            "git_commit": git_commit,
            "wrapper_sha256": wrapper_sha256,
            "node_version": node_version,
        }


def resolve_video_editor_config(
    *,
    explicit_command: Sequence[str] | None = None,
    video_editor_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    node_binary: str | None = None,
    env: Mapping[str, str] | None = None,
    require_built_artifact: bool = True,
) -> VideoEditorConfig:
    """Resolve the public renderer command without shell-string parsing."""

    environment = env or os.environ
    source: str
    root: Path | None = None
    if explicit_command is not None:
        command = _as_command(explicit_command, "explicit_command")
        source = "explicit"
        root = _infer_root(command)
    elif environment.get("VIDEO_EDITOR_COMMAND_JSON"):
        try:
            value = json.loads(environment["VIDEO_EDITOR_COMMAND_JSON"])
        except json.JSONDecodeError as error:
            raise VideoEditorRunnerError("VIDEO_EDITOR_COMMAND_JSON must be valid JSON array") from error
        command = _as_command(value, "VIDEO_EDITOR_COMMAND_JSON")
        source = "env"
        root = _infer_root(command)
    else:
        configured_root = video_editor_root or environment.get("VIDEO_EDITOR_ROOT")
        if configured_root:
            root = Path(configured_root).expanduser().resolve()
            source = "root"
        else:
            sibling = (Path(repo_root).resolve().parent / "video_editor") if repo_root else None
            if sibling and (sibling / "scripts" / "video-editor.mjs").is_file():
                root = sibling
                source = "sibling"
            else:
                raise VideoEditorNotFoundError(
                    "video_editor was not found; set VIDEO_EDITOR_COMMAND_JSON or VIDEO_EDITOR_ROOT"
                )
        command = (_node_binary(node_binary, environment), str(root / "scripts" / "video-editor.mjs"))
    return VideoEditorConfig(
        command=command,
        source=source,
        video_editor_root=root,
        require_built_artifact=require_built_artifact,
    )


@dataclass(frozen=True)
class RenderInvocation:
    clip_id: str
    edit_project_path: Path
    output_path: Path
    staging_path: Path
    stdout_log_path: Path
    stderr_log_path: Path
    cancel_event: Any | None = None


@dataclass(frozen=True)
class RenderResult:
    clip_id: str
    state: str
    exit_code: int | None
    output_path: Path
    output_sha256: str | None
    output_size: int
    stdout_json: dict[str, Any] | None
    stderr_tail: str
    error: str | None


PopenFactory = Callable[..., Any]


class VideoEditorRunner:
    def __init__(self, config: VideoEditorConfig, *, popen_factory: PopenFactory = subprocess.Popen):
        self.config = config
        self._popen_factory = popen_factory

    def preflight(self) -> None:
        executable = self.config.command[0] if self.config.command else ""
        if not executable or not _executable_available(executable):
            raise VideoEditorNotFoundError(f"external renderer executable was not found: {executable}")

        root = self.config.video_editor_root or _infer_root(self.config.command)
        strict_wrapper = self.config.source in {"root", "sibling"}
        known_wrapper = _infer_root(self.config.command) is not None
        if strict_wrapper or known_wrapper:
            if len(self.config.command) < 2:
                raise VideoEditorNotFoundError(
                    "video_editor root/sibling command must include scripts/video-editor.mjs"
                )
            script = Path(self.config.command[1])
            if script.name != "video-editor.mjs" or script.parent.name != "scripts" or not script.is_file():
                raise VideoEditorNotFoundError(f"video_editor launcher was not found: {script}")
        if self.config.require_built_artifact and root:
            artifact = root / "out" / "cli" / "video-editor.js"
            if not artifact.is_file():
                raise VideoEditorNotBuiltError(
                    f"video_editor is not built: {artifact}. Build video_editor before rendering."
                )

    def render(self, invocation: RenderInvocation) -> RenderResult:
        self.preflight()
        edit_project_path = invocation.edit_project_path.resolve()
        invocation.output_path.parent.mkdir(parents=True, exist_ok=True)
        invocation.stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
        invocation.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        invocation.staging_path.unlink(missing_ok=True)
        argv = [
            *self.config.command,
            "render",
            str(edit_project_path),
            "-o",
            str(invocation.staging_path),
        ]
        exit_code: int | None = None
        try:
            with invocation.stdout_log_path.open("wb") as stdout, invocation.stderr_log_path.open("wb") as stderr:
                process = self._popen_factory(
                    argv,
                    cwd=str(self.config.video_editor_root) if self.config.video_editor_root else None,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
                if invocation.cancel_event is None:
                    exit_code = int(process.wait())
                else:
                    while True:
                        polled = process.poll()
                        if polled is not None:
                            exit_code = int(polled)
                            break
                        if invocation.cancel_event.is_set():
                            process.terminate()
                            try:
                                exit_code = int(process.wait(timeout=2.0))
                            except subprocess.TimeoutExpired:
                                process.kill()
                                exit_code = int(process.wait())
                            invocation.staging_path.unlink(missing_ok=True)
                            raise PipelineCancelled("動画レンダリングを停止しました。")
                        invocation.cancel_event.wait(0.05)
        except OSError as error:
            invocation.staging_path.unlink(missing_ok=True)
            raise VideoEditorRenderError(self._error_message(invocation, None, str(error))) from error

        stderr_tail = _tail(invocation.stderr_log_path)
        stdout_json: dict[str, Any] | None = None
        try:
            stdout_bytes = invocation.stdout_log_path.read_bytes()
            if len(stdout_bytes) > 1024 * 1024:
                raise ValueError("stdout JSON exceeds 1 MiB")
            parsed = json.loads(stdout_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("stdout JSON must be an object")
            stdout_json = parsed
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            return self._failed(invocation, exit_code, stderr_tail, f"stdout JSON validation failed: {error}")

        if exit_code != 0:
            return self._failed(invocation, exit_code, stderr_tail, f"renderer exited with code {exit_code}")
        try:
            stat = invocation.staging_path.stat()
            if not invocation.staging_path.is_file() or stat.st_size <= 0:
                raise RenderOutputValidationError("staging output is missing, not a regular file, or zero bytes")
            os.replace(invocation.staging_path, invocation.output_path)
            final_stat = invocation.output_path.stat()
            if not invocation.output_path.is_file() or final_stat.st_size <= 0:
                raise RenderOutputValidationError("promoted output is missing or zero bytes")
            return RenderResult(
                clip_id=invocation.clip_id,
                state="completed",
                exit_code=exit_code,
                output_path=invocation.output_path,
                output_sha256=_sha256_file(invocation.output_path),
                output_size=final_stat.st_size,
                stdout_json=stdout_json,
                stderr_tail=stderr_tail,
                error=None,
            )
        except (OSError, RenderOutputValidationError) as error:
            return self._failed(invocation, exit_code, stderr_tail, str(error), stdout_json=stdout_json)

    @staticmethod
    def _error_message(invocation: RenderInvocation, exit_code: int | None, message: str) -> str:
        return f"clip_id={invocation.clip_id} edit_project={invocation.edit_project_path} exit_code={exit_code}: {message}"

    def _failed(
        self,
        invocation: RenderInvocation,
        exit_code: int | None,
        stderr_tail: str,
        message: str,
        *,
        stdout_json: dict[str, Any] | None = None,
    ) -> RenderResult:
        invocation.staging_path.unlink(missing_ok=True)
        return RenderResult(
            clip_id=invocation.clip_id,
            state="failed",
            exit_code=exit_code,
            output_path=invocation.output_path,
            output_sha256=_sha256_file(invocation.output_path) if invocation.output_path.is_file() else None,
            output_size=invocation.output_path.stat().st_size if invocation.output_path.is_file() else 0,
            stdout_json=stdout_json,
            stderr_tail=stderr_tail,
            error=self._error_message(invocation, exit_code, message),
        )
