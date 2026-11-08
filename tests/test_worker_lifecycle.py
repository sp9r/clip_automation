import io
import json
import time
import unittest

from spectra_caption_worker import WorkerCancelled
from spectra_caption_worker_server import CONTROL_VERSION, SERVER_EVENT_VERSION, run_server
from transcribe import WhisperModelCache


def request(job_id: str) -> dict:
    return {
        "version": "spectra.auto-caption.request.v1",
        "jobId": job_id,
        "mediaPath": "C:/media/qa.mp4",
        "sourceStartUs": 0,
        "sourceEndUs": 5_000_000,
        "language": "ja",
        "maxChars": 32,
        "maxLines": 2,
        "segmentationMode": "rule",
    }


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class WorkerLifecycleTests(unittest.TestCase):
    def test_model_cache_reuses_warm_model(self):
        calls = []
        clock = Clock()

        def loader(model_size, *, acceleration_mode):
            calls.append((model_size, acceleration_mode))
            return object(), {
                "device": "cuda",
                "computeType": "float16",
                "model": model_size,
                "cudaAvailable": True,
                "fallbackReason": "none",
            }

        cache = WhisperModelCache(
            idle_timeout_seconds=10,
            clock=clock,
            gpu_fingerprint=lambda: "gpu-a",
            loader=loader,
        )
        first, first_diag = cache.get_or_load("large-v3", "gpu")
        second, second_diag = cache.get_or_load("large-v3", "gpu")
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)
        self.assertFalse(first_diag["cacheHit"])
        self.assertTrue(second_diag["cacheHit"])

    def test_idle_timeout_releases_model(self):
        clock = Clock()
        cache = WhisperModelCache(
            idle_timeout_seconds=5,
            clock=clock,
            gpu_fingerprint=lambda: "gpu-a",
            loader=lambda *_args, **_kwargs: (object(), {
                "device": "cuda", "computeType": "float16", "model": "large-v3",
                "cudaAvailable": True, "fallbackReason": "none",
            }),
        )
        cache.get_or_load("large-v3", "gpu")
        clock.value = 4.9
        self.assertFalse(cache.release_if_idle())
        clock.value = 5.0
        self.assertTrue(cache.release_if_idle())
        self.assertFalse(cache.loaded)

    def test_gpu_fingerprint_change_reinitializes_model(self):
        calls = []
        fingerprint = ["gpu-a"]

        def loader(*_args, **_kwargs):
            calls.append(True)
            return object(), {
                "device": "cuda", "computeType": "float16", "model": "large-v3",
                "cudaAvailable": True, "fallbackReason": "none",
            }

        cache = WhisperModelCache(gpu_fingerprint=lambda: fingerprint[0], loader=loader)
        first, _ = cache.get_or_load("large-v3", "gpu")
        fingerprint[0] = "gpu-b"
        second, second_diag = cache.get_or_load("large-v3", "gpu")
        self.assertIsNot(first, second)
        self.assertFalse(second_diag["cacheHit"])
        self.assertEqual(len(calls), 2)

    def test_server_warm_jobs_and_versioned_events(self):
        loader_calls = []

        def loader(*_args, **_kwargs):
            loader_calls.append(True)
            return object(), {
                "device": "cuda", "computeType": "float16", "model": "large-v3",
                "cudaAvailable": True, "fallbackReason": "none",
            }

        cache = WhisperModelCache(loader=loader, gpu_fingerprint=lambda: "gpu-a")

        def runner(job, *, progress_stream, cancel_event, model_cache, acceleration_mode):
            _, diagnostics = model_cache.get_or_load("large-v3", acceleration_mode)
            progress_stream.write(json.dumps({
                "version": "spectra.auto-caption.progress.v1",
                "jobId": job["jobId"], "state": "completed", "ratio": 1.0,
                "message": "done",
            }) + "\n")
            return {"jobId": job["jobId"], "cacheHit": diagnostics["cacheHit"]}

        input_stream = io.StringIO(json.dumps(request("job-1")) + "\n" + json.dumps(request("job-2")) + "\n")
        output_stream = io.StringIO()
        self.assertEqual(run_server(input_stream, output_stream, worker_runner=runner, model_cache=cache), 0)
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertTrue(all(event["version"] == SERVER_EVENT_VERSION for event in events))
        results = [event for event in events if event["type"] == "result"]
        self.assertEqual([result["payload"]["cacheHit"] for result in results], [False, True])
        self.assertEqual(len(loader_calls), 1)

    def test_server_cancel_is_forwarded_to_active_job(self):
        def runner(job, *, progress_stream, cancel_event, model_cache, acceleration_mode):
            while not cancel_event.is_set():
                time.sleep(0.005)
            raise WorkerCancelled("cancelled")

        commands = "\n".join([
            json.dumps(request("job-cancel")),
            json.dumps({"version": CONTROL_VERSION, "type": "cancel", "jobId": "job-cancel"}),
        ]) + "\n"
        output_stream = io.StringIO()
        self.assertEqual(run_server(io.StringIO(commands), output_stream, worker_runner=runner), 0)
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertTrue(any(event.get("state") == "cancel_requested" for event in events))
        self.assertTrue(any(event.get("type") == "error" and event.get("errorCode") == "CANCELLED" for event in events))

    def test_server_timeout_returns_safe_error_and_disposes_process(self):
        def runner(_job, *, progress_stream, cancel_event, model_cache, acceleration_mode):
            while not cancel_event.is_set():
                time.sleep(0.005)
            raise WorkerCancelled("timed out")

        output_stream = io.StringIO()
        self.assertEqual(
            run_server(
                io.StringIO(json.dumps(request("job-timeout")) + "\n"),
                output_stream,
                worker_runner=runner,
                job_timeout_seconds=0.02,
            ),
            2,
        )
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertTrue(any(event.get("errorCode") == "WORKER_TIMEOUT" for event in events))
        self.assertNotIn("timed out", output_stream.getvalue())

    def test_server_crash_isolated_and_next_job_can_run(self):
        calls = []

        def runner(job, **_kwargs):
            calls.append(job["jobId"])
            if job["jobId"] == "job-crash":
                raise RuntimeError("local path must not escape")
            return {"jobId": job["jobId"]}

        commands = json.dumps(request("job-crash")) + "\n" + json.dumps(request("job-next")) + "\n"
        output_stream = io.StringIO()
        self.assertEqual(run_server(io.StringIO(commands), output_stream, worker_runner=runner), 0)
        events = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual(calls, ["job-crash", "job-next"])
        self.assertTrue(any(event.get("errorCode") == "WORKER_CRASH" for event in events))
        self.assertTrue(any(event.get("type") == "result" and event.get("jobId") == "job-next" for event in events))
        self.assertNotIn("local path", output_stream.getvalue())


if __name__ == "__main__":
    unittest.main()
