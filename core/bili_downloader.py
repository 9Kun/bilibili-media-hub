import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from .exceptions import (
    CoreApiError,
    FfmpegExecutionError,
    FfmpegNotFoundError,
    InvalidBvidError,
    NetworkError,
)
from .output_layout import media_stem


ProgressListener = Callable[[str, float], None]


QUALITY_LABELS = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩色",
    120: "4K 超清",
    116: "1080P 60帧",
    112: "1080P 高码率",
    80: "1080P",
    74: "720P 60帧",
    64: "720P",
    32: "480P",
    16: "360P",
    6: "240P",
}


@dataclass
class Clip:
    cid: int
    page: int
    part: str


@dataclass
class VideoInfo:
    bvid: str = ""
    aid: int = 0
    title: str = ""
    desc: str = ""
    cover: str = ""
    owner_name: str = ""
    clips: List[Clip] = field(default_factory=list)


@dataclass(frozen=True)
class QualityOption:
    qn: int
    description: str


@dataclass(frozen=True)
class MediaValidation:
    path: Path
    size_bytes: int
    duration_seconds: float
    width: Optional[int]
    height: Optional[int]
    video_codec: Optional[str]
    audio_codec: Optional[str]

    def summary(self) -> str:
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width}×{self.height}")
        if self.video_codec:
            parts.append(self.video_codec.upper())
        if self.audio_codec:
            parts.append(self.audio_codec.upper())
        parts.append(f"{self.duration_seconds:.2f}秒")
        parts.append(f"{self.size_bytes / 1024 / 1024:.2f} MB")
        return " / ".join(parts)


