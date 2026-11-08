import io
import json
import os
import contextlib
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import caption
import caption_llm
from spectra_caption_worker import _caption_runtime_diagnostic_entries


class CaptionLocalLlmTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="spectra-caption-llm-")
        self.addCleanup(self.temporary.cleanup)
        self.key_path = Path(self.temporary.name) / "local-key.txt"
        self.key_path.write_text("private-fixture-key\n", encoding="utf-8")
        self.environment = {
            "SPECTRA_CAPTION_LLM_BASE_URL": "http://127.0.0.1:8089/v1",
            "SPECTRA_CAPTION_LLM_MODEL": "qwen3.6-35b",
            "SPECTRA_CAPTION_LLM_API_KEY_FILE": str(self.key_path),
            "SPECTRA_CAPTION_LLM_DISABLE_LOCAL_CONFIG": "1",
        }
        self.env = patch.dict(os.environ, self.environment)
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def assert_safe_error(self, action):
        with self.assertRaises(caption_llm.CaptionLlmError) as caught:
            action()
        text = str(caught.exception)
        self.assertNotIn("private-fixture-key", text)
        self.assertNotIn(str(self.key_path), text)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_openai_request_authentication_payload_and_content(self):
        response = {"model": "qwen3.6-35b", "choices": [{"finish_reason": "stop", "message": {"content": '{"break_after":[2]}', "reasoning_content": "not caption text"}}]}
        with patch("caption_llm._open_request", return_value=self.response(response)) as opened:
            text = caption._ollama_generate("fixture transcript", "old-default", 120)
        self.assertEqual(text, '{"break_after":[2]}')
        request, timeout = opened.call_args.args
        self.assertEqual(request.full_url, "http://127.0.0.1:8089/v1/chat/completions")
        self.assertEqual(timeout, 120)
        self.assertEqual(request.get_header("Authorization"), "Bearer private-fixture-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "qwen3.6-35b")
        self.assertEqual(payload["temperature"], 0.1)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertTrue(0 < payload["max_tokens"] <= 8192)
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"], [{"role": "user", "content": "fixture transcript"}])
        self.assertNotIn("private-fixture-key", request.data.decode())

    def test_unconfigured_profile_preserves_ollama_request(self):
        with patch.dict(os.environ, {**{name: "" for name in self.environment}, "SPECTRA_CAPTION_LLM_DISABLE_LOCAL_CONFIG": "1"}), patch("caption.urllib.request.urlopen", return_value=self.response({"response": "old-result"})) as opened:
            self.assertEqual(caption._ollama_generate("prompt", "qwen2.5:14b", 30), "old-result")
            self.assertEqual(caption_llm.default_model(), "qwen2.5:14b")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/generate")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(json.loads(request.data)["model"], "qwen2.5:14b")

    def test_credentials_read_per_request_without_logging_raw_failure(self):
        with patch("caption_llm._open_request", side_effect=OSError(f"private-fixture-key {self.key_path}")):
            self.assert_safe_error(lambda: caption_llm.generate("prompt", "model", 10))
        self.key_path.unlink()
        self.assert_safe_error(lambda: caption_llm.generate("prompt", "model", 10))

    def test_invalid_non_loopback_or_credential_urls_are_rejected_before_network(self):
        for base in ["https://example.com/v1", "http://192.168.1.2/v1", "http://user:private-fixture-key@127.0.0.1:8089/v1", "http://127.0.0.1:8089/v1?key=secret", "http://localhost:bad/v1", "file:///tmp/v1", "http://localhost.example.com/v1"]:
            with self.subTest(base=base), patch.dict(os.environ, {"SPECTRA_CAPTION_LLM_BASE_URL": base}), patch("caption_llm._open_request") as opened:
                self.assert_safe_error(lambda: caption_llm.generate("prompt", "model", 10))
                opened.assert_not_called()

    def test_loopback_variants_are_accepted(self):
        for base in ["http://localhost:8089/v1", "http://127.0.0.2:8089/v1", "http://[::1]:8089/v1"]:
            with self.subTest(base=base), patch.dict(os.environ, {"SPECTRA_CAPTION_LLM_BASE_URL": base}), patch("caption_llm._open_request", return_value=self.response({"choices": [{"message": {"content": "{}"}}]})):
                self.assertEqual(caption_llm.generate("prompt", "model", 10), "{}")

    def test_redirects_never_forward_authorization(self):
        with self.assertRaises(caption_llm.CaptionLlmError):
            caption_llm._NoRedirect().redirect_request(None, None, 302, "private-fixture-key", {}, "https://example.com")

    def test_empty_multiline_and_non_ascii_keys_are_rejected(self):
        for key in ["", "one\ntwo", "秘密", "x" * 8193]:
            self.key_path.write_text(key, encoding="utf-8")
            with patch("caption_llm._open_request") as opened:
                self.assert_safe_error(lambda: caption_llm.generate("prompt", "model", 10))
                opened.assert_not_called()

    def test_malformed_oversized_and_truncated_completions_fail_safely(self):
        for raw in [b"not JSON private-fixture-key", b"x" * (caption_llm.MAX_RESPONSE_BYTES + 1), b"[]", b'{}', b'{"choices":[{"message":{"content":""}}]}', b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}']:
            with self.subTest(length=len(raw)), patch("caption_llm._open_request", return_value=io.BytesIO(raw)):
                self.assert_safe_error(lambda: caption_llm.generate("prompt", "model", 10))

    def test_runtime_uses_authenticated_models_route_and_never_claims_gpu_measurement(self):
        with patch("caption_llm._open_request", return_value=self.response({"data": [{"id": "qwen3.6-35b"}]})) as opened, patch("caption.urllib.request.urlopen") as ollama:
            result = caption.get_ollama_runtime_diagnostic("old-default")
        ollama.assert_not_called()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8089/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-fixture-key")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["model"], "qwen3.6-35b")
        self.assertEqual(result["acceleration"], "unmeasured")
        entries = _caption_runtime_diagnostic_entries(result)
        self.assertIn({"level": "info", "code": "CAPTION_AI_BACKEND", "message": "backend=openai-compatible"}, entries)
        self.assertIn({"level": "info", "code": "CAPTION_AI_RUNTIME", "message": "status=available"}, entries)
        self.assertNotIn("private-fixture-key", json.dumps(entries))

    def test_model_default_is_read_at_worker_process_start(self):
        command = [sys.executable, "-c", "import spectra_caption_worker as w; print(w.DEFAULT_CAPTION_MODEL)"]
        selected = subprocess.check_output(command, env=dict(os.environ), text=True)
        self.assertEqual(selected.strip(), "qwen3.6-35b")
        environment = {key: value for key, value in os.environ.items() if key not in self.environment}
        environment["SPECTRA_CAPTION_LLM_DISABLE_LOCAL_CONFIG"] = "1"
        legacy = subprocess.check_output(command, env=environment, text=True)
        self.assertEqual(legacy.strip(), "qwen2.5:14b")

    def test_openai_transport_failure_preserves_source_and_falls_back_without_secrets(self):
        words = [{"word": "これは", "start": 0, "end": 0.5}, {"word": "テストです", "start": 0.5, "end": 1.2}]
        transcript = Path(self.temporary.name) / "transcript.json"
        transcript.write_text(json.dumps([{"start": 0, "end": 1.2, "text": "これはテストです", "words": words}]), encoding="utf-8")
        output = Path(self.temporary.name) / "captions.srt"
        printed = io.StringIO()
        with patch("caption_llm._open_request", side_effect=OSError("private-fixture-key")), contextlib.redirect_stdout(printed):
            result = caption.generate_subtitles(str(transcript), str(output))
        self.assertIn("fallback", result["method"])
        self.assertEqual("".join(item["text"] for item in result["captions"]), "これはテストです")
        self.assertEqual(result["caption_ai_runtime"]["backend"], "openai-compatible")
        self.assertNotIn("private-fixture-key", printed.getvalue())
        self.assertNotIn(str(self.key_path), printed.getvalue())
        self.assertNotIn("private-fixture-key", json.dumps(result))

    def test_unavailable_or_wrong_model_diagnostics_are_explicit_and_secret_free(self):
        with patch("caption_llm._open_request", side_effect=OSError(f"private-fixture-key {self.key_path}")):
            result = caption_llm.runtime_diagnostic("fallback")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("private-fixture-key", json.dumps(result))
        with patch("caption_llm._open_request", return_value=self.response({"data": [{"id": "other-model"}]})):
            result = caption_llm.runtime_diagnostic("fallback")
        self.assertEqual(result["reason"], "configured_model_not_advertised")

    def router_responses(self, states, calls):
        """Serve GET /models from a list of states; record POST routes."""
        states = list(states)

        def opened(request, timeout):
            if request.data is not None:
                calls.append((request.full_url, json.loads(request.data)))
                return self.response({"success": True})
            value = states.pop(0) if len(states) > 1 else states[0]
            return self.response({"data": [{"id": "qwen3.6-35b", "status": {"value": value}}]})
        return opened

    def test_gpu_handoff_unloads_then_reloads_through_router_root(self):
        calls = []
        clock = iter(range(0, 1000)).__next__
        with patch("caption_llm._open_request", side_effect=self.router_responses(["loaded", "unloading", "unloaded"], calls)):
            self.assertEqual(caption_llm.release_gpu("x", sleep=lambda _s: None, clock=clock), "unloaded")
        self.assertEqual(calls, [("http://127.0.0.1:8089/models/unload", {"model": "qwen3.6-35b"})])
        calls.clear()
        with patch("caption_llm._open_request", side_effect=self.router_responses(["unloaded", "loading", "loaded"], calls)):
            caption_llm.ensure_loaded("x", sleep=lambda _s: None, clock=clock)
        self.assertEqual(calls, [("http://127.0.0.1:8089/models/load", {"model": "qwen3.6-35b"})])

    def test_failed_load_makes_later_generation_fail_fast_without_network(self):
        clock = iter(range(0, 1000, 5)).__next__
        with patch("caption_llm._open_request", side_effect=self.router_responses(["failed"], [])):
            self.assert_safe_error(lambda: caption_llm.ensure_loaded("x", sleep=lambda _s: None, clock=clock))
        with patch("caption_llm._open_request") as opened:
            self.assert_safe_error(lambda: caption_llm.generate("prompt", "x", 10))
            opened.assert_not_called()
        with patch("caption_llm._open_request", side_effect=self.router_responses(["loaded"], [])):
            caption_llm.ensure_loaded("x")
        self.assertFalse(caption_llm._unavailable)

    def test_release_never_raises_when_server_is_down(self):
        with patch("caption_llm._open_request", side_effect=OSError("down")):
            self.assertEqual(caption_llm.release_gpu("x"), "unavailable")

    def test_japanese_uses_the_japanese_whisper_model_by_default(self):
        import spectra_caption_worker as worker
        self.assertEqual(worker.whisper_model_for_language("ja"), "aikobin/whisper-ja-1.5B-ct2")
        self.assertEqual(worker.whisper_model_for_language("en"), "large-v3")
        self.assertEqual(worker.whisper_model_for_language("ja", "large-v3"), "large-v3")
        self.assertEqual(worker._model_label("aikobin/whisper-ja-1.5B-ct2"), "whisper-ja-1.5B-ct2")
        self.assertEqual(worker._model_label("C:/models/x"), "C:/models/x")

    def test_thinking_mode_asks_for_reasoning_without_a_json_grammar(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": '{"c":[]}', "reasoning_content": "..."}}]}
        with patch("caption_llm._open_request", return_value=self.response(response)) as opened:
            self.assertEqual(caption_llm.generate("prompt", "x", 600, thinking=True), '{"c":[]}')
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": True})
        self.assertNotIn("response_format", payload)
        self.assertGreater(payload["max_tokens"], caption_llm.MAX_OUTPUT_TOKENS)
        self.assertTrue(caption_llm.writer_thinking())
        with patch.dict(os.environ, {"SPECTRA_CAPTION_LLM_WRITER_THINKING": "false"}):
            self.assertFalse(caption_llm.writer_thinking())


if __name__ == "__main__":
    unittest.main()
