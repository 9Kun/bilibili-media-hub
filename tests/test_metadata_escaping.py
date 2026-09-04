import tempfile
import unittest
from pathlib import Path

from core import BiliTranscriber, Clip, VideoInfo
from scripts.bilibili_download_skill import BilibiliDownloadSkill, State


UNTRUSTED_TITLE = "演示 [点我](https://example.com) <script>alert(1)</script>"
ESCAPED_TITLE = (
    r"演示 \[点我\]\(https://example.com\) "
    r"\<script\>alert\(1\)\</script\>"
)


class MetadataEscapingTests(unittest.TestCase):
    def test_video_title_is_escaped_in_message_and_transcript_header(self):
        class FakeDownloader:
            def get_video_info(self, _bvid):
                return VideoInfo(
                    bvid="BV1TEST",
                    title=UNTRUSTED_TITLE,
                    clips=[
                        Clip(cid=1, page=1, part="上"),
                        Clip(cid=2, page=2, part="下"),
                    ],
                )

            def get_available_quality_options(self, _bvid, info=None):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            skill = BilibiliDownloadSkill(
                FakeDownloader(), Path(tmp), lambda _message: None
            )
            prompt = skill.download_bilibili_media("BV1TEST")

            self.assertEqual(skill.state, State.WAIT_CONFIRM)
            self.assertIn(ESCAPED_TITLE, prompt)
            self.assertNotIn("[点我](https://example.com)", prompt)
            self.assertNotIn("<script>", prompt)

            info = skill.pending_info
            transcript = BiliTranscriber._build_transcript(
                info.title,
                "测试UP",
                info.bvid,
                1,
                "测 试 。",
                [[0, 100], [100, 200], [200, 300]],
            )
            self.assertTrue(transcript.startswith(f"# {ESCAPED_TITLE}\n\n"))
            self.assertNotIn("[点我](https://example.com)", transcript)
            self.assertNotIn("<script>", transcript)

            # Raw metadata must remain available for filename/path sanitization.
            self.assertEqual(str(info.title), UNTRUSTED_TITLE)


if __name__ == "__main__":
    unittest.main()