class BiliDownloader:
    DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:93.0) Gecko/20100101 Firefox/93.0"

    def __init__(
        self,
        user_agent: Optional[str] = None,
        ffmpeg_path: Optional[str] = None,
        multi_thread_count: int = 0,
        cookies_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
    ):
        self.user_agent = user_agent.strip() if user_agent and user_agent.strip() else self.DEFAULT_UA
        self.ffmpeg_path = ffmpeg_path.strip() if ffmpeg_path and ffmpeg_path.strip() else "ffmpeg"
        self.ffprobe_path = (
            ffprobe_path.strip()
            if ffprobe_path and ffprobe_path.strip()
            else _derive_ffprobe_path(self.ffmpeg_path)
        )
        self.http = _HttpClient(self.user_agent, cookies_path)
        self.signer = _WbiSigner(self.http)
        self.downloader = _FileDownloader(self.user_agent, max(0, int(multi_thread_count)))
        self._validations: Dict[str, MediaValidation] = {}

    def get_video_info(self, bvid: str) -> VideoInfo:
        normalized = self._normalize_bvid(bvid)
        headers = self._bili_api_headers(normalized)

        pagelist_url = f"https://api.bilibili.com/x/player/pagelist?bvid={normalized}&jsonp=jsonp"
        pagelist_obj = self.http.get_json(pagelist_url, headers, with_fingerprint_cookie=True)
        pages = pagelist_obj.get("data", [])
        if not pages:
            raise InvalidBvidError(f"Empty pagelist for bvid: {normalized}")

        detail_url = (
            "https://api.bilibili.com/x/web-interface/wbi/view/detail?"
            f"platform=web&page_no=1&p=1&need_operation_card=1&web_rm_repeat=1&need_elec=1&bvid={normalized}"
        )
        detail_url = self.signer.enc_wbi(detail_url)
        detail_raw = self.http.get_json(detail_url, headers, with_fingerprint_cookie=True)

        try:
            view = detail_raw["data"]["View"]
        except Exception as exc:
            raise CoreApiError(f"Invalid detail response for bvid: {normalized}") from exc

        info = VideoInfo(
            bvid=normalized,
            aid=int(view.get("aid", 0) or 0),
            title=view.get("title", "") or "",
            desc=view.get("desc", "") or "",
            cover=view.get("pic", "") or "",
            owner_name=(view.get("owner") or {}).get("name", "") or "",
        )

        for i, p in enumerate(pages):
            info.clips.append(
                Clip(
                    cid=int(p.get("cid", 0) or 0),
                    page=int(p.get("page", i + 1) or (i + 1)),
                    part=(p.get("part", f"P{i + 1}") or f"P{i + 1}"),
                )
            )
        return info

    def get_available_quality_options(
        self,
        bvid: str,
        info: Optional[VideoInfo] = None,
        page: int = 1,
    ) -> List[QualityOption]:
        info = info or self.get_video_info(bvid)
        if not info.clips:
            return []

        clip = next((item for item in info.clips if item.page == int(page)), info.clips[0])
        play = self._query_play_url(info.bvid, clip.cid, 127)
        data = play.get("data") or {}

        quality_list: List[int] = []
        accept = data.get("accept_quality")
        descriptions = data.get("accept_description")
        description_by_qn: Dict[int, str] = {}
        if isinstance(accept, list) and isinstance(descriptions, list):
            for raw_qn, raw_description in zip(accept, descriptions):
                try:
                    description_by_qn[int(raw_qn)] = str(raw_description or "").strip()
                except (TypeError, ValueError):
                    continue
        if isinstance(accept, list):
            for q in accept:
                try:
                    quality_list.append(int(q))
                except Exception:
                    pass
        else:
            dash = data.get("dash") or {}
            videos = dash.get("video") or []
            for v in videos:
                qn = int(v.get("id", 0) or 0)
                if qn > 0 and qn not in quality_list:
                    quality_list.append(qn)

        quality_list.sort(reverse=True)
        return [
            QualityOption(qn, description_by_qn.get(qn) or QUALITY_LABELS.get(qn, f"QN {qn}"))
            for qn in quality_list
        ]

    def get_available_qualities(
        self,
        bvid: str,
        info: Optional[VideoInfo] = None,
        page: int = 1,
    ) -> List[int]:
        return [option.qn for option in self.get_available_quality_options(bvid, info, page)]

    @staticmethod
    def format_quality_options(options: List[QualityOption]) -> str:
        if not options:
            return "自动选择最高可用"
        return "、".join(f"{item.description}({item.qn})" for item in options)

    def download(
        self,
        bvid: str,
        path: str,
        progress_cb: Optional[ProgressListener] = None,
        media_type: str = "video",
        quality: Optional[int] = None,
        info: Optional[VideoInfo] = None,
    ) -> Path:
        info = info or self.get_video_info(bvid)
        if not info.clips:
            raise InvalidBvidError(f"No clips for bvid: {info.bvid}")
        save_dir = Path(path)
        save_dir.mkdir(parents=True, exist_ok=True)

        clip = info.clips[0]
        return self._download_single_clip(
            info, clip, save_dir, progress_cb, media_type, quality
        )

    def download_by_page(
        self,
        bvid: str,
        path: str,
        page: int,
        progress_cb: Optional[ProgressListener] = None,
        media_type: str = "video",
        quality: Optional[int] = None,
        info: Optional[VideoInfo] = None,
    ) -> Path:
        info = info or self.get_video_info(bvid)
        if not info.clips:
            raise InvalidBvidError(f"No clips for bvid: {info.bvid}")

        target_page = int(page)
        target_clip = next((clip for clip in info.clips if clip.page == target_page), None)
        if target_clip is None:
            raise InvalidBvidError(f"Page {target_page} not found for bvid: {info.bvid}")

        save_dir = Path(path)
        save_dir.mkdir(parents=True, exist_ok=True)
        return self._download_single_clip(
            info, target_clip, save_dir, progress_cb, media_type, quality
        )

    def download_clip(
        self,
        info: VideoInfo,
        clip: Clip,
        path: str,
        progress_cb: Optional[ProgressListener] = None,
        media_type: str = "video",
        quality: Optional[int] = None,
    ) -> Path:
        save_dir = Path(path)
        save_dir.mkdir(parents=True, exist_ok=True)
        return self._download_single_clip(
            info, clip, save_dir, progress_cb, media_type, quality
        )

    def download_all_pages(
        self,
        bvid: str,
        path: str,
        pages: Optional[List[int]] = None,
        progress_cb: Optional[ProgressListener] = None,
        media_type: str = "video",
        quality: Optional[int] = None,
        info: Optional[VideoInfo] = None,
    ) -> List[Path]:
        info = info or self.get_video_info(bvid)
        if not info.clips:
            raise InvalidBvidError(f"No clips for bvid: {info.bvid}")

        clip_by_page = {clip.page: clip for clip in info.clips}
        if pages is None:
            target_pages = [clip.page for clip in info.clips]
        else:
            target_pages = sorted({int(page) for page in pages if int(page) > 0})
            if not target_pages:
                raise InvalidBvidError("pages is empty")

            missing_pages = [page for page in target_pages if page not in clip_by_page]
            if missing_pages:
                raise InvalidBvidError(f"Pages not found for bvid {info.bvid}: {missing_pages}")

        save_dir = Path(path)
        save_dir.mkdir(parents=True, exist_ok=True)

        outputs: List[Path] = []
        total = len(target_pages)
        for idx, page in enumerate(target_pages):
            clip = clip_by_page[page]

            def _wrapped_progress(stage: str, percent: float, i=idx, t=total) -> None:
                if progress_cb:
                    current = min(100.0, max(0.0, percent))
                    progress_cb(stage, ((i * 100.0) + current) / t)

            output = self._download_single_clip(
                info,
                clip,
                save_dir,
                _wrapped_progress if progress_cb else None,
                media_type,
                quality,
            )
            outputs.append(output)

        if len(outputs) != len(target_pages) or any(not output.exists() for output in outputs):
            raise CoreApiError(
                f"Multi-page validation failed: expected={len(target_pages)} actual={len(outputs)}"
            )
        return outputs

    def _download_single_clip(
        self,
        info: VideoInfo,
        clip: Clip,
        save_dir: Path,
        progress_cb: Optional[ProgressListener],
        media_type: str,
        quality: Optional[int],
    ) -> Path:
        media_kind = (media_type or "video").strip().lower()
        audio_only = media_kind == "audio"

        requested_qn = int(quality) if quality is not None else 127
        if requested_qn <= 0:
            raise CoreApiError(f"Invalid quality: {requested_qn}")
        play = self._query_play_url(info.bvid, clip.cid, requested_qn)
        data = play.get("data")
        if not isinstance(data, dict):
            raise CoreApiError(f"No play data for bvid: {info.bvid}")
        actual_qn = int(data.get("quality", 0) or 0)
        if quality is not None and actual_qn != requested_qn:
            available = data.get("accept_quality") or []
            raise CoreApiError(
                f"Requested quality {requested_qn} unavailable for P{clip.page}; "
                f"actual={actual_qn or 'unknown'}, available={available}"
            )

        base_name = media_stem(
            info.title if info.title else info.bvid,
            info.bvid,
            clip.page,
        )
        output_ext = ".m4a" if audio_only else ".mp4"
        output = save_dir / f"{base_name}{output_ext}"

        dash = data.get("dash")
        if isinstance(dash, dict):
            video_urls, audio_urls = self._choose_dash(
                dash, int(data.get("quality", 80) or 80)
            )
            if audio_only:
                if not audio_urls:
                    raise CoreApiError(f"No dash audio url for {info.bvid}")

                a_tmp = save_dir / f"{base_name}_audio.m4s"
                self.downloader.download_candidates(
                    audio_urls,
                    a_tmp,
                    self._bili_media_headers(info.bvid),
                    lambda stage, p: progress_cb("audio", p) if progress_cb else None,
                )
                if progress_cb:
                    progress_cb("merge", 99.0)
                output = _FfmpegMerger.extract_audio_prefer_m4a(self.ffmpeg_path, a_tmp, output)
                try:
                    a_tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                if progress_cb:
                    progress_cb("done", 100.0)
                return self._validate_and_record(output, media_kind)

            if not video_urls:
                raise CoreApiError(f"No dash video url for {info.bvid}")

            v_tmp = save_dir / f"{base_name}_video.m4s"
            a_tmp = save_dir / f"{base_name}_audio.m4s"

            self.downloader.download_candidates(
                video_urls,
                v_tmp,
                self._bili_media_headers(info.bvid),
                lambda stage, p: progress_cb("video", p * 0.50) if progress_cb else None,
            )

            if audio_urls:
                self.downloader.download_candidates(
                    audio_urls,
                    a_tmp,
                    self._bili_media_headers(info.bvid),
                    lambda stage, p: progress_cb("audio", 50.0 + p * 0.50) if progress_cb else None,
                )
                if progress_cb:
                    progress_cb("merge", 99.0)
                _FfmpegMerger.merge_av(self.ffmpeg_path, v_tmp, a_tmp, output)
                try:
                    a_tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                if progress_cb:
                    progress_cb("merge", 99.0)
                _FfmpegMerger.remux_single(self.ffmpeg_path, v_tmp, output)

            try:
                v_tmp.unlink(missing_ok=True)
            except Exception:
                pass

            if progress_cb:
                progress_cb("done", 100.0)
            return self._validate_and_record(output, media_kind)

        durl = data.get("durl")
        if not isinstance(durl, list) or not durl:
            raise CoreApiError(f"No dash/durl links for {info.bvid}")

        if len(durl) == 1:
            media_urls = _item_urls(durl[0], primary_key="url")
            if not media_urls:
                raise CoreApiError(f"No durl media url for {info.bvid}")

            if audio_only:
                media_tmp = save_dir / f"{base_name}_full.mp4"
                self.downloader.download_candidates(
                    media_urls,
                    media_tmp,
                    self._bili_media_headers(info.bvid),
                    progress_cb,
                )
                if progress_cb:
                    progress_cb("merge", 99.0)
                output = _FfmpegMerger.extract_audio_prefer_m4a(self.ffmpeg_path, media_tmp, output)
                try:
                    media_tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                self.downloader.download_candidates(
                    media_urls,
                    output,
                    self._bili_media_headers(info.bvid),
                    progress_cb,
                )

            if progress_cb:
                progress_cb("done", 100.0)
            return self._validate_and_record(output, media_kind)

        parts: List[Path] = []
        total = len(durl)
        for idx, item in enumerate(durl):
            order = int(item.get("order", idx + 1) or (idx + 1))
            p_file = save_dir / f"{base_name}-part{order}.flv"

            def _part_progress(stage: str, percent: float, i=idx, t=total) -> None:
                if progress_cb:
                    base = (i * 100.0) / t
                    step = percent / t
                    progress_cb("flv-part", base + step)

            media_urls = _item_urls(item, primary_key="url")
            if not media_urls:
                raise CoreApiError(f"No flv part url for {info.bvid}")
            self.downloader.download_candidates(
                media_urls,
                p_file,
                self._bili_media_headers(info.bvid),
                _part_progress,
            )
            parts.append(p_file)

        if progress_cb:
            progress_cb("merge", 99.0)
        if audio_only:
            merged_tmp = save_dir / f"{base_name}_merged.mp4"
            _FfmpegMerger.concat(self.ffmpeg_path, parts, merged_tmp)
            output = _FfmpegMerger.extract_audio_prefer_m4a(self.ffmpeg_path, merged_tmp, output)
            try:
                merged_tmp.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            _FfmpegMerger.concat(self.ffmpeg_path, parts, output)

        for p in parts:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

        if progress_cb:
            progress_cb("done", 100.0)
        return self._validate_and_record(output, media_kind)

    def _validate_and_record(self, output: Path, media_type: str) -> Path:
        resolved = output.resolve()
        validation = _FfmpegMerger.probe(self.ffprobe_path, resolved)
        if validation.size_bytes <= 0 or validation.duration_seconds <= 0:
            raise FfmpegExecutionError(f"ffprobe validation failed for {resolved}")
        if media_type == "audio" and not validation.audio_codec:
            raise FfmpegExecutionError(f"ffprobe found no audio stream: {resolved}")
        if media_type != "audio" and not validation.video_codec:
            raise FfmpegExecutionError(f"ffprobe found no video stream: {resolved}")
        self._validations[str(resolved)] = validation
        return resolved

    def get_validation(self, path: Path) -> Optional[MediaValidation]:
        return self._validations.get(str(Path(path).resolve()))

    @staticmethod
    def cleanup_temporary_files(directory: Path) -> None:
        directory = Path(directory)
        if not directory.is_dir():
            return
        patterns = (
            "*.part",
            "*.part[0-9]*",
            "*_video.m4s",
            "*_audio.m4s",
            "*-part*.flv",
            "*_full.mp4",
            "*_merged.mp4",
        )
        for pattern in patterns:
            for path in directory.glob(pattern):
                try:
                    if path.is_file():
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _query_play_url(self, bvid: str, cid: int, qn: int) -> dict:
        query = (
            f"cid={cid}&bvid={bvid}&qn={qn}&type=&otype=json&fnver=0&fnval=4048&fourk=1"
        )
        wbi_url = (
            "https://api.bilibili.com/x/player/wbi/playurl?"
            f"{query}"
        )
        if not self.http.has_login_cookie:
            wbi_url += "&try_look=1"
        headers = self._bili_api_headers(bvid)
        try:
            return self.http.get_json(
                self.signer.enc_wbi(wbi_url),
                headers,
                with_fingerprint_cookie=True,
            )
        except CoreApiError:
            plain_url = f"https://api.bilibili.com/x/player/playurl?{query}"
            if not self.http.has_login_cookie:
                plain_url += "&try_look=1"
            return self.http.get_json(
                plain_url,
                headers,
                with_fingerprint_cookie=True,
            )

    @staticmethod
    def _normalize_bvid(bvid: str) -> str:
        if bvid is None:
            raise InvalidBvidError("bvid is null")
        m = re.search(r"(BV[0-9A-Za-z]+)", bvid, re.IGNORECASE)
        if not m:
            raise InvalidBvidError(f"Invalid bvid: {bvid}")
        result = m.group(1)
        return "BV" + result[2:]

    def _bili_api_headers(self, av_id_or_bvid: str) -> Dict[str, str]:
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.8",
            "Connection": "keep-alive",
            "Host": "api.bilibili.com",
            "Referer": f"https://www.bilibili.com/video/{av_id_or_bvid}",
            "User-Agent": self.user_agent,
            "X-Requested-With": "ShockwaveFlash/28.0.0.137",
        }

    def _bili_media_headers(self, bvid: str) -> Dict[str, str]:
        return {
            "Referer": f"https://www.bilibili.com/video/{bvid}",
            "User-Agent": self.user_agent,
        }

    @staticmethod
    def _choose_dash(dash: dict, preferred_qn: int) -> Tuple[List[str], List[str]]:
        video_urls: List[str] = []
        audio_urls: List[str] = []

        videos = dash.get("video") or []
        audios = dash.get("audio") or []

        if isinstance(videos, list) and videos:
            sorted_v = sorted(videos, key=lambda x: abs(int(x.get("id", preferred_qn) or preferred_qn) - preferred_qn))
            v = sorted_v[0]
            video_urls = _item_urls(v)

        # 对齐 Java：普通音频 + (可选)杜比 + FLAC，按优先级选 id
        audio_candidates: List[dict] = []
        if isinstance(audios, list):
            audio_candidates.extend([a for a in audios if isinstance(a, dict)])

        dolby = dash.get("dolby") or {}
        if preferred_qn == 126 and isinstance(dolby, dict):
            dolby_audios = dolby.get("audio") or []
            if isinstance(dolby_audios, list):
                audio_candidates.extend([a for a in dolby_audios if isinstance(a, dict)])

        flac = dash.get("flac") or {}
        if isinstance(flac, dict):
            flac_audio = flac.get("audio")
            if isinstance(flac_audio, dict):
                audio_candidates.append(flac_audio)

        if audio_candidates:
            priorities = [30280, 30232, 30216, -1, 30251, 30250]
            chosen_audio = None
            for p in priorities:
                if p == -1:
                    chosen_audio = audio_candidates[0]
                else:
                    for a in audio_candidates:
                        if int(a.get("id", 0) or 0) == p:
                            chosen_audio = a
                            break
                if chosen_audio is not None:
                    break

            if chosen_audio is None:
                chosen_audio = audio_candidates[0]

            audio_urls = _item_urls(chosen_audio)

        return video_urls, audio_urls


