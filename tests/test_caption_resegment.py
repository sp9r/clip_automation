import unittest

from auto_caption_contract import AutoCaptionContractError, parse_resegment_request
from caption_layout import fits
from spectra_caption_resegment import resegment

TEXTS = ["就職試験の", "面接を", "英語で", "突破する", "コツを", "3分で", "解説します", "あなたは", "エントリーシートを", "憧れの会社に", "提出すると", "嬉しいことに", "面接の", "通知が来ました"]


def words():
    return [{"id": f"w{i}", "text": text, "startUs": i * 600_000, "endUs": i * 600_000 + 550_000} for i, text in enumerate(TEXTS)]


def request(**extra):
    return {"version": "spectra.auto-caption.resegment.request.v1", "jobId": "rs1", "language": "ja", "maxChars": 32, "maxLines": 2, "segmentationMode": "rule", "words": words(), **extra}


class ResegmentTests(unittest.TestCase):
    def test_every_word_owned_once_in_order_and_fits_layout(self):
        result = resegment(request(lineCapacity=8, maxLines=1))
        owned = [wid for caption in result["captions"] for wid in caption["sourceWordIds"]]
        self.assertEqual(owned, [f"w{i}" for i in range(len(TEXTS))])
        by_id = {word["id"]: word["text"] for word in words()}
        for caption in result["captions"]:
            self.assertTrue(fits([by_id[w] for w in caption["sourceWordIds"]], 8, 1) or len(caption["sourceWordIds"]) == 1, caption)
            self.assertGreater(caption["endUs"], caption["startUs"])

    def test_smaller_limits_make_more_captions(self):
        loose = resegment(request(lineCapacity=16, maxLines=2))
        tight = resegment(request(lineCapacity=8, maxLines=1))
        self.assertGreater(len(tight["captions"]), len(loose["captions"]))

    def test_contract_rejects_bad_requests(self):
        for bad in ({"words": []}, {"segmentationMode": "x"}, {"lineCapacity": 0}, {"extra": 1}):
            with self.assertRaises(AutoCaptionContractError):
                parse_resegment_request(request(**bad))
        dup = request()
        dup["words"][1]["id"] = "w0"
        with self.assertRaises(AutoCaptionContractError):
            parse_resegment_request(dup)


if __name__ == "__main__":
    unittest.main()
