# Unified Acceleration UX

## User setting

The legacy Spectra UI exposes one processing-device preference:

- `auto`: automatic selection, GPU preferred with safe fallback
- `gpu`: require GPU Whisper and GPU-capable legacy FFmpeg render; no silent CPU fallback
- `cpu`: explicit CPU Whisper and `libx264`/software decode for the legacy render path

The preference is stored atomically as `processing_preferences.json` below the platform userData directory (`%APPDATA%/Spectra` on Windows, or the platform equivalent). `SPECTRA_USER_DATA_DIR` is available for isolated tests. The value is a policy (`auto/gpu/cpu`), not a machine-specific backend name.

No `h264_nvenc`, CUDA device identity, local path, or other machine-specific backend is written into a ProjectFile. Existing job manifests may retain the user policy needed to resume that run; the backend is resolved again at execution time.

## Runtime status

The UI shows:

- GPU: available / unavailable
- Whisper: CUDA / CPU
- subtitle AI: GPU / mixed / CPU / unavailable
- Render: NVENC / CPU

The status is path-free. NVENC is based on an actual one-frame FFmpeg probe, CUDA availability is based on the CTranslate2 runtime, and subtitle-AI placement is read from Ollama's official runtime endpoint when available.

## Enforcement boundary

For `gpu`, faster-whisper uses explicit `gpu` mode, legacy `clip.py` uses explicit `h264_nvenc` and CUDA decode, and subtitle generation fails if the LLM is not GPU-resident. For `auto`, the existing safe fallback policy remains. For `cpu`, Whisper and the legacy FFmpeg path are explicit CPU; Ollama placement remains Ollama-managed and is shown as an observed runtime status because changing a running Ollama server's placement is not a portable worker contract.

The v2 production renderer remains an external `video_editor` process. `edit_project.v1` does not carry this machine policy or a fixed encoder; renderer-specific encoder selection remains inside that external renderer boundary.
