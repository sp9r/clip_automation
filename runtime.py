import os
import sys
from pathlib import Path


_DLL_DIRECTORY_HANDLES = []


def configure_cuda_dll_paths() -> list[str]:
    """Make CUDA 12 libraries installed in the venv discoverable on Windows."""
    if os.name != "nt":
        return []

    nvidia_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    candidates = [
        nvidia_root / "cublas" / "bin",
        nvidia_root / "cudnn" / "bin",
        nvidia_root / "cuda_nvrtc" / "bin",
    ]

    cuda_env_vars = sorted(
        (name for name in os.environ if name.startswith("CUDA_PATH_V12_")),
        reverse=True,
    )
    candidates.extend(Path(os.environ[name]) / "bin" for name in cuda_env_vars)

    configured = []
    current_path = os.environ.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []

    for candidate in candidates:
        if not candidate.is_dir():
            continue

        directory = str(candidate.resolve())
        if directory not in path_entries:
            path_entries.insert(0, directory)
        if hasattr(os, "add_dll_directory"):
            _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(directory))
        configured.append(directory)

    os.environ["PATH"] = os.pathsep.join(path_entries)
    return configured
