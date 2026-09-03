import tempfile
import unittest
from pathlib import Path

from core.bili_downloader import Clip, VideoInfo
from core.bili_subtitle_downloader import (
    BiliSubtitleDownloader,
    classify_subtitle_source,
)


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


if __name__ == "__main__":
    unittest.main()
