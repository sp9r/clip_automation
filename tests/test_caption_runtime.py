import io
import json
import unittest
from unittest.mock import patch

import caption


class CaptionRuntimeTests(unittest.TestCase):
    def _probe(self, payload):
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda: response
        response.__exit__ = lambda *_args: None
        with patch("caption.urllib.request.urlopen", return_value=response):
            return caption.get_ollama_runtime_diagnostic("qwen2.5:14b")

    def test_full_vram_is_gpu(self):
        result = self._probe({"models": [{"name": "qwen2.5:14b", "size": 100, "size_vram": 100}]})
        self.assertEqual(result["status"], "gpu")

    def test_zero_vram_is_cpu(self):
        result = self._probe({"models": [{"model": "qwen2.5:14b", "size": 100, "size_vram": 0}]})
        self.assertEqual(result["status"], "cpu")

    def test_partial_vram_is_mixed(self):
        result = self._probe({"models": [{"name": "qwen2.5:14b", "size": 100, "size_vram": 40}]})
        self.assertEqual(result["status"], "mixed")

    def test_unavailable_runtime_is_explicit(self):
        with patch("caption.urllib.request.urlopen", side_effect=OSError("offline")):
            result = caption.get_ollama_runtime_diagnostic("qwen2.5:14b")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("offline", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
