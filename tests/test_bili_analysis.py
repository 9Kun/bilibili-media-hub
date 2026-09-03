import json
import tempfile
import unittest
from pathlib import Path

from core.bili_analysis import BiliAnalysisPreparer
from core.bili_downloader import Clip, VideoInfo
from core.exceptions import NoSubtitleError
from core.output_layout import BiliProjectLayout


class FakeDownloader:
    def __init__(self, clips=None):
        self.calls = 0
        self.info = VideoInfo(
            bvid="BV1TEST",
            title="测试/视频",
            desc="简介",
            cover="https://example.test/cover.jpg",
            owner_name="测试UP",
            clips=clips
            or [Clip(cid=11, page=1, part="开场"), Clip(cid=22, page=2, part="深入")],
        )

    def get_video_info(self, _bvid):
        self.calls += 1
        return self.info


class FakeSubtitleDownloader:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def download_subtitle(self, bvid, output_path, page=1, lang=None, info=None):
        self.calls.append((bvid, page, lang))
        outcome = self.outcomes[page]
        if isinstance(outcome, Exception):
            raise outcome
        output_path = Path(output_path)
        output_path.write_text(
            f"1\n00:00:01,000 --> 00:00:03,000\n{outcome}\n",
            encoding="utf-8",
        )
        used_lang = "ai-zh" if outcome == "AI字幕" else "zh-CN"
        return output_path.resolve(), used_lang


class FakeTranscriber:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    def transcribe(self, bvid, output_path, page=1, info=None, clip=None):
        self.calls.append((bvid, page))
        if page in self.failures:
            raise self.failures[page]
        output_path = Path(output_path)
        output_path.write_text(f"[00:01] P{page} 转录文本。\n", encoding="utf-8")
        return output_path.resolve()


class BiliAnalysisPreparerTests(unittest.TestCase):
    @staticmethod
    def _layout(tmp):
        return BiliProjectLayout.create(Path(tmp), "测试/视频", "BV1TEST")

    def test_cc_success_does_not_call_asr_and_writes_manifest(self):
        subtitle = FakeSubtitleDownloader({1: "字幕一", 2: "字幕二"})
        transcriber = FakeTranscriber()
        downloader = FakeDownloader()
        preparer = BiliAnalysisPreparer(downloader, subtitle, transcriber)

        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(tmp)
            result = preparer.prepare("BV1TEST", layout, lang="zh-CN")
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(result.success_count, 2)
            self.assertEqual(result.failure_count, 0)
            self.assertEqual(transcriber.calls, [])
            self.assertEqual([p["source_type"] for p in manifest["pages"]], ["cc", "cc"])
            self.assertTrue(Path(manifest["pages"][0]["source_path"]).is_absolute())
            self.assertEqual(result.report_path, layout.analysis_dir / "report.md")
            self.assertEqual(result.manifest_path.name, "analysis-input.json")
            self.assertEqual(Path(manifest["run_dir"]), layout.run_dir)
            self.assertEqual(Path(manifest["pages"][0]["source_path"]).parent, layout.subtitles_dir)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(downloader.calls, 1)

    def test_ai_subtitle_is_distinguished_from_cc(self):
        subtitle = FakeSubtitleDownloader({1: "AI字幕", 2: "人工字幕"})
        preparer = BiliAnalysisPreparer(FakeDownloader(), subtitle, FakeTranscriber())

        with tempfile.TemporaryDirectory() as tmp:
            result = preparer.prepare("BV1TEST", self._layout(tmp))

            self.assertEqual(
                [page["source_type"] for page in result.pages],
                ["ai_subtitle", "cc"],
            )
            self.assertEqual(result.pages[0]["language"], "ai-zh")

    def test_no_subtitle_falls_back_per_page_and_preserves_order(self):
        subtitle = FakeSubtitleDownloader(
            {1: NoSubtitleError("no cc subtitle"), 2: "字幕二"}
        )
        transcriber = FakeTranscriber()
        preparer = BiliAnalysisPreparer(FakeDownloader(), subtitle, transcriber)

        with tempfile.TemporaryDirectory() as tmp:
            layout = self._layout(tmp)
            result = preparer.prepare("BV1TEST", layout, pages=[2, 1, 2])

            self.assertEqual([p["page"] for p in result.pages], [1, 2])
            self.assertEqual([p["source_type"] for p in result.pages], ["asr", "cc"])
            self.assertEqual(transcriber.calls, [("BV1TEST", 1)])
            self.assertEqual(Path(result.pages[0]["source_path"]).name, "P01.asr.txt")
            self.assertEqual(Path(result.pages[1]["source_path"]).name, "P02.zh-CN.srt")

    def test_non_missing_subtitle_error_is_recorded_without_asr_fallback(self):
        subtitle = FakeSubtitleDownloader({1: RuntimeError("network down"), 2: "字幕二"})
        transcriber = FakeTranscriber()
        preparer = BiliAnalysisPreparer(FakeDownloader(), subtitle, transcriber)

        with tempfile.TemporaryDirectory() as tmp:
            result = preparer.prepare("BV1TEST", self._layout(tmp))

            self.assertEqual((result.success_count, result.failure_count), (1, 1))
            self.assertEqual(transcriber.calls, [])
            self.assertEqual(result.pages[0]["error_stage"], "subtitle")
            self.assertEqual(result.pages[1]["status"], "ready")

    def test_all_pages_failed_still_writes_failure_manifest(self):
        missing = NoSubtitleError("没有字幕")
        subtitle = FakeSubtitleDownloader({1: missing, 2: missing})
        preparer = BiliAnalysisPreparer(FakeDownloader(), subtitle, None)

        with tempfile.TemporaryDirectory() as tmp:
            result = preparer.prepare("BV1TEST", self._layout(tmp))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(result.success_count, 0)
            self.assertEqual(result.failure_count, 2)
            self.assertEqual(manifest["summary"]["failure_count"], 2)
            self.assertTrue(all(p["error_stage"] == "transcription" for p in result.pages))

    def test_invalid_requested_page_is_rejected(self):
        preparer = BiliAnalysisPreparer(
            FakeDownloader(), FakeSubtitleDownloader({1: "一", 2: "二"}), FakeTranscriber()
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "页码不存在"):
                preparer.prepare("BV1TEST", self._layout(tmp), pages=[3])


if __name__ == "__main__":
    unittest.main()
