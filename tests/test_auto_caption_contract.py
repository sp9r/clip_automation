import copy
import unittest

from auto_caption_contract import (
    BASELINE_VERSION,
    MAPPING_FORMULA,
    REQUEST_VERSION,
    RESULT_VERSION,
    AutoCaptionContractError,
    build_baseline,
    parse_baseline,
    parse_request,
    parse_result,
    source_time_to_timeline_us,
)


def request():
    return {
        "version": REQUEST_VERSION,
        "jobId": "caption-job-1",
        "mediaPath": r"C:\media\source.mp4",
        "sourceStartUs": 10_000_000,
        "sourceEndUs": 15_000_000,
        "language": "ja",
        "maxChars": 32,
        "maxLines": 2,
        "segmentationMode": "rule",
        "silenceCut": "strong",
        "accelerationMode": "gpu",
        "subtitleTemplate": "high-contrast",
    }


def result():
    return {
        "version": RESULT_VERSION,
        "jobId": "caption-job-1",
        "sourceStartUs": 10_000_000,
        "sourceEndUs": 15_000_000,
        "language": "ja",
        "provider": "faster-whisper",
        "model": "large-v3",
        "rawWords": [
            {"id": "word-1", "text": "字幕", "startUs": 10_100_000, "endUs": 10_300_000, "confidence": 0.9},
            {"id": "word-2", "text": "です", "startUs": 10_400_000, "endUs": 10_600_000},
        ],
        "captions": [{
            "id": "caption-1", "text": "字幕です", "sourceWordIds": ["word-1", "word-2"],
            "startUs": 10_100_000, "endUs": 10_600_000,
            "textStatus": "as_transcribed", "provenance": "caption_algorithm",
        }],
        "diagnostics": [],
        "fallback": {"used": False},
    }


class AutoCaptionContractTests(unittest.TestCase):
    def test_request_is_range_scoped_and_uses_integer_microseconds(self):
        self.assertEqual(parse_request(request())["sourceEndUs"], 15_000_000)
        self.assertEqual(parse_request(request())["silenceCut"], "strong")
        self.assertEqual(parse_request(request())["accelerationMode"], "gpu")
        invalid = copy.deepcopy(request())
        invalid["sourceStartUs"] = 1.5
        with self.assertRaises(AutoCaptionContractError):
            parse_request(invalid)
        for field, value in (("silenceCut", "aggressive"), ("accelerationMode", "fallback"), ("subtitleTemplate", "../unknown")):
            invalid = copy.deepcopy(request())
            invalid[field] = value
            with self.assertRaises(AutoCaptionContractError):
                parse_request(invalid)

    def test_youtube_silence_profile_is_a_valid_worker_setting(self):
        payload = request()
        payload["silenceCut"] = "youtube"
        self.assertEqual(parse_request(payload)["silenceCut"], "youtube")

    def test_request_accepts_bounded_custom_silence_threshold(self):
        payload = request()
        payload["silenceMinUs"] = 150_000
        self.assertEqual(parse_request(payload)["silenceMinUs"], 150_000)
        payload["silenceMinUs"] = 0
        self.assertEqual(parse_request(payload)["silenceMinUs"], 0)
        payload["silenceMinUs"] = -1
        with self.assertRaises(AutoCaptionContractError):
            parse_request(payload)

    def test_result_rejects_secrets_and_unknown_word_ownership(self):
        parsed = parse_result(result())
        self.assertEqual(parsed["captions"][0]["sourceWordIds"], ["word-1", "word-2"])
        invalid = copy.deepcopy(result())
        invalid["credentials"] = {"token": "secret"}
        with self.assertRaises(AutoCaptionContractError):
            parse_result(invalid)
        invalid = copy.deepcopy(result())
        invalid["captions"][0]["sourceWordIds"] = ["missing"]
        with self.assertRaises(AutoCaptionContractError):
            parse_result(invalid)
        invalid = copy.deepcopy(result())
        invalid["rawWords"][0]["startUs"] = 9_999_999
        with self.assertRaises(AutoCaptionContractError):
            parse_result(invalid)

    def test_source_to_timeline_mapping_is_plus_delta(self):
        clip = {"startUs": 2_000_000, "sourceInUs": 10_000_000, "durationUs": 5_000_000}
        self.assertEqual(source_time_to_timeline_us(clip, 10_000_000), 2_000_000)
        self.assertEqual(source_time_to_timeline_us(clip, 12_500_000), 4_500_000)
        with self.assertRaises(AutoCaptionContractError):
            source_time_to_timeline_us(clip, 15_000_001)

    def test_baseline_is_path_free_and_round_trips(self):
        baseline = build_baseline(
            result=result(), job_id="caption-job-1", created_at="2026-08-15T00:00:00.000Z",
            project_fingerprint="sha256:project", media_fingerprint="sha256:media",
            source_clip={"clipId": "clip-1", "assetId": "asset-1", "startUs": 2_000_000, "sourceInUs": 10_000_000, "durationUs": 5_000_000},
            settings={"language": "ja", "maxChars": 32, "maxLines": 2, "segmentationMode": "rule", "silenceCut": "strong", "accelerationMode": "gpu", "subtitleTemplate": "high-contrast"},
        )
        self.assertEqual(baseline["version"], BASELINE_VERSION)
        self.assertEqual(baseline["timelineMapping"]["formula"], MAPPING_FORMULA)
        self.assertNotIn("mediaPath", str(baseline))
        self.assertEqual(parse_baseline(baseline)["jobId"], "caption-job-1")


if __name__ == "__main__":
    unittest.main()