class _WbiSigner:
    MIXIN_ARRAY = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    ]

    def __init__(self, http_client: "_HttpClient"):
        self._http = http_client
        self._wbi_img: Optional[str] = None
        self._lock = threading.Lock()

    def enc_wbi(self, url: str) -> str:
        self._ensure_wbi_url()
        assert self._wbi_img is not None

        mixin_key = self._get_mixin_key(self._wbi_img)
        wts = f"wts={int(time.time())}"

        q_idx = url.find("?")
        if q_idx >= 0:
            base_url = url[:q_idx]
            raw = url[q_idx + 1 :]
            sep = "" if not raw else "&"
            raw = f"{raw}{sep}{wts}"
            params = raw.split("&")
            encoded = []
            for p in params:
                kv = p.split("=", 1)
                key = quote(kv[0], safe="")
                value = self._encode_url_preserving_percent(kv[1]) if len(kv) >= 2 else ""
                encoded.append(f"{key}={value}")
            encoded.sort()
            param_encoded_sorted = "&".join(encoded)
        else:
            base_url = url
            param_encoded_sorted = wts

        md5 = hashlib.md5((param_encoded_sorted + mixin_key).encode("utf-8")).hexdigest()
        return f"{base_url}?{param_encoded_sorted}&w_rid={md5}"

    def _ensure_wbi_url(self) -> None:
        if self._wbi_img is not None:
            return
        with self._lock:
            if self._wbi_img is not None:
                return
            nav = self._http.get_json(
                "https://api.bilibili.com/x/web-interface/nav",
                self._common_headers(),
                with_fingerprint_cookie=True,
            )
            wbi = nav.get("data", {}).get("wbi_img", {})
            img_url = wbi.get("img_url", "")
            sub_url = wbi.get("sub_url", "")
            if not img_url or not sub_url:
                raise CoreApiError("wbi_img missing from nav response")
            self._wbi_img = self._file_name_without_ext(img_url) + self._file_name_without_ext(sub_url)

    @staticmethod
    def _file_name_without_ext(url: str) -> str:
        s = url.rfind("/")
        e = url.find(".", s)
        return url[s + 1 : e]

    @classmethod
    def _get_mixin_key(cls, content: str) -> str:
        return "".join(content[i] for i in cls.MIXIN_ARRAY[:32])

    @staticmethod
    def _encode_url_preserving_percent(raw: str) -> str:
        if "%" in raw:
            return raw.replace("+", "%20")
        return quote(raw, safe="").replace("+", "%20")

    @staticmethod
    def _common_headers() -> Dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.8",
            "Connection": "keep-alive",
            "User-Agent": BiliDownloader.DEFAULT_UA,
        }


