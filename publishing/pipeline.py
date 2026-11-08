"""Prepare rendered clips and execute explicit publishing actions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from render_manifest import load_manifest as load_render_manifest
from job_control import PipelineCancelled

from .errors import PublishPreparationError, PublishingError, redact_text
from .manifest import (
    SCHEMA_VERSION,
    _summary,
    atomic_write_manifest,
    load_manifest,
    portable_relative,
    resolve_relative,
    sha256_file,
    update_clip,
)
from .models import DEFAULT_CATEGORY_ID, validate_metadata
from .thumbnail import copy_thumbnail, extract_thumbnail, validate_thumbnail
from .youtube import Publisher, YouTubePublisher


def _default_metadata(clip_id: str) -> dict[str, Any]:
    return {
        "title": clip_id,
        "description": "",
        "tags": [],
        "privacy_status": "private",
        "category_id": DEFAULT_CATEGORY_ID,
    }


def _verify_render_source(planning_dir: Path, render_manifest_path: Path) -> dict[str, Any]:
    if not render_manifest_path.is_file():
        raise PublishPreparationError("render_manifest.v1.json is missing")
    render_manifest = load_render_manifest(render_manifest_path)
    for item in render_manifest.get("clips", []):
        if item.get("state") != "completed":
            continue
        output = item.get("output", {})
        output_path = (planning_dir / output["path"]).resolve()
        if not output_path.is_file() or not output.get("sha256") or sha256_file(output_path) != output["sha256"]:
            raise PublishPreparationError(f"render output hash mismatch: {item.get('clip_id')}")
    return render_manifest


def _empty_remote() -> dict[str, Any]:
    return {"video_id": None, "url": None, "processing_status": None, "channel_id": None, "channel_title": None}


def _merge_existing_clip(entry: dict[str, Any], existing: dict[str, Any] | None, *, explicit_thumbnail: bool) -> dict[str, Any]:
    """Rebuild local preparation while retaining durable remote identity."""
    if not existing:
        return entry
    if existing.get("platform", "youtube") != "youtube":
        raise PublishPreparationError(f"unsupported existing publish platform: {existing.get('platform')}")
    same_video = existing.get("video", {}).get("sha256") == entry["video"]["sha256"]
    existing_remote = dict(existing.get("remote") or {})
    if not same_video:
        if existing_remote.get("video_id"):
            entry["previous_remote"] = existing_remote
            entry["state"] = "needs_republish_confirmation"
            entry["error"] = {
                "type": "NeedsRepublishConfirmation",
                "message": "rendered video hash changed; explicit confirmation is required before new upload",
                "clip_id": entry["clip_id"],
            }
        return entry

    if existing.get("state") == "needs_republish_confirmation" and existing.get("previous_remote"):
        entry["previous_remote"] = dict(existing["previous_remote"])
        entry["state"] = "needs_republish_confirmation"
        entry["error"] = dict(existing.get("error") or {
            "type": "NeedsRepublishConfirmation",
            "message": "explicit confirmation is required before new upload",
            "clip_id": entry["clip_id"],
        })
        return entry

    # Remote identity is the durable external side effect. It is never replaced
    # by a newly generated local preparation for the same clip and video hash.
    if existing_remote.get("video_id"):
        entry["remote"] = existing_remote
        if existing.get("previous_remote"):
            entry["previous_remote"] = existing["previous_remote"]
        diagnostics = dict(existing.get("diagnostics") or {})
        if existing.get("metadata") != entry.get("metadata"):
            diagnostics["metadata_changed_after_upload"] = True
        if diagnostics:
            entry["diagnostics"] = diagnostics
        if not explicit_thumbnail and existing.get("thumbnail", {}).get("path"):
            entry["thumbnail"] = dict(existing["thumbnail"])
        old_thumbnail = existing.get("thumbnail", {})
        new_thumbnail = entry.get("thumbnail", {})
        thumbnail_changed = bool(new_thumbnail.get("sha256")) and new_thumbnail.get("sha256") != old_thumbnail.get("sha256")
        entry["state"] = "uploaded" if thumbnail_changed else existing.get("state", "uploaded")
        entry["error"] = None if not diagnostics else existing.get("error")
    return entry


def prepare_publish_jobs(
    run_dir: str | Path,
    *,
    platform: str = "youtube",
    metadata_by_clip: Mapping[str, Mapping[str, Any]] | None = None,
    thumbnail_by_clip: Mapping[str, str | Path] | None = None,
    thumbnail_timestamps: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    if platform != "youtube":
        raise PublishPreparationError("Phase 7 currently prepares only YouTube publishing")
    run_root = Path(run_dir).resolve()
    planning_dir = run_root / "planning"
    render_manifest_path = planning_dir / "render_manifest.v1.json"
    render_manifest = _verify_render_source(planning_dir, render_manifest_path)
    publishing_dir = run_root / "publishing"
    target = run_root / "publishing" / "publish_manifest.v1.json"
    existing_manifest = load_manifest(target) if target.is_file() else None
    if existing_manifest is not None and existing_manifest.get("platform") != platform:
        raise PublishPreparationError("existing publish manifest platform does not match")
    existing_by_clip = {item["clip_id"]: item for item in (existing_manifest or {}).get("clips", [])}
    entries = []
    current_clip_ids: set[str] = set()
    metadata_by_clip = metadata_by_clip or {}
    thumbnail_by_clip = thumbnail_by_clip or {}
    thumbnail_timestamps = thumbnail_timestamps or {}
    for render_clip in render_manifest.get("clips", []):
        if render_clip.get("state") != "completed":
            continue
        clip_id = str(render_clip["clip_id"])
        current_clip_ids.add(clip_id)
        video_path = (planning_dir / render_clip["output"]["path"]).resolve()
        metadata_input = metadata_by_clip.get(clip_id, metadata_by_clip.get("default", _default_metadata(clip_id)))
        metadata = validate_metadata(metadata_input).as_dict()
        clip_publish_dir = publishing_dir / clip_id
        thumbnail_path = None
        thumbnail_info = {"path": None, "sha256": None, "mime_type": None, "state": "skipped"}
        if clip_id in thumbnail_by_clip:
            source_thumbnail = Path(thumbnail_by_clip[clip_id]).expanduser().resolve()
            extension = source_thumbnail.suffix.lower() if source_thumbnail.suffix.lower() in {".jpg", ".jpeg", ".png"} else ".jpg"
            thumbnail_path = copy_thumbnail(source_thumbnail, clip_publish_dir / f"thumbnail{extension}")
        elif clip_id in thumbnail_timestamps:
            thumbnail_path = extract_thumbnail(video_path, float(thumbnail_timestamps[clip_id]), clip_publish_dir / "thumbnail.jpg")
        if thumbnail_path:
            info = validate_thumbnail(thumbnail_path)
            thumbnail_info = {
                "path": portable_relative(thumbnail_path, publishing_dir),
                "sha256": sha256_file(thumbnail_path),
                "mime_type": info["mime_type"],
                "state": "prepared",
            }
        entry = {
            "clip_id": clip_id,
            "state": "prepared",
            "video": {
                "path": portable_relative(video_path, publishing_dir),
                "sha256": sha256_file(video_path),
            },
            "metadata": metadata,
            "thumbnail": thumbnail_info,
            "remote": _empty_remote(),
            "error": None,
        }
        entries.append(_merge_existing_clip(entry, existing_by_clip.get(clip_id), explicit_thumbnail=(clip_id in thumbnail_by_clip or clip_id in thumbnail_timestamps)))

    # Do not silently drop a remote identity if a clip disappeared from the new
    # render manifest. Keep it auditable and non-publishable instead.
    for clip_id, existing in existing_by_clip.items():
        if clip_id in current_clip_ids or not existing.get("remote", {}).get("video_id"):
            continue
        orphan = dict(existing)
        orphan["state"] = "orphaned"
        orphan["error"] = {
            "type": "OrphanedRemoteClip",
            "message": "clip is absent from the current render manifest; remote identity retained and publishing disabled",
            "clip_id": clip_id,
        }
        entries.append(orphan)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "platform": "youtube",
        "source_render_manifest": {
            "path": portable_relative(render_manifest_path, publishing_dir),
            "sha256": sha256_file(render_manifest_path),
        },
        "clips": entries,
        "summary": {"total": 0, "prepared": 0, "uploaded": 0, "completed": 0, "failed": 0},
    }
    manifest["summary"] = _summary(entries)
    atomic_write_manifest(target, manifest)
    manifest["manifest_path"] = str(target)
    return manifest


def _save_state(path: Path, manifest: dict[str, Any], clip_id: str, **updates: Any) -> None:
    update_clip(manifest, clip_id, **updates)
    atomic_write_manifest(path, manifest)


def _status_value(status: Mapping[str, Any]) -> str:
    return str(status.get("processing_status") or status.get("status") or "unknown").lower()


def _error_dict(error: BaseException, *, clip_id: str, publisher: Publisher) -> dict[str, Any]:
    if isinstance(error, PublishingError):
        data = error.to_dict()
        data["clip_id"] = clip_id
        return data
    return {
        "type": type(error).__name__,
        "message": redact_text(error, getattr(publisher, "secret_values", ())),
        "clip_id": clip_id,
        "platform": "youtube",
        "retryable": False,
        "user_action": "manifestのerrorとYouTube API診断を確認してください。",
    }


def publish_prepared_jobs(
    manifest_path: str | Path,
    *,
    publisher: Publisher | None = None,
    dry_run: bool = False,
    force_reupload: bool = False,
    confirm_changed_render: bool = False,
    wait_processing: bool = False,
    processing_timeout: float = 0.0,
    processing_interval: float = 2.0,
    cancel_event=None,
    log_callback=None,
) -> dict[str, Any]:
    path = Path(manifest_path).resolve()
    manifest = load_manifest(path)
    publisher = publisher or YouTubePublisher(include_readonly=wait_processing)
    if dry_run:
        available = publisher.credentials_available()
        return {
            "dry_run": True,
            "credentials_available": available,
            "remote_calls": 0,
            "manifest_path": str(path),
            "planned_clips": [item["clip_id"] for item in manifest["clips"] if item["state"] in {"prepared", "uploaded", "processing_pending"}],
            "summary": manifest["summary"],
        }
    if not publisher.credentials_available() and getattr(publisher, "service", None) is None:
        raise PublishPreparationError("YouTube credentials are not available", user_action="先にauthを実行してください。")
    publishing_dir = path.parent
    run_root = publishing_dir.parent
    source = manifest["source_render_manifest"]
    source_path = resolve_relative(source["path"], publishing_dir)
    if not source_path.is_file() or sha256_file(source_path) != source["sha256"]:
        raise PublishPreparationError("source render manifest hash mismatch; publish is blocked")
    for item in manifest["clips"]:
        clip_id = item["clip_id"]
        if item["state"] == "orphaned":
            continue
        if item["state"] == "needs_republish_confirmation" and not confirm_changed_render:
            continue
        if cancel_event is not None and cancel_event.is_set():
            _save_state(path, manifest, clip_id, state="canceled", error={"type": "Canceled", "message": "publish canceled", "clip_id": clip_id})
            continue
        thumbnail_requires_work = bool(item["thumbnail"].get("path")) and item["thumbnail"].get("state") != "completed"
        if item["state"] == "completed" and not force_reupload and not thumbnail_requires_work:
            continue
        video_path = resolve_relative(item["video"]["path"], publishing_dir)
        if not video_path.is_file() or sha256_file(video_path) != item["video"]["sha256"]:
            _save_state(path, manifest, clip_id, state="failed", error={"type": "IntegrityError", "message": "rendered video hash mismatch", "clip_id": clip_id})
            continue
        remote = dict(item["remote"])
        try:
            video_id = remote.get("video_id")
            if not video_id or force_reupload:
                _save_state(path, manifest, clip_id, state="uploading", error=None)
                response = publisher.publish_video(video_path, item["metadata"], cancel_event=cancel_event)
                video_id = str(response["video_id"])
                remote.update({"video_id": video_id, "url": response.get("url") or f"https://www.youtube.com/watch?v={video_id}"})
                _save_state(path, manifest, clip_id, state="uploaded", remote=remote, error=None)
            else:
                # A persisted remote ID is already the durable result. Avoid an
                # unnecessary local atomic rewrite on the idempotent no-op path.
                if item["state"] != "uploaded" or item.get("error") is not None:
                    _save_state(path, manifest, clip_id, state="uploaded", remote=remote, error=None)
            thumbnail = item["thumbnail"]
            if thumbnail.get("path") and thumbnail.get("state") != "completed":
                thumbnail_path = resolve_relative(thumbnail["path"], publishing_dir)
                _save_state(path, manifest, clip_id, state="thumbnail_setting")
                try:
                    if not thumbnail_path.is_file() or sha256_file(thumbnail_path) != thumbnail.get("sha256"):
                        raise PublishPreparationError("thumbnail hash mismatch")
                    validate_thumbnail(thumbnail_path)
                    publisher.set_thumbnail(video_id, thumbnail_path)
                    thumbnail = dict(thumbnail, state="completed")
                    _save_state(path, manifest, clip_id, thumbnail=thumbnail, state="uploaded", error=None)
                except Exception as error:
                    thumbnail = dict(thumbnail, state="failed", error=_error_dict(error, clip_id=clip_id, publisher=publisher))
                    _save_state(path, manifest, clip_id, thumbnail=thumbnail, state="uploaded_with_thumbnail_error", remote=remote, error=thumbnail["error"])
            if wait_processing:
                deadline = time.monotonic() + max(0.0, processing_timeout)
                while True:
                    _save_state(path, manifest, clip_id, state="processing", remote=remote, error=None)
                    status = publisher.get_status(video_id)
                    remote.update({key: status.get(key) for key in ("processing_status", "channel_id", "channel_title") if status.get(key) is not None})
                    value = _status_value(status)
                    if value in {"succeeded", "success", "completed"}:
                        _save_state(path, manifest, clip_id, state="completed", remote=remote, error=None)
                        break
                    if value in {"failed", "rejected", "terminated"}:
                        raise PublishingError("YouTube processing failed", clip_id=clip_id, user_action="YouTubeのprocessing errorを確認してください。")
                    if time.monotonic() >= deadline:
                        _save_state(path, manifest, clip_id, state="processing_pending", remote=remote, error=None)
                        break
                    if cancel_event is not None and cancel_event.is_set():
                        _save_state(path, manifest, clip_id, state="canceled", remote=remote, error={"type": "Canceled", "message": "publish canceled", "clip_id": clip_id})
                        break
                    time.sleep(max(0.0, processing_interval))
            elif manifest["clips"][[c["clip_id"] for c in manifest["clips"]].index(clip_id)]["state"] not in {"uploaded", "uploaded_with_thumbnail_error"}:
                _save_state(path, manifest, clip_id, state="uploaded", remote=remote, error=None)
        except PipelineCancelled:
            _save_state(
                path, manifest, clip_id, state="canceled", remote=remote,
                error={"type": "Canceled", "message": "publish canceled", "clip_id": clip_id},
            )
        except Exception as error:
            _save_state(path, manifest, clip_id, state="failed", remote=remote, error=_error_dict(error, clip_id=clip_id, publisher=publisher))
        if log_callback:
            log_callback(f"clip_id={clip_id} state={next(c['state'] for c in manifest['clips'] if c['clip_id'] == clip_id)}")
    manifest = load_manifest(path)
    return {"dry_run": False, "manifest_path": str(path), "manifest": manifest, "summary": manifest["summary"]}


def refresh_processing_status(
    manifest_path: str | Path,
    *,
    publisher: Publisher | None = None,
    cancel_event=None,
) -> dict[str, Any]:
    """Refresh only already-uploaded videos; never uploads a prepared clip."""
    path = Path(manifest_path).resolve()
    manifest = load_manifest(path)
    publisher = publisher or YouTubePublisher(include_readonly=True)
    if not publisher.credentials_available() and getattr(publisher, "service", None) is None:
        raise PublishPreparationError("YouTube credentials are not available", user_action="先にauthを実行してください。")
    for item in manifest["clips"]:
        video_id = item.get("remote", {}).get("video_id")
        if not video_id:
            continue
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            status = publisher.get_status(video_id)
            remote = dict(item["remote"])
            remote.update({key: status.get(key) for key in ("processing_status", "channel_id", "channel_title") if status.get(key) is not None})
            value = _status_value(status)
            state = "completed" if value in {"succeeded", "success", "completed"} else "failed" if value in {"failed", "rejected", "terminated"} else "processing_pending"
            error = None if state != "failed" else {"type": "ProcessingError", "message": "YouTube processing failed", "clip_id": item["clip_id"]}
            _save_state(path, manifest, item["clip_id"], state=state, remote=remote, error=error)
        except Exception as error:
            _save_state(path, manifest, item["clip_id"], state="processing_pending", error=_error_dict(error, clip_id=item["clip_id"], publisher=publisher))
    return {"dry_run": False, "manifest_path": str(path), "manifest": load_manifest(path)}
