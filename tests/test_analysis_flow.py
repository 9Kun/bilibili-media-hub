import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.bili_analysis import AnalysisPreparationResult
from core.bili_downloader import Clip, QualityOption, VideoInfo
from scripts.bilibili_download_skill import BilibiliDownloadSkill, State
from scripts.skill_runner import build_parser


class FakeDownloader:
    def get_video_info(self, _bvid):
        return VideoInfo(
            bvid="BV1TEST",
            title="测试视频",
            clips=[Clip(cid=1, page=1, part="上"), Clip(cid=2, page=2, part="下")],
        )

    def get_available_quality_options(self, _bvid, info=None):
        return [QualityOption(120, "4K 超清"), QualityOption(80, "1080P")]

    def download_all_pages(
        self, _bvid, path, pages=None, progress_cb=None, media_type="video",
        quality=None, info=None,
    ):
        target_pages = pages or [clip.page for clip in info.clips]
        outputs = []
        for page in target_pages:
            output = Path(path) / f"P{page:02d}.mp4"
            output.write_bytes(b"media")
            outputs.append(output)
        return outputs

    def get_validation(self, _path):
        return None

    def cleanup_temporary_files(self, _directory):
        return None


class BlockingPreparer:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def prepare(self, bvid, layout, pages=None, lang=None, info=None):
        self.calls.append((bvid, pages, lang))
        self.started.set()
        self.release.wait(timeout=2)
        return AnalysisPreparationResult(
            manifest_path=layout.analysis_dir / "analysis-input.json",
            report_path=layout.analysis_dir / "report.md",
            success_count=1,
            failure_count=0,
            pages=[{"page": 1, "part_title": "上", "status": "ready"}],
        )


class AnalysisFlowTests(unittest.TestCase):
    def test_explicit_pages_start_download_without_confirmation(self):
        class BlockingDownloader(FakeDownloader):
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()
                self.call_kwargs = None

            def download_all_pages(self, *args, **kwargs):
                self.call_kwargs = kwargs
                self.started.set()
                self.release.wait(timeout=2)
                return super().download_all_pages(*args, **kwargs)

        replies = []
        downloader = BlockingDownloader()
        with tempfile.TemporaryDirectory() as tmp:
            skill = BilibiliDownloadSkill(downloader, Path(tmp), replies.append)
            prompt = skill.download_bilibili_multi_p(
                "BV1TEST", pages=[1], quality=120
            )

            self.assertTrue(downloader.started.wait(timeout=1))
            self.assertEqual(skill.state, State.DOWNLOADING)
            self.assertIn("已按显式分P范围开始下载", prompt)
            self.assertNotIn("是否开始", prompt)
            self.assertEqual(downloader.call_kwargs["quality"], 120)

            downloader.release.set()
            deadline = time.time() + 2
            while skill.state != State.IDLE and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(skill.state, State.IDLE)
            self.assertTrue(any("共1个文件" in reply for reply in replies))

    def test_explicit_all_pages_starts_without_confirmation(self):
        replies = []
        with tempfile.TemporaryDirectory() as tmp:
            skill = BilibiliDownloadSkill(
                FakeDownloader(), Path(tmp), replies.append
            )
            prompt = skill.download_bilibili_multi_p(
                "BV1TEST", all_pages=True
            )

            deadline = time.time() + 2
            while skill.state != State.IDLE and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn("显式 --all-pages", prompt)
            self.assertNotEqual(skill.state, State.WAIT_CONFIRM)
            self.assertTrue(any("共2个文件" in reply for reply in replies))

    def test_multi_page_confirmation_starts_analysis_and_resets_state(self):
        replies = []
        preparer = BlockingPreparer()
        with tempfile.TemporaryDirectory() as tmp:
            skill = BilibiliDownloadSkill(
                FakeDownloader(), Path(tmp), replies.append, analysis_preparer=preparer
            )
            prompt = skill.analyze_bilibili_media("BV1TEST", lang="zh-CN")
            self.assertEqual(skill.state, State.WAIT_CONFIRM)
            self.assertIn("确认分析范围", prompt)

            confirmation = skill.confirm_download("1")
            self.assertIn("开始分析", confirmation)
            self.assertTrue(preparer.started.wait(timeout=1))
            self.assertEqual(skill.state, State.ANALYZING)
            self.assertIn("分析任务", skill.download_bilibili_media("BV1TEST"))

            preparer.release.set()
            deadline = time.time() + 2
            while skill.state != State.IDLE and time.time() < deadline:
                time.sleep(0.01)

            self.assertEqual(skill.state, State.IDLE)
            self.assertEqual(preparer.calls, [("BV1TEST", [1], "zh-CN")])
            self.assertTrue(any("分析材料准备完成" in reply for reply in replies))
            runs = list((Path(tmp) / "bili-project").iterdir())
            self.assertEqual(len(runs), 1)
            self.assertTrue(all((runs[0] / name).is_dir() for name in (
                "media", "subtitles", "transcripts", "analysis"
            )))

    def test_all_pages_failure_reply_does_not_request_agent_summary(self):
        class FailedPreparer:
            def prepare(self, _bvid, layout, pages=None, lang=None, info=None):
                path = layout.analysis_dir / "analysis-input.json"
                return AnalysisPreparationResult(
                    manifest_path=path,
                    report_path=path.with_suffix(".md"),
                    success_count=0,
                    failure_count=1,
                    pages=[{
                        "page": 1,
                        "part_title": "上",
                        "status": "failed",
                        "error": "ASR failed",
                    }],
                )

        replies = []
        with tempfile.TemporaryDirectory() as tmp:
            downloader = FakeDownloader()
            downloader.get_video_info = lambda _bvid: VideoInfo(
                bvid="BV1TEST", title="单P", clips=[Clip(cid=1, page=1, part="正片")]
            )
            skill = BilibiliDownloadSkill(
                downloader, Path(tmp), replies.append, analysis_preparer=FailedPreparer()
            )
            skill.analyze_bilibili_media("BV1TEST")
            deadline = time.time() + 2
            while skill.state != State.IDLE and time.time() < deadline:
                time.sleep(0.01)

            self.assertTrue(any("没有可供宿主 Agent 总结" in reply for reply in replies))
            self.assertFalse(any("请宿主 Agent 读取" in reply for reply in replies))


class SkillRunnerParserTests(unittest.TestCase):
    def test_analyze_inherits_page_language_and_device_options(self):
        args = build_parser().parse_args([
            "--bvid", "BV1TEST", "--analyze", "--pages", "1,3",
            "--subtitle-lang", "zh-CN", "--device", "cuda",
        ])
        self.assertTrue(args.analyze)
        self.assertEqual(args.pages, "1,3")
        self.assertEqual(args.subtitle_lang, "zh-CN")
        self.assertEqual(args.device, "cuda")

    def test_quality_and_page_scope_options_parse(self):
        parser = build_parser()
        args = parser.parse_args([
            "--bvid", "BV1TEST", "--pages", "1,3", "--quality", "120",
        ])
        self.assertEqual(args.quality, 120)
        self.assertEqual(args.pages, "1,3")

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([
                    "--bvid", "BV1TEST", "--pages", "1", "--all-pages",
                ])

    def test_analysis_is_mutually_exclusive_with_existing_actions(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--bvid", "BV1TEST", "--analyze", "--subtitle"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--bvid", "BV1TEST", "--analyze", "--transcribe"])


if __name__ == "__main__":
    unittest.main()
