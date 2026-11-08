import json
import os
import subprocess
import time
import urllib.error
import urllib.request


OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"


def check_ollama(timeout=2.0):
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {
            "available": True,
            "models": [item.get("name", "") for item in data.get("models", []) if item.get("name")],
            "error_code": None,
            "message": "Ollamaは利用可能です。",
        }
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "models": [],
            "error_code": "OLLAMA_UNAVAILABLE",
            "message": f"Ollamaが起動していません: {exc}",
        }


def start_ollama(timeout=12.0):
    current = check_ollama()
    if current["available"]:
        return current
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = check_ollama(timeout=1.0)
        if state["available"]:
            return state
        time.sleep(0.4)
    return check_ollama(timeout=1.0)
