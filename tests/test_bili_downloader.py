import unittest
import json
import tempfile
from hashlib import md5
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from core.bili_downloader import (
    BiliDownloader,
    Clip,
    VideoInfo,
    _FileDownloader,
    _FfmpegMerger,
    _HttpClient,
    _WbiSigner,
    _derive_ffprobe_path,
    _response_total_bytes,
)
from core.exceptions import CoreApiError, NetworkError


class BiliDownloaderParsingTests(unittest.TestCase):
    def test_quality_options_use_api_descriptions_and_static_fallback(self):
        downloader = BiliDownloader.__new__(BiliDownloader)
        downloader._query_play_url = lambda *_args: {
            "data": {
                "accept_quality": [126, 120, 112],
                "accept_description": ["杜比视界", "超清 4K"],
            }
        }
        info = VideoInfo(
            bvid="BV1TEST",
            clips=[Clip(cid=1, page=1, part="正片")],
        )

        options = downloader.get_available_quality_options("BV1TEST", info=info)

        self.assertEqual([item.qn for item in options], [126, 120, 112])
        self.assertEqual(
            [item.description for item in options],
            ["杜比视界", "超清 4K", "1080P 高码率"],
        )
        self.assertEqual(
            downloader.format_quality_options(options),
            "杜比视界(126)、超清 4K(120)、1080P 高码率(112)",
        )

    def test_requested_quality_does_not_silently_downgrade(self):
        downloader = BiliDownloader.__new__(BiliDownloader)
        downloader._query_play_url = lambda *_args: {
            "data": {"quality": 80, "accept_quality": [80, 64], "dash": {}}
        }
        info = VideoInfo(
            bvid="BV1TEST",
            title="测试",
            clips=[Clip(cid=1, page=1, part="正片")],
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(CoreApiError, "Requested quality 120 unavailable"):
                downloader._download_single_clip(
                    info, info.clips[0], Path(tmp), None, "video", 120
                )

    def test_normalizes_bvid_from_text(self):
        self.assertEqual(
            BiliDownloader._normalize_bvid("视频 BV1Zj411w7mz"),
            "BV1Zj411w7mz",
        )

    def test_parses_login_cookie_string(self):
        cookies = _HttpClient._parse_cookie_string(
            "SESSDATA=session-value; bili_jct=csrf-value"
        )

        self.assertEqual(cookies["SESSDATA"], "session-value")
        self.assertEqual(cookies["bili_jct"], "csrf-value")

    def test_play_url_falls_back_when_wbi_response_is_invalid(self):
        requested_urls = []

        class FakeHttp:
            has_login_cookie = True

            def get_json(self, url, headers, with_fingerprint_cookie):
                requested_urls.append(url)
                if "/wbi/playurl" in url:
                    raise CoreApiError("Invalid JSON response")
                return {"code": 0, "data": {"quality": 32}}

        class FakeSigner:
            @staticmethod
            def enc_wbi(url):
                return url + "&w_rid=test"

        downloader = BiliDownloader.__new__(BiliDownloader)
        downloader.user_agent = BiliDownloader.DEFAULT_UA
        downloader.http = FakeHttp()
        downloader.signer = FakeSigner()

        result = downloader._query_play_url("BV1TEST", 123, 32)

        self.assertEqual(result["data"]["quality"], 32)
        self.assertIn("/wbi/playurl", requested_urls[0])
        self.assertIn("/x/player/playurl", requested_urls[1])

    def test_wbi_signer_sends_the_sorted_query_it_signed(self):
        signer = _WbiSigner.__new__(_WbiSigner)
        signer._wbi_img = "0" * 64

        with patch("core.bili_downloader.time.time", return_value=100):
            signed = signer.enc_wbi("https://example.test/api?b=2&a=1")

        query = "a=1&b=2&wts=100"
        expected = md5((query + "0" * 32).encode("utf-8")).hexdigest()
        self.assertEqual(
            signed,
            f"https://example.test/api?{query}&w_rid={expected}",
        )

    def test_response_size_prefers_content_range(self):
        headers = httpx.Headers({
            "Content-Range": "bytes 0-0/406288612",
            "Content-Length": "1",
        })
        self.assertEqual(_response_total_bytes(headers, 0), 406288612)

    def test_ffprobe_path_falls_back_to_path_lookup(self):
        with patch("core.bili_downloader.Path.exists", return_value=False), patch(
            "core.bili_downloader.shutil.which", return_value=r"C:\tools\ffprobe.exe"
        ):
            self.assertEqual(
                _derive_ffprobe_path(r"D:\release\ffmpeg.exe"),
                r"C:\tools\ffprobe.exe",
            )

    def test_download_retries_then_switches_cdn(self):
        downloader = _FileDownloader("ua", 0)
        calls = []

        def fake_download(url, dst, headers, listener):
            calls.append(url)
            if url == "https://cdn-1.test/file":
                raise NetworkError("timeout")
            Path(dst).write_bytes(b"ok")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "video.m4s"
            with patch.object(downloader, "_single_thread_download", side_effect=fake_download), patch(
                "core.bili_downloader.time.sleep", return_value=None
            ):
                downloader.download_candidates(
                    ["https://cdn-1.test/file", "https://cdn-2.test/file"],
                    output,
                    {},
                )

            self.assertEqual(calls.count("https://cdn-1.test/file"), 3)
            self.assertEqual(calls[-1], "https://cdn-2.test/file")
            self.assertEqual(output.read_bytes(), b"ok")

    def test_unrecoverable_download_cleans_partial_files(self):
        downloader = _FileDownloader("ua", 2)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "video.m4s"
            Path(str(output) + ".part").write_bytes(b"partial")
            Path(str(output) + ".part0").write_bytes(b"partial")
            with patch.object(
                downloader, "_multi_thread_download", side_effect=NetworkError("down")
            ), patch("core.bili_downloader.time.sleep", return_value=None):
                with self.assertRaises(NetworkError):
                    downloader.download_candidates(["https://cdn.test/file"], output, {})

            self.assertFalse(Path(str(output) + ".part").exists())
            self.assertFalse(Path(str(output) + ".part0").exists())

    def test_ffprobe_result_is_parsed(self):
        payload = {
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "218.56", "size": "406288612"},
        }
        process = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "video.mp4"
            media.write_bytes(b"media")
            with patch("core.bili_downloader.subprocess.run", return_value=process):
                result = _FfmpegMerger.probe("ffprobe", media)

        self.assertEqual((result.width, result.height), (3840, 2160))
        self.assertEqual(result.video_codec, "hevc")
        self.assertEqual(result.audio_codec, "aac")
        self.assertEqual(result.size_bytes, 406288612)


if __name__ == "__main__":
    unittest.main()
