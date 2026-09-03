import tempfile
import unittest
from pathlib import Path

from core.bili_downloader import Clip, VideoInfo
from core.bili_transcriber import BiliTranscriber


class FakeDownloader:
    def __init__(self):
        self.info_calls = 0
        self.downloaded_pages = []
        self.info = VideoInfo(
            bvid="BV1TEST",
            title="测试视频",
            owner_name="测试UP",
            clips=[
                Clip(cid=11, page=1, part="上"),
                Clip(cid=22, page=2, part="下"),
            ],
        )

    def get_video_info(self, _bvid):
        self.info_calls += 1
        return self.info

    def download_clip(self, info, clip, path, *args, **kwargs):
        self.downloaded_pages.append(clip.page)
        output = Path(path) / f"P{clip.page:02d}.m4a"
        output.write_bytes(b"audio")
        return output


class StubTranscriber(BiliTranscriber):
    def _run_paraformer(self, _audio_path):
        return "测 试 。", [[0, 100], [100, 200], [200, 300]]


class BiliTranscriberMetadataTests(unittest.TestCase):
    def test_multi_page_reuses_video_info(self):
        downloader = FakeDownloader()
        transcriber = StubTranscriber(downloader)

        with tempfile.TemporaryDirectory() as tmp:
            outputs = transcriber.transcribe_multi_p(
                "BV1TEST", Path(tmp), pages=[1, 2]
            )

            self.assertEqual(downloader.info_calls, 1)
            self.assertEqual(downloader.downloaded_pages, [1, 2])
            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(path.exists() for path in outputs))


if __name__ == "__main__":
    unittest.main()
