"""CLI entry point for Phase 5 headless rendering of a planning directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from render_pipeline import render_planning_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render independent edit_project.v1 clips with video_editor")
    parser.add_argument("planning_dir", type=Path)
    parser.add_argument("--video-editor-root", type=Path, default=None)
    parser.add_argument("--video-editor-command-json", default=None, help="JSON argv prefix, e.g. [node, scripts/video-editor.mjs]")
    args = parser.parse_args(argv)
    explicit_command = None
    if args.video_editor_command_json:
        try:
            value = json.loads(args.video_editor_command_json)
        except json.JSONDecodeError as error:
            parser.error(f"--video-editor-command-json must be a JSON array: {error}")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            parser.error("--video-editor-command-json must be a JSON string array")
        explicit_command = value
    try:
        result = render_planning_outputs(
            args.planning_dir,
            explicit_command=explicit_command,
            video_editor_root=args.video_editor_root,
            repo_root=Path(__file__).resolve().parent,
        )
    except Exception as error:
        print(json.dumps({"state": "failed", "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result.manifest["summary"]["failed"] or result.manifest["summary"]["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
