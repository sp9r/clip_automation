import io
import json
import unittest
from unittest import mock

from ollama_service import check_ollama, start_ollama
from ui_presenter import format_health, format_user_error


class _Response:
    def __init__(self, data):
        self.data = data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return json.dumps(self.data).encode("utf-8")


class OllamaServiceTests(unittest.TestCase):
    def test_health_lists_models(self):
        with mock.patch("ollama_service.urllib.request.urlopen", return_value=_Response({"models": [{"name": "gemma3:latest"}]})):
            state = check_ollama()
        self.assertTrue(state["available"])
        self.assertEqual(state["models"], ["gemma3:latest"])

    def test_explicit_start_does_not_spawn_when_already_ready(self):
        with mock.patch("ollama_service.check_ollama", return_value={"available": True, "models": [], "error_code": None}) as check, mock.patch("ollama_service.subprocess.Popen") as popen:
            state = start_ollama()
        self.assertTrue(state["available"])
        popen.assert_not_called()

    def test_presenter_includes_reason_and_user_action(self):
        text = format_user_error({"message": "接続できません", "error_code": "OLLAMA_UNAVAILABLE", "retryable": True, "user_action": "Ollamaを起動してください。"})
        self.assertIn("接続できません", text)
        self.assertIn("Ollamaを起動", text)
        self.assertIn("再試行可能", text)
        self.assertIn("Ollama", format_health({"available": False, "models": [], "message": "未起動"}, "gemma3:latest"))


if __name__ == "__main__":
    unittest.main()
