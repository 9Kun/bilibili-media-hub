import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.bili_analysis import BiliAnalysisPreparer
from core.bili_downloader import Clip, VideoInfo
from core.bili_subtitle_downloader import (
    BiliSubtitleDownloader,
    classify_subtitle_source,
)
from core.exceptions import CoreApiError, NoSubtitleError
from core.output_layout import BiliProjectLayout


class FakeDownloader:
    def __init__(self):
        self.info_calls = 0
        self.info = VideoInfo(
            bvid="BV1TEST",
            clips=[
                Clip(cid=11, page=1, part="上"),
                Clip(cid=22, page=2, part="下"),
            ],
        )

    def get_video_info(self, _bvid):
        self.info_calls += 1
        return self.info


class StubSubtitleDownloader(BiliSubtitleDownloader):
    def _query_subtitle_url(self, _bvid, cid, _lang):
        return f"https://subtitle.test/{cid}", "ai-zh" if cid == 11 else "zh-CN"

    def _fetch_subtitle_text(self, _url):
        return '{"body":[{"from":0,"to":1,"content":"字幕"}]}'


class BiliSubtitleTests(unittest.TestCase):
    def test_multi_page_reuses_video_info_and_preserves_source_language(self):
        downloader = FakeDownloader()
        subtitle = StubSubtitleDownloader(downloader)

        with tempfile.TemporaryDirectory() as tmp:
            outputs = subtitle.download_subtitle_multi_p(
                "BV1TEST", Path(tmp), pages=[1, 2]
            )

            self.assertEqual(downloader.info_calls, 1)
            self.assertEqual([lang for _, lang in outputs], ["ai-zh", "zh-CN"])
            self.assertTrue(all(path.exists() for path, _ in outputs))
            self.assertEqual(classify_subtitle_source("ai-zh"), "ai_subtitle")
            self.assertEqual(classify_subtitle_source("zh-CN"), "cc")


class BiliSubtitleQueryTests(unittest.TestCase):
    def setUp(self):
        self.downloader = FakeDownloader()
        self.downloader.http = Mock()
        self.downloader.signer = Mock()
        self.downloader.signer.enc_wbi.side_effect = lambda url: url
        self.downloader._bili_api_headers = Mock(return_value={})
        self.subtitle = BiliSubtitleDownloader(self.downloader)

    def test_api_errors_are_not_reported_as_missing_subtitles(self):
        for code in (-101, -400, -412):
            with self.subTest(code=code):
                self.downloader.http.get_json.return_value = {
                    "code": code,
                    "message": "Subtitle query rejected",
                    "data": {"subtitle": {"subtitles": []}},
                }

                with self.assertRaisesRegex(CoreApiError, str(code)) as caught:
                    self.subtitle._query_subtitle_url("BV1TEST", 11, None)

                self.assertIn("Subtitle query rejected", str(caught.exception))

    def test_malformed_responses_are_api_errors(self):
        responses = [
            None,
            [],
            {},
            {"code": 0, "data": None},
            {"code": 0, "data": {}},
            {"code": 0, "data": {"subtitle": {}}},
            {"code": 0, "data": {"subtitle": {"subtitles": None}}},
            {"code": 0, "data": {"subtitle": {"subtitles": {}}}},
            {"code": 0, "data": {"subtitle": {"subtitles": ""}}},
        ]
        for response in responses:
            with self.subTest(response=response):
                self.downloader.http.get_json.return_value = response
                with self.assertRaises(CoreApiError):
                    self.subtitle._query_subtitle_url("BV1TEST", 11, None)

    def test_successful_empty_list_still_reports_missing_subtitles(self):
        self.downloader.http.get_json.return_value = {
            "code": 0,
            "data": {"subtitle": {"subtitles": []}},
        }

        with self.assertRaises(NoSubtitleError):
            self.subtitle._query_subtitle_url("BV1TEST", 11, None)

    def test_successful_query_preserves_language_selection_and_url(self):
        self.downloader.http.get_json.return_value = {
            "code": 0,
            "data": {
                "subtitle": {
                    "subtitles": [
                        {"lan": "ai-zh", "subtitle_url": "//subtitle.test/ai.json"},
                        {
                            "lan": "zh-CN",
                            "subtitle_url": "https://subtitle.test/cc.json",
                        },
                    ]
                }
            },
        }

        self.assertEqual(
            self.subtitle._query_subtitle_url("BV1TEST", 11, "zh-CN"),
            ("https://subtitle.test/cc.json", "zh-CN"),
        )
        self.assertEqual(
            self.subtitle._query_subtitle_url("BV1TEST", 11, "en-US"),
            ("https://subtitle.test/ai.json", "ai-zh"),
        )

    def test_analysis_records_query_errors_without_starting_asr(self):
        self.downloader.http.get_json.return_value = {
            "code": -400,
            "message": "Bad Request",
            "data": None,
        }
        transcriber = Mock()
        preparer = BiliAnalysisPreparer(self.downloader, self.subtitle, transcriber)

        with tempfile.TemporaryDirectory() as tmp:
            layout = BiliProjectLayout.create(Path(tmp), "测试", "BV1TEST")
            result = preparer.prepare("BV1TEST", layout, pages=[1])

            transcriber.transcribe.assert_not_called()
            self.assertEqual((result.success_count, result.failure_count), (0, 1))
            self.assertEqual(result.pages[0]["error_stage"], "subtitle")
            self.assertIn("-400", result.pages[0]["error"])
            self.assertEqual(list(layout.subtitles_dir.iterdir()), [])
            self.assertEqual(list(layout.transcripts_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