def _item_urls(item: dict, primary_key: str = "base_url") -> List[str]:
    primary = item.get(primary_key) or item.get("baseUrl")
    backups = item.get("backup_url") or item.get("backupUrl") or []
    candidates = [primary] + (list(backups) if isinstance(backups, list) else [])
    result: List[str] = []
    for url in candidates:
        if isinstance(url, str) and url and url not in result:
            result.append(url)
    return result


def _derive_ffprobe_path(ffmpeg_path: str) -> str:
    ffmpeg = Path(ffmpeg_path)
    if ffmpeg.parent != Path(".") or ffmpeg.suffix:
        suffix = ffmpeg.suffix or (".exe" if os.name == "nt" else "")
        sibling = ffmpeg.with_name(f"ffprobe{suffix}")
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


class _HttpClient:
    def __init__(self, user_agent: str, cookies_path: Optional[str] = None):
        self.user_agent = user_agent
        self._fingerprint_cookies: Optional[Dict[str, str]] = None
        self._login_cookies: Dict[str, str] = self._load_login_cookies(cookies_path)
        self._lock = threading.Lock()

    @property
    def has_login_cookie(self) -> bool:
        return bool(self._login_cookies.get("SESSDATA"))

    def get_json(self, url: str, headers: Optional[Dict[str, str]], with_fingerprint_cookie: bool) -> dict:
        text = self.get(url, headers, with_fingerprint_cookie)
        try:
            return httpx.Response(200, text=text).json()
        except Exception as exc:
            raise CoreApiError(f"Invalid JSON response from {url}") from exc

    def get(self, url: str, headers: Optional[Dict[str, str]], with_fingerprint_cookie: bool) -> str:
        req_headers = dict(headers or {})
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = self.user_agent

        cookies = None
        if with_fingerprint_cookie:
            cookies = self._fingerprint_cookies_dict().copy()
            # Java 语义：全局登录 cookie 与指纹 cookie 合并发送
            cookies.update(self._login_cookies)
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url, headers=req_headers, cookies=cookies)
            if resp.status_code >= 400 and not resp.text:
                raise NetworkError(f"HTTP {resp.status_code} with empty response for {url}")
            return resp.text
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc)) from exc

    def _fingerprint_cookies_dict(self) -> Dict[str, str]:
        if self._fingerprint_cookies is not None:
            return self._fingerprint_cookies

        with self._lock:
            if self._fingerprint_cookies is not None:
                return self._fingerprint_cookies

            kv: Dict[str, str] = {}
            try:
                with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                    resp = client.get("https://www.bilibili.com/", headers={"User-Agent": self.user_agent})
                for c in resp.cookies.jar:
                    kv[c.name] = c.value
            except httpx.HTTPError as exc:
                raise NetworkError(str(exc)) from exc

            kv["i-wanna-go-back"] = "-1"
            kv["b_lsid"] = f"{_random_hex(8)}_{format(int(time.time() * 1000), 'X')}"
            kv["_uuid"] = uuid.uuid4().hex + "infoc"
            kv["buvid_fp"] = _random_hex(32)
            kv["fingerprint"] = kv["buvid_fp"]

            try:
                with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                    spi = client.get(
                        "https://api.bilibili.com/x/frontend/finger/spi",
                        headers={"User-Agent": self.user_agent},
                        cookies=kv,
                    )
                obj = spi.json()
                b4 = (obj.get("data") or {}).get("b_4", "")
                if b4:
                    kv["buvid4"] = b4
            except Exception:
                # 与 Java 行为对齐：该字段获取失败不终止流程
                pass

            self._fingerprint_cookies = kv
            return self._fingerprint_cookies

    def _load_login_cookies(self, cookies_path: Optional[str]) -> Dict[str, str]:
        # 优先从环境变量读取（推荐方式，避免会话凭据落入本地明文文件）
        env_cookies = os.environ.get("BILI_COOKIES", "").strip()
        if env_cookies:
            return self._parse_cookie_string(env_cookies)

        # 仅当显式传入 cookies_path 时才读文件（宿主环境高级用法，不作为默认引导）
        if not (cookies_path and cookies_path.strip()):
            return {}

        p = Path(cookies_path.strip())
        if not p.exists() or not p.is_file():
            return {}

        content = ""
        for enc in ("utf-8", "gbk"):
            try:
                content = p.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
            except OSError:
                return {}
        if not content:
            return {}

        first_line = content.splitlines()[0] if content.splitlines() else ""
        return self._parse_cookie_string(first_line)

    @staticmethod
    def _parse_cookie_string(raw: str) -> Dict[str, str]:
        if not raw:
            return {}
        cleaned = (
            raw.replace("|", "")
            .replace("\r", "")
            .replace("\n", "")
            .replace(" ", "")
            .replace("[", "")
            .replace("]", "")
            .replace('"', "")
        )
        cookies: Dict[str, str] = {}
        for token in re.split(r"[,;&]", cleaned):
            if not token or "=" not in token:
                continue
            k, v = token.split("=", 1)
            if k and v:
                cookies[k] = v
        return cookies


