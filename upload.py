"""Compatibility facade and CLI for explicit Phase 7 publishing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from publishing.credentials import CredentialPaths, authorize_youtube
from publishing.pipeline import prepare_publish_jobs, publish_prepared_jobs, refresh_processing_status


def _metadata(args) -> dict:
    return {
        "title": args.title,
        "description": args.description,
        "tags": [item for item in args.tags.split(",") if item.strip()],
        "privacy_status": args.privacy,
        "category_id": args.category_id,
    }


def _manifest_for(value: str | Path) -> Path:
    target = Path(value).resolve()
    if target.is_dir():
        target = target / "publishing" / "publish_manifest.v1.json"
    return target


def _mapping(items):
    result = {}
    for item in items or []:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise ValueError("mapping must use clip_id=value")
        result[key] = value
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit YouTube publishing foundation")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("auth", help="Authorize YouTube using desktop loopback OAuth + PKCE")
    auth.add_argument("--include-readonly", action="store_true", help=argparse.SUPPRESS)
    prepare = sub.add_parser("prepare", help="Prepare publish manifest without remote mutation")
    prepare.add_argument("run_dir")
    prepare.add_argument("--title", default=None)
    prepare.add_argument("--description", default="")
    prepare.add_argument("--tags", default="")
    prepare.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    prepare.add_argument("--category-id", default="22")
    prepare.add_argument("--thumbnail", action="append", default=[], help="clip_id=path (repeatable)")
    prepare.add_argument("--thumbnail-at", action="append", default=[], help="clip_id=seconds (repeatable)")
    publish = sub.add_parser("publish", help="Execute an explicit YouTube publish")
    publish.add_argument("manifest")
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument("--force-reupload", action="store_true")
    publish.add_argument("--confirm-changed-render", action="store_true", help="explicitly approve upload after rendered video hash changed")
    publish.add_argument("--wait-processing", action="store_true")
    status = sub.add_parser("status", help="Recheck processing status from a prepared manifest")
    status.add_argument("manifest")
    args = parser.parse_args(argv)
    if args.command == "auth":
        authorize_youtube(CredentialPaths.from_env(), include_readonly=True)
        print(json.dumps({"authorized": True}, ensure_ascii=False))
        return 0
    if args.command == "prepare":
        metadata = None
        if args.title is not None:
            metadata = {"default": _metadata(args)}
        timestamps = {key: float(value) for key, value in _mapping(args.thumbnail_at).items()}
        result = prepare_publish_jobs(
            args.run_dir,
            metadata_by_clip=metadata,
            thumbnail_by_clip=_mapping(args.thumbnail),
            thumbnail_timestamps=timestamps,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    manifest_path = _manifest_for(args.manifest)
    if args.command == "publish":
        result = publish_prepared_jobs(
            manifest_path, dry_run=args.dry_run, force_reupload=args.force_reupload,
            confirm_changed_render=args.confirm_changed_render,
            wait_processing=args.wait_processing,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("dry_run") or result.get("summary", {}).get("failed", 0) == 0 else 1
    result = refresh_processing_status(manifest_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("manifest", {}).get("summary", {}).get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
