"""Persistent, disposable process wrapper for the Spectra caption worker.

The server keeps only a process-local Whisper model cache. Requests, media
paths, project paths, and results are never persisted by this module. The
process is intentionally disposable: a parent can terminate and restart it
after a crash or a stale timeout without sharing editor state.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, TextIO

from auto_caption_contract import AutoCaptionContractError, parse_request
from spectra_caption_worker import (
    PROGRESS_VERSION,
    WorkerCancelled,
    WorkerFailure,
    run_worker,
)
from transcribe import AccelerationMode, WhisperModelCache


SERVER_EVENT_VERSION = "spectra.auto-caption.worker-event.v1"
CONTROL_VERSION = "spectra.auto-caption.control.v1"


class _EventWriter:
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.lock = threading.Lock()

    def write(self, payload: dict) -> None:
        with self.lock:
            self.stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.stream.flush()

    def progress_stream(self, job_id: str) -> TextIO:
        return _ProgressStream(self, job_id)


class _ProgressStream:
    def __init__(self, writer: _EventWriter, job_id: str):
        self.writer = writer
        self.job_id = job_id

    def write(self, text: str) -> int:
        for line in text.splitlines():
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.writer.write({
                "version": SERVER_EVENT_VERSION,
                "type": "progress",
                "jobId": self.job_id,
                "payload": payload,
            })
        return len(text)

    def flush(self) -> None:
        return None


def _safe_error(code: str) -> dict:
    return {
        "version": SERVER_EVENT_VERSION,
        "type": "error",
        "errorCode": code,
        "message": "自動字幕workerで処理に失敗しました。",
    }


def run_server(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    idle_timeout_seconds: float = 900.0,
    job_timeout_seconds: float = 1800.0,
    worker_runner: Callable[..., dict] = run_worker,
    model_cache: WhisperModelCache | None = None,
    clock: Callable[[], float] = time.monotonic,
    acceleration_mode: AccelerationMode = "auto",
) -> int:
    """Serve sequential JSONL jobs until stdin closes or a stale job occurs."""

    writer = _EventWriter(output_stream)
    cache = model_cache or WhisperModelCache(idle_timeout_seconds=idle_timeout_seconds, clock=clock)
    commands: queue.Queue[object] = queue.Queue()
    eof = object()

    def read_commands() -> None:
        try:
            for line in input_stream:
                try:
                    commands.put(json.loads(line))
                except json.JSONDecodeError:
                    commands.put({"_invalid": True})
        finally:
            commands.put(eof)

    reader = threading.Thread(target=read_commands, name="spectra-worker-input", daemon=True)
    reader.start()
    writer.write({"version": SERVER_EVENT_VERSION, "type": "lifecycle", "state": "started"})
    active: dict | None = None
    active_thread: threading.Thread | None = None
    active_cancel: threading.Event | None = None
    active_started = 0.0
    input_closed = False
    pending_requests: list[dict] = []

    def launch_job(request: dict) -> tuple[dict, threading.Thread, threading.Event, float]:
        cancel_event = threading.Event()

        def execute(job: dict = request, event: threading.Event = cancel_event) -> None:
            try:
                result = worker_runner(
                    job,
                    progress_stream=writer.progress_stream(job["jobId"]),
                    cancel_event=event,
                    model_cache=cache,
                    acceleration_mode=acceleration_mode,
                    **({"collect_caption_runtime": True} if worker_runner is run_worker else {}),
                )
                writer.write({
                    "version": SERVER_EVENT_VERSION,
                    "type": "result",
                    "jobId": job["jobId"],
                    "payload": result,
                })
            except WorkerCancelled:
                writer.write(_safe_error("CANCELLED") | {"jobId": job["jobId"]})
            except (WorkerFailure, AutoCaptionContractError, ValueError):
                writer.write(_safe_error("WORKER_FAILED") | {"jobId": job["jobId"]})
            except Exception:
                # Never expose arbitrary exception text or paths over the
                # machine-readable boundary. The process remains disposable.
                writer.write(_safe_error("WORKER_CRASH") | {"jobId": job["jobId"]})

        thread = threading.Thread(target=execute, name=f"spectra-worker-{request['jobId']}", daemon=True)
        thread.start()
        return request, thread, cancel_event, clock()

    while True:
        now = clock()
        cache.release_if_idle(now=now)

        if active_thread is not None and not active_thread.is_alive():
            active_thread.join(timeout=0)
            active = None
            active_thread = None
            active_cancel = None

        if active_thread is None and pending_requests:
            active, active_thread, active_cancel, active_started = launch_job(pending_requests.pop(0))

        if active_thread is not None and job_timeout_seconds > 0 and now - active_started >= job_timeout_seconds:
            if active_cancel is not None:
                active_cancel.set()
            writer.write(_safe_error("WORKER_TIMEOUT") | {"jobId": active.get("jobId", "unknown")})
            cache.clear()
            return 2

        if active_thread is None and input_closed and not pending_requests:
            cache.clear()
            writer.write({"version": SERVER_EVENT_VERSION, "type": "lifecycle", "state": "stopped"})
            return 0

        try:
            command = commands.get(timeout=0.05)
        except queue.Empty:
            continue

        if command is eof:
            input_closed = True
            continue
        if not isinstance(command, dict) or command.get("_invalid"):
            writer.write(_safe_error("MALFORMED_COMMAND"))
            continue

        command_type = command.get("type")
        if command.get("version") == CONTROL_VERSION:
            if command_type == "cancel":
                if active is not None and command.get("jobId") == active.get("jobId") and active_cancel is not None:
                    active_cancel.set()
                    writer.write({"version": SERVER_EVENT_VERSION, "type": "control", "jobId": active["jobId"], "state": "cancel_requested"})
                else:
                    writer.write(_safe_error("JOB_NOT_FOUND"))
                continue
            if command_type == "health":
                writer.write({
                    "version": SERVER_EVENT_VERSION,
                    "type": "health",
                    "cacheLoaded": cache.loaded,
                })
                continue
            writer.write(_safe_error("UNSUPPORTED_CONTROL"))
            continue

        try:
            request = parse_request(command)
        except AutoCaptionContractError:
            writer.write(_safe_error("INVALID_REQUEST"))
            continue
        if active_thread is not None or pending_requests:
            pending_requests.append(request)
            continue

        active, active_thread, active_cancel, active_started = launch_job(request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spectra persistent caption worker server")
    parser.add_argument("--idle-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--job-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--acceleration-mode", choices=("auto", "gpu", "cpu"), default="auto")
    args = parser.parse_args(argv)
    return run_server(
        sys.stdin,
        sys.stdout,
        idle_timeout_seconds=args.idle_timeout_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        acceleration_mode=args.acceleration_mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