class _FileDownloader:
    RETRY_DELAYS = (0.0, 0.5, 1.5)

    def __init__(self, user_agent: str, multi_thread_count: int):
        self.user_agent = user_agent
        self.multi_thread_count = multi_thread_count
        self.timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

    def download(
        self,
        url: str,
        dst: Path,
        headers: Optional[Dict[str, str]],
        listener: Optional[ProgressListener] = None,
    ) -> None:
        self.download_candidates([url], dst, headers, listener)

    def download_candidates(
        self,
        urls: List[str],
        dst: Path,
        headers: Optional[Dict[str, str]],
        listener: Optional[ProgressListener] = None,
    ) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        candidates = [url for url in urls if isinstance(url, str) and url]
        if not candidates:
            raise NetworkError("No download URL available")

        errors: List[str] = []
        for url_index, url in enumerate(candidates):
            for attempt, delay in enumerate(self.RETRY_DELAYS, start=1):
                if delay:
                    time.sleep(delay)
                try:
                    if self.multi_thread_count > 1 and not re.search(
                        r"(github|ffmpeg|\.jpg|\.png|\.webp|\.xml)", url
                    ):
                        self._multi_thread_download(url, dst, headers, listener)
                    else:
                        self._single_thread_download(url, dst, headers, listener)
                    if listener:
                        listener("download", 100.0)
                    return
                except NetworkError as exc:
                    errors.append(
                        f"cdn={url_index + 1}/{len(candidates)} attempt={attempt}: {exc}"
                    )

        self.cleanup_partials(dst)
        raise NetworkError("All CDN download attempts failed: " + " | ".join(errors))

    def _single_thread_download(
        self,
        url: str,
        dst: Path,
        headers: Optional[Dict[str, str]],
        listener: Optional[ProgressListener],
    ) -> None:
        part = Path(str(dst) + ".part")
        offset = part.stat().st_size if part.exists() else 0
        h = dict(headers or {})
        h["User-Agent"] = self.user_agent
        if offset > 0:
            h["range"] = f"bytes={offset}-"

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                with client.stream("GET", url, headers=h) as resp:
                    resp.raise_for_status()
                    if offset > 0 and resp.status_code == 200:
                        offset = 0
                    total = _response_total_bytes(resp.headers, offset)
                    mode = "ab" if offset > 0 else "wb"
                    with open(part, mode) as f:
                        downloaded = offset
                        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            if listener and total > 0:
                                listener("download", min(100.0, downloaded * 100.0 / total))
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc)) from exc

        os.replace(part, dst)
        if listener:
            listener("download", 100.0)

    def _multi_thread_download(
        self,
        url: str,
        dst: Path,
        headers: Optional[Dict[str, str]],
        listener: Optional[ProgressListener],
    ) -> None:
        total = self._probe_size(url, headers)
        if total <= 0:
            self._single_thread_download(url, dst, headers, listener)
            return

        part_size = total // self.multi_thread_count
        temp_parts = [Path(f"{dst}.part{i}") for i in range(self.multi_thread_count)]

        def _download_part(i: int) -> None:
            p = temp_parts[i]
            min_b = i * part_size
            max_b = total - 1 if i == self.multi_thread_count - 1 else (min_b + part_size - 1)
            offset = p.stat().st_size if p.exists() else 0
            if offset >= (max_b - min_b + 1):
                return
            h = dict(headers or {})
            h["User-Agent"] = self.user_agent
            h["range"] = f"bytes={min_b + offset}-{max_b}"
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    with client.stream("GET", url, headers=h) as resp:
                        resp.raise_for_status()
                        if resp.status_code != 206:
                            raise NetworkError("CDN ignored byte range request")
                        with open(p, "ab") as f:
                            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                                if chunk:
                                    f.write(chunk)
            except httpx.HTTPError as exc:
                raise NetworkError(str(exc)) from exc

        with ThreadPoolExecutor(max_workers=self.multi_thread_count) as executor:
            futures = [executor.submit(_download_part, i) for i in range(self.multi_thread_count)]
            for fut in as_completed(futures):
                fut.result()

        dst.unlink(missing_ok=True)
        with open(dst, "wb") as out:
            for p in temp_parts:
                with open(p, "rb") as src:
                    while True:
                        data = src.read(4 * 1024 * 1024)
                        if not data:
                            break
                        out.write(data)
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    def _probe_size(self, url: str, headers: Optional[Dict[str, str]]) -> int:
        h = dict(headers or {})
        h["User-Agent"] = self.user_agent
        h["Range"] = "bytes=0-0"
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                with client.stream("GET", url, headers=h) as resp:
                    resp.raise_for_status()
                    if resp.status_code != 206 or not resp.headers.get("Content-Range"):
                        return -1
                    return _response_total_bytes(resp.headers, 0)
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc)) from exc

    @staticmethod
    def cleanup_partials(dst: Path) -> None:
        targets = [Path(str(dst) + ".part")]
        targets.extend(dst.parent.glob(dst.name + ".part[0-9]*"))
        for path in targets:
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def _response_total_bytes(headers: httpx.Headers, offset: int) -> int:
    content_range = headers.get("Content-Range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        return int(match.group(1))
    content_length = headers.get("Content-Length", "")
    if content_length.isdigit():
        return offset + int(content_length)
    return -1


class _FfmpegMerger:
    @staticmethod
    def probe(ffprobe_path: str, media: Path) -> MediaValidation:
        cmd = [
            ffprobe_path,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise FfmpegNotFoundError(f"ffprobe not found: {ffprobe_path}") from exc
        if proc.returncode != 0:
            raise FfmpegExecutionError(
                f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise FfmpegExecutionError(f"Invalid ffprobe JSON: {exc}") from exc

        streams = payload.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        file_format = payload.get("format") or {}
        duration = _first_float(
            file_format.get("duration"),
            video.get("duration") if video else None,
            audio.get("duration") if audio else None,
        )
        try:
            size_bytes = int(file_format.get("size") or media.stat().st_size)
        except (OSError, TypeError, ValueError):
            size_bytes = 0
        return MediaValidation(
            path=media.resolve(),
            size_bytes=size_bytes,
            duration_seconds=duration,
            width=_optional_int(video.get("width")) if video else None,
            height=_optional_int(video.get("height")) if video else None,
            video_codec=(video.get("codec_name") or None) if video else None,
            audio_codec=(audio.get("codec_name") or None) if audio else None,
        )

    @staticmethod
    def merge_av(ffmpeg_path: str, video: Path, audio: Path, out: Path) -> None:
        cmd = [ffmpeg_path, "-y", "-i", str(video), "-i", str(audio), "-c", "copy", str(out)]
        _FfmpegMerger._run(cmd)

    @staticmethod
    def remux_single(ffmpeg_path: str, src: Path, out: Path) -> None:
        cmd = [ffmpeg_path, "-y", "-i", str(src), "-c", "copy", str(out)]
        _FfmpegMerger._run(cmd)

    @staticmethod
    def transcode_to_mp3(ffmpeg_path: str, src: Path, out: Path) -> None:
        cmd = [ffmpeg_path, "-y", "-i", str(src), "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)]
        try:
            _FfmpegMerger._run(cmd)
        except FfmpegExecutionError as exc:
            # 某些精简版 ffmpeg 未暴露 libmp3lame 名称，回退到 mp3 编码器以保证功能可用。
            if "Unknown encoder 'libmp3lame'" not in str(exc):
                raise
            fallback = [ffmpeg_path, "-y", "-i", str(src), "-vn", "-c:a", "mp3", str(out)]
            _FfmpegMerger._run(fallback)

    @staticmethod
    def extract_audio_prefer_m4a(ffmpeg_path: str, src: Path, out_m4a: Path) -> Path:
        # 先尝试无损封装为 m4a，若 ffmpeg/流不支持再回退到 mp3。
        cmd = [ffmpeg_path, "-y", "-i", str(src), "-vn", "-c:a", "copy", "-f", "mp4", str(out_m4a)]
        try:
            _FfmpegMerger._run(cmd)
            return out_m4a
        except FfmpegExecutionError:
            try:
                out_m4a.unlink(missing_ok=True)
            except Exception:
                pass

        out_mp3 = out_m4a.with_suffix(".mp3")
        _FfmpegMerger.transcode_to_mp3(ffmpeg_path, src, out_mp3)
        return out_mp3

    @staticmethod
    def concat(ffmpeg_path: str, parts: List[Path], out: Path) -> None:
        list_file = Path(str(out) + ".txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in parts:
                f.write(f"file '{str(p.resolve()).replace('\\\\', '/')}'\n")

        cmd = [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)]
        try:
            _FfmpegMerger._run(cmd)
        finally:
            try:
                list_file.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _run(cmd: List[str]) -> None:
        try:
            # 使用字节模式捕获输出，避免 text=True 在 Windows 上按本地编码解码崩溃。
            proc = subprocess.run(cmd, capture_output=True, text=False, check=False)
        except FileNotFoundError as exc:
            raise FfmpegNotFoundError(str(exc)) from exc

        stdout_text = _FfmpegMerger._decode_bytes(proc.stdout)
        stderr_text = _FfmpegMerger._decode_bytes(proc.stderr)
        if proc.returncode != 0:
            output = stdout_text + stderr_text
            raise FfmpegExecutionError(f"ffmpeg failed ({proc.returncode})\n{output}")

    @staticmethod
    def _decode_bytes(data: Optional[bytes]) -> str:
        if not data:
            return ""
        # 优先 UTF-8，其次 GBK，再兜底替换非法字符，确保永不抛 UnicodeDecodeError。
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace")


def _first_float(*values: object) -> float:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def _optional_int(value: object) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _random_hex(n: int) -> str:
    raw = uuid.uuid4().hex + uuid.uuid4().hex
    return raw[:n]

