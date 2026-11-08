import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from planning_artifacts import (
    PlanningValidationError,
    artifact_hash,
    load_artifact,
    validate_artifact,
    write_artifact,
)
from planning_core import (
    ChunkingConfig,
    DecisionSettings,
    decide_clips,
    evaluate_candidates,
    generate_candidates,
    plan_clips,
    chunk_transcript,
)
from planning_providers import (
    DeterministicCandidateProvider,
    DeterministicEvaluationProvider,
    FailingCandidateProvider,
    FallbackCandidateProvider,
)


HASH = "0" * 64


def _transcript(duration=30.0, count=6):
    step = duration / count
    return [
        {"start": index * step, "end": (index + 1) * step, "text": f"発話{index}"}
        for index in range(count)
    ]


def _candidate_artifact(items):
    transcript = _transcript(30.0, 3)
    return generate_candidates(
        transcript,
        input_hashes={"clean_transcript.json": HASH},
        provider=DeterministicCandidateProvider(selector=lambda chunk: items),
        config=ChunkingConfig(max_duration=30.0, overlap_duration=0.0),
    )


class Phase2PlanningTests(unittest.TestCase):
    def test_single_oversized_record_is_split_without_dropping_text(self):
        text = "x" * 55
        chunks = chunk_transcript([{"start": 0.0, "end": 5.0, "text": text}], ChunkingConfig(max_chars=20, max_duration=10.0, overlap_duration=0.0))
        self.assertTrue(all(len(chunk["text"]) <= 20 for chunk in chunks))
        self.assertEqual("".join(chunk["text"] for chunk in chunks), text)

    def test_short_transcript_uses_one_chunk_and_generates_candidate(self):
        artifact = generate_candidates(
            _transcript(10.0, 2),
            input_hashes={"transcript": HASH},
            provider=DeterministicCandidateProvider(),
        )
        self.assertEqual(len(artifact["chunks"]), 1)
        self.assertEqual(artifact["chunks"][0]["timeline"], {"start": 0.0, "end": 10.0})
        self.assertEqual(len(artifact["candidates"]), 1)
        self.assertEqual(validate_artifact("candidates", artifact), [])

    def test_long_transcript_has_multiple_overlapping_chunks_covering_timeline(self):
        chunks = chunk_transcript(_transcript(300.0, 30), ChunkingConfig(max_duration=60.0, overlap_duration=10.0))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0]["timeline"]["start"], 0.0)
        self.assertEqual(chunks[-1]["timeline"]["end"], 300.0)
        for first, second in zip(chunks, chunks[1:]):
            self.assertLessEqual(first["timeline"]["end"], 60.0 + first["timeline"]["start"] + 1e-6)
            self.assertLessEqual(second["timeline"]["start"], first["timeline"]["end"])

    def test_overlap_candidates_are_deduplicated(self):
        artifact = generate_candidates(
            _transcript(30.0, 3),
            input_hashes={"transcript": HASH},
            provider=DeterministicCandidateProvider(
                selector=lambda chunk: [{"start": 16.0, "end": 19.0, "reason": "same"}]
            ),
            config=ChunkingConfig(max_duration=20.0, overlap_duration=5.0),
        )
        self.assertEqual(len(artifact["candidates"]), 1)

    def test_out_of_range_candidate_fails_without_clamp(self):
        artifact = generate_candidates(
            _transcript(10.0, 2),
            input_hashes={"transcript": HASH},
            provider=DeterministicCandidateProvider(selector=lambda chunk: [{"start": -1, "end": 5, "reason": "bad"}]),
        )
        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["candidates"], [])
        self.assertTrue(any(item["code"] == "candidate_provider_failed" for item in artifact["diagnostics"]))

    def test_evaluation_total_is_computed_from_code_weights(self):
        candidates = _candidate_artifact([{"start": 0.0, "end": 10.0, "reason": "good"}])
        weights = {axis: 1.0 for axis in ("hook", "standalone", "interest", "coherence", "novelty", "confidence")}
        scores = {"hook": 100, "standalone": 0, "interest": 50, "coherence": 50, "novelty": 50, "confidence": 50}
        evaluations = evaluate_candidates(
            candidates,
            input_hashes={"candidates": artifact_hash(candidates)},
            evaluator=DeterministicEvaluationProvider(scores=scores),
            weights=weights,
        )
        self.assertEqual(evaluations["evaluations"][0]["total_score"], 50.0)
        self.assertEqual(validate_artifact("evaluations", evaluations), [])

    def test_invalid_evaluation_score_nan_is_failed(self):
        candidates = _candidate_artifact([{"start": 0.0, "end": 10.0, "reason": "good"}])
        evaluations = evaluate_candidates(
            candidates,
            input_hashes={"candidates": artifact_hash(candidates)},
            evaluator=DeterministicEvaluationProvider(scores={axis: math.nan for axis in ("hook", "standalone", "interest", "coherence", "novelty", "confidence")}),
        )
        self.assertEqual(evaluations["status"], "failed")
        self.assertTrue(any(item["code"] == "evaluation_failed" for item in evaluations["diagnostics"]))

    def test_evaluation_candidate_id_mismatch_is_rejected(self):
        class WrongIdEvaluator:
            name = "wrong-id"
            version = "1"

            def evaluate(self, candidate, context):
                return {"candidate_id": "other", "scores": {axis: 80 for axis in ("hook", "standalone", "interest", "coherence", "novelty", "confidence")}}

        candidates = _candidate_artifact([{"start": 0.0, "end": 10.0, "reason": "good"}])
        evaluations = evaluate_candidates(candidates, input_hashes={"candidates": artifact_hash(candidates)}, evaluator=WrongIdEvaluator())
        self.assertEqual(evaluations["status"], "failed")
        self.assertEqual(evaluations["evaluations"], [])

    def test_fallback_status_propagates_to_decision_and_plan(self):
        candidates = generate_candidates(_transcript(10.0, 2), input_hashes={"transcript": HASH}, provider=FallbackCandidateProvider())
        evaluations = evaluate_candidates(candidates, input_hashes={"candidates": artifact_hash(candidates)}, evaluator=DeterministicEvaluationProvider())
        decision = decide_clips(candidates, evaluations, input_hashes={"evaluations": artifact_hash(evaluations)}, settings=DecisionSettings(min_duration=0.1))
        plans = plan_clips(candidates, decision, input_hashes={"decision": artifact_hash(decision)}, target_output={"width": 1, "height": 1, "fps": 1})
        self.assertEqual(decision["status"], "fallback")
        self.assertEqual(plans[0]["status"], "fallback")

    def test_decision_is_deterministic_and_suppresses_overlap(self):
        candidates = _candidate_artifact([
            {"start": 0.0, "end": 10.0, "reason": "a"},
            {"start": 7.0, "end": 17.0, "reason": "b"},
            {"start": 15.0, "end": 25.0, "reason": "c"},
        ])
        evaluations = evaluate_candidates(
            candidates,
            input_hashes={"candidates": artifact_hash(candidates)},
            evaluator=DeterministicEvaluationProvider(),
        )
        settings = DecisionSettings(max_selected_clips=2, overlap_suppression=0.15)
        first = decide_clips(candidates, evaluations, input_hashes={"evaluations": artifact_hash(evaluations)}, settings=settings)
        second = decide_clips(candidates, evaluations, input_hashes={"evaluations": artifact_hash(evaluations)}, settings=settings)
        self.assertEqual(first, second)
        self.assertEqual(len(first["selected_candidate_ids"]), 2)
        self.assertIn("overlap_suppressed", first["rejection_reasons"].values())

    def test_three_selected_candidates_produce_three_independent_plans(self):
        candidates = _candidate_artifact([
            {"start": 0.0, "end": 8.0, "reason": "a"},
            {"start": 10.0, "end": 18.0, "reason": "b"},
            {"start": 20.0, "end": 28.0, "reason": "c"},
        ])
        evaluations = evaluate_candidates(candidates, input_hashes={"candidates": artifact_hash(candidates)}, evaluator=DeterministicEvaluationProvider())
        decision = decide_clips(candidates, evaluations, input_hashes={"evaluations": artifact_hash(evaluations)}, settings=DecisionSettings(max_selected_clips=3))
        plans = plan_clips(candidates, decision, input_hashes={"decision": artifact_hash(decision)}, target_output={"width": 1080, "height": 1920, "fps": 30})
        self.assertEqual([plan["clip_id"] for plan in plans], ["clip_000001", "clip_000002", "clip_000003"])
        self.assertEqual(len({plan["candidate_id"] for plan in plans}), 3)
        self.assertTrue(all(validate_artifact("plan", plan) == [] for plan in plans))

    def test_fallback_and_provider_failure_are_explicit(self):
        fallback = generate_candidates(_transcript(10.0, 2), input_hashes={"transcript": HASH}, provider=FallbackCandidateProvider())
        self.assertEqual(fallback["status"], "fallback")
        self.assertEqual(fallback["candidates"][0]["provenance"]["status"], "fallback")
        failed = generate_candidates(_transcript(10.0, 2), input_hashes={"transcript": HASH}, provider=FailingCandidateProvider())
        self.assertEqual(failed["status"], "failed")
        self.assertNotEqual(failed["status"], "normal")

    def test_artifact_round_trip_is_atomic_and_semantic(self):
        artifact = _candidate_artifact([{"start": 0.0, "end": 10.0, "reason": "good"}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "highlight_candidates.v1.json"
            write_artifact(path, "candidates", artifact)
            self.assertEqual(load_artifact(path, "candidates"), artifact)
            self.assertFalse(path.with_suffix(path.suffix + ".tmp").exists())

    def test_candidate_input_is_not_mutated_and_generation_is_deterministic(self):
        transcript = _transcript(20.0, 4)
        before = copy.deepcopy(transcript)
        provider = DeterministicCandidateProvider()
        first = generate_candidates(transcript, input_hashes={"transcript": HASH}, provider=provider)
        second = generate_candidates(transcript, input_hashes={"transcript": HASH}, provider=provider)
        self.assertEqual(first, second)
        self.assertEqual(transcript, before)


if __name__ == "__main__":
    unittest.main()
