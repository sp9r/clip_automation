import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from editor_api import create_app
from timeline_preparation import TimelinePreparationService, create_native_timeline_project
from timeline_project import load_timeline_project


class P0ReleaseGateTests(unittest.TestCase):
    """P0 Release Gate: 評価copyの実runで1,477字幕読込と準備成功を実証する。"""

    def setUp(self):
        self.repo_root = Path(__file__).parent.parent.resolve()
        self.original_run_dir = self.repo_root / "data" / "runs" / "20260806_163921"
        self.assertTrue(
            self.original_run_dir.is_dir(),
            f"実run {self.original_run_dir} が存在しません。",
        )
        self.clean_path = self.original_run_dir / "clean_transcript.json"
        self.v1_path = self.original_run_dir / "caption_project.json"
        self.assertTrue(self.clean_path.is_file(), "clean_transcript.json が見つかりません。")
        self.assertTrue(self.v1_path.is_file(), "caption_project.json が見つかりません。")

        # 元データのハッシュを記録
        self.original_hashes = {
            "clean_transcript.json": hashlib.sha256(self.clean_path.read_bytes()).hexdigest(),
            "caption_project.json": hashlib.sha256(self.v1_path.read_bytes()).hexdigest(),
        }

        # テスト用一時ディレクトリに評価copyを作成
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runs_root = Path(self.temp_dir.name)
        self.eval_copy_dir = self.runs_root / "eval_copy_20260806_163921"
        self.eval_copy_dir.mkdir(parents=True)

        shutil.copy2(self.clean_path, self.eval_copy_dir / "clean_transcript.json")
        shutil.copy2(self.v1_path, self.eval_copy_dir / "caption_project.json")

    def tearDown(self):
        self.temp_dir.cleanup()
        # 元の20260806_163921が変更されていないか最終検証
        current_hashes = {
            "clean_transcript.json": hashlib.sha256(self.clean_path.read_bytes()).hexdigest(),
            "caption_project.json": hashlib.sha256(self.v1_path.read_bytes()).hexdigest(),
        }
        self.assertEqual(
            self.original_hashes,
            current_hashes,
            "元の20260806_163921のファイルが変更されました！",
        )
        self.assertFalse(
            (self.original_run_dir / "caption_project.v2.json").exists(),
            "元の20260806_163921にcaption_project.v2.jsonが生成されています！",
        )
        self.assertFalse(
            (self.original_run_dir / "transcript_words.json").exists(),
            "元の20260806_163921にtranscript_words.jsonが生成されています！",
        )

    def test_p0_release_gate_prepare_and_load_1477_captions(self):
        """評価copyでprepare-timelineを実行し、20,124 words / 1,477 captions がreadyになることを検証。"""
        service = TimelinePreparationService(self.eval_copy_dir)
        report = service.prepare()

        self.assertEqual(report["state"], "ready", "既存runの評価copyがreadyになりませんでした。")
        self.assertEqual(report["word_count"], 20124, "word_countが20,124ではありません。")
        self.assertEqual(report["caption_count"], 1477, "caption_countが1,477ではありません。")
        self.assertIsNone(report["first_mismatch"])

        # 生成されたv2ファイルとwordsファイルを検証
        v2_path = self.eval_copy_dir / "caption_project.v2.json"
        words_path = self.eval_copy_dir / "transcript_words.json"
        self.assertTrue(v2_path.is_file())
        self.assertTrue(words_path.is_file())

        project = load_timeline_project(v2_path)
        words = json.loads(words_path.read_text(encoding="utf-8"))

        self.assertEqual(len(words), 20124)
        self.assertEqual(len(project["captions"]), 1477)
        self.assertEqual(project["revision"], 0)

        # 最先頭と最終字幕の整合性チェック
        first_cap = project["captions"][0]
        last_cap = project["captions"][-1]
        self.assertEqual(first_cap["id"], "cap_000001")
        self.assertEqual(last_cap["id"], "cap_001477")
        self.assertGreater(last_cap["end"], first_cap["start"])

    def test_p0_release_gate_editor_api(self):
        """Editor API経由で評価copyのprepare・projectロード・範囲取得ができることを検証。"""
        client = TestClient(create_app(self.runs_root))

        # 最初は not_ready
        runs_resp = client.get("/api/runs")
        self.assertEqual(runs_resp.status_code, 200)
        run_info = next(r for r in runs_resp.json() if r["run_id"] == "eval_copy_20260806_163921")
        self.assertFalse(run_info["timeline_ready"])

        # prepare-timeline 呼出
        prep_resp = client.post("/api/runs/eval_copy_20260806_163921/prepare-timeline")
        self.assertEqual(prep_resp.status_code, 200)
        report = prep_resp.json()
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["caption_count"], 1477)
        self.assertEqual(report["word_count"], 20124)

        # プロジェクト読み込み
        proj_resp = client.get("/api/projects/eval_copy_20260806_163921")
        self.assertEqual(proj_resp.status_code, 200)
        proj_data = proj_resp.json()
        self.assertEqual(proj_data["word_count"], 20124)
        self.assertEqual(len(proj_data["project"]["captions"]), 1477)

        # タイムライン部分範囲取得 (start=0, end=30)
        timeline_resp = client.get("/api/projects/eval_copy_20260806_163921/timeline?start=0&end=30")
        self.assertEqual(timeline_resp.status_code, 200)
        timeline_data = timeline_resp.json()
        self.assertGreater(len(timeline_data["captions"]), 0)
        self.assertLess(len(timeline_data["captions"]), 1477)

    def test_p0_native_v2_generation(self):
        """新規runのclean_transcriptからv1を経由せずネイティブv2生成を検証。"""
        native_dir = self.runs_root / "native_new_run"
        native_dir.mkdir()
        shutil.copy2(self.clean_path, native_dir / "clean_transcript.json")

        v1_sample = json.loads(self.v1_path.read_text(encoding="utf-8"))
        report = create_native_timeline_project(
            native_dir,
            captions=v1_sample["captions"],
            source_path="input.mp4",
            edl_path="edl.json",
            default_template_id="standard",
            width=1920,
            height=1080,
            fps=60,
            max_chars=24,
        )
        self.assertEqual(report["generation"], "native")
        self.assertEqual(report["state"], "ready")
        self.assertEqual(report["caption_count"], 1477)
        self.assertEqual(report["word_count"], 20124)
        self.assertTrue((native_dir / "caption_project.v2.json").is_file())


if __name__ == "__main__":
    unittest.main()
