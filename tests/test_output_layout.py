import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.output_layout import (
    BiliProjectLayout,
    media_filename,
    sanitize_path_component,
    subtitle_filename,
    titled_path_component,
    transcript_filename,
)


class OutputLayoutTests(unittest.TestCase):
    def test_creates_standard_tree_and_unique_same_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 8, 29, 14, 5, 6)
            first = BiliProjectLayout.create(
                Path(tmp), "测试/视频", "BV1TEST", now=now
            )
            second = BiliProjectLayout.create(
                Path(tmp), "测试/视频", "BV1TEST", now=now
            )

            self.assertEqual(first.project_root, Path(tmp).resolve() / "bili-project")
            self.assertEqual(
                first.run_dir.name,
                "《测试_视频》 [BV1TEST] 20260829-140506",
            )
            self.assertEqual(second.run_dir.name, first.run_dir.name + "-02")
            self.assertTrue(all(path.is_dir() for path in (
                first.media_dir,
                first.subtitles_dir,
                first.transcripts_dir,
                first.analysis_dir,
            )))

    def test_windows_safe_names_and_standard_file_names(self):
        self.assertEqual(sanitize_path_component("CON"), "_CON")
        self.assertEqual(sanitize_path_component(' a<b>:c. '), "a_b__c")
        self.assertEqual(
            media_filename("标题", "BV1TEST", 2, ".mp4"),
            "《标题》-BV1TEST-P02.mp4",
        )
        self.assertEqual(titled_path_component("《标题》"), "《标题》")
        self.assertEqual(subtitle_filename(2, "zh-CN"), "P02.zh-CN.srt")
        self.assertEqual(subtitle_filename(1, ""), "P01.unknown.srt")
        self.assertEqual(transcript_filename(12), "P12.asr.txt")


if __name__ == "__main__":
    unittest.main()
