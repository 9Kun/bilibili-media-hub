"""B 站平台字幕下载器（人工/UP主 CC 与 AI 智能字幕）。

设计要点：
- 复用 BiliDownloader 的 http 客户端、WBI 签名、指纹 cookie，不重复实现网络层。
- 两步流程：
  1. 调用 /x/player/wbi/v2 查询字幕 URL（与 Java AbstractBaseParser.getVideoSubtitleLink 对齐）
  2. 拉取字幕 JSON 并转为标准 SRT 格式（与 Java CCDownloader.save2srt 对齐）
- SRT 时间格式采用标准 `HH:MM:SS,mmm`（逗号分隔毫秒），优于 Java 版的 `%.2f`。
"""

import json
from pathlib import Path
from typing import List, Optional, Tuple

from .bili_downloader import BiliDownloader, VideoInfo
from .exceptions import CoreApiError, NoSubtitleError
from .output_layout import subtitle_filename


# 字幕查询接口（与 Java 版一致）
SUBTITLE_QUERY_URL = "https://api.bilibili.com/x/player/wbi/v2"


class BiliSubtitleDownloader:
    """B 站平台字幕下载器：查询字幕 URL -> 拉取 JSON -> 转 SRT。"""

    def __init__(self, downloader: BiliDownloader):
        self.downloader = downloader

    def download_subtitle(
        self,
        bvid: str,
        output_path: Path,
        page: int = 1,
        lang: Optional[str] = None,
        info: Optional[VideoInfo] = None,
    ) -> Tuple[Path, str]:
        """下载指定分P的人工/UP主 CC 或 AI 智能字幕为 .srt 文件。

        :param bvid: B 站视频 BV 号
        :param output_path: .srt 输出路径
        :param page: 分P 页码，从 1 开始
        :param lang: 字幕语言代码（如 "zh-CN"、"ai-zh"）；None 表示取第一个
        :return: (输出文件路径, 实际使用的语言代码)
        """
        info = info or self.downloader.get_video_info(bvid)
        target_page = int(page)
        clip = next((c for c in info.clips if c.page == target_page), None)
        if clip is None:
            raise NoSubtitleError(f"Page {target_page} not found for bvid: {info.bvid}")

        sub_url, used_lang = self._query_subtitle_url(info.bvid, clip.cid, lang)
        subtitle_text = self._fetch_subtitle_text(sub_url)
        srt_content = _json_to_srt(subtitle_text)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(srt_content, encoding="utf-8")
        return output_path.resolve(), used_lang

    def download_subtitle_multi_p(
        self,
        bvid: str,
        output_dir: Path,
        pages: Optional[List[int]] = None,
        lang: Optional[str] = None,
        info: Optional[VideoInfo] = None,
    ) -> List[Tuple[Path, str]]:
        """多P批量下载字幕。

        :param pages: 指定页码列表；None 表示全部
        :return: [(输出路径, 语言代码), ...]
        """
        info = info or self.downloader.get_video_info(bvid)
        if not info.clips:
            raise NoSubtitleError(f"No clips for bvid: {bvid}")

        clip_pages = {c.page for c in info.clips}
        if pages is None:
            target_pages = sorted(clip_pages)
        else:
            target_pages = sorted({int(p) for p in pages if int(p) > 0})
            if not target_pages:
                raise NoSubtitleError("pages is empty")
            missing = [p for p in target_pages if p not in clip_pages]
            if missing:
                raise NoSubtitleError(f"Pages not found: {missing}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: List[Tuple[Path, str]] = []
        for page in target_pages:
            temporary = output_dir / f"P{page:02d}.srt"
            output, used_lang = self.download_subtitle(
                bvid, temporary, page=page, lang=lang, info=info
            )
            final = output_dir / subtitle_filename(page, used_lang)
            output.replace(final)
            outputs.append((final.resolve(), used_lang))
        return outputs

    # ---------- 内部实现 ----------

    def _query_subtitle_url(self, bvid: str, cid: int, lang: Optional[str]) -> Tuple[str, str]:
        """调用 /x/player/wbi/v2 查询字幕 URL，按 lang 选择。

        与 Java AbstractBaseParser.getVideoSubtitleLink 对齐：
        - 接口：/x/player/wbi/v2?bvid=&cid=&isGaiaAvoided=false
        - 响应路径：data.subtitle.subtitles[].lan / subtitle_url
        - 简化：不传 genDmImgParams 反爬指纹参数（指纹 cookie 已覆盖）
        """
        url = (
            f"{SUBTITLE_QUERY_URL}?bvid={bvid}&cid={cid}&isGaiaAvoided=false"
        )
        url = self.downloader.signer.enc_wbi(url)
        headers = self.downloader._bili_api_headers(bvid)
        obj = self.downloader.http.get_json(url, headers, with_fingerprint_cookie=True)

        try:
            subtitles = obj["data"]["subtitle"]["subtitles"]
        except (KeyError, TypeError):
            raise NoSubtitleError(f"Subtitle field missing in response for {bvid}/{cid}")

        if not subtitles:
            raise NoSubtitleError(
                f"No platform subtitle for bvid={bvid} cid={cid} "
                "(possibly no CC/AI subtitle or login is required)"
            )

        # 语言选择：精确匹配 -> 回退第一个
        target = None
        if lang:
            target = next((s for s in subtitles if s.get("lan") == lang), None)
        if target is None:
            target = subtitles[0]

        sub_url = target.get("subtitle_url", "") or ""
        if not sub_url:
            raise NoSubtitleError(f"Subtitle URL empty for lang={target.get('lan')}")

        # Java 版补 https: 前缀
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        elif not sub_url.startswith("http"):
            sub_url = "https://" + sub_url

        return sub_url, target.get("lan", "") or ""

    def _fetch_subtitle_text(self, sub_url: str) -> str:
        """拉取字幕 JSON 文本。"""
        # 字幕域名（aisubtitle.hdslb.com 等）不需要 Referer，但带上不影响
        headers = {
            "User-Agent": self.downloader.user_agent,
            "Referer": "https://www.bilibili.com/",
        }
        # 复用 _HttpClient.get（自动带指纹 cookie，但字幕 URL 通常不需要）
        try:
            text = self.downloader.http.get(sub_url, headers, with_fingerprint_cookie=False)
        except Exception as exc:
            raise CoreApiError(f"Fetch subtitle failed: {exc}") from exc
        if not text:
            raise NoSubtitleError("Subtitle response empty")
        return text


# ---------- 模块级辅助函数 ----------


def classify_subtitle_source(language: Optional[str]) -> str:
    """按 B 站语言代码区分 AI 智能字幕与人工/UP主 CC 字幕。"""
    normalized = (language or "").strip().lower()
    return "ai_subtitle" if normalized.startswith("ai-") else "cc"


def _json_to_srt(subtitle_text: str) -> str:
    """将 B 站字幕 JSON 转为标准 SRT 格式。

    B 站 JSON 结构：
        {"body": [{"from": 0.5, "to": 2.3, "content": "大家好"}, ...]}

    SRT 输出：
        1
        00:00:00,500 --> 00:00:02,300
        大家好

        2
        ...
    """
    try:
        obj = json.loads(subtitle_text)
    except json.JSONDecodeError as exc:
        raise CoreApiError(f"Invalid subtitle JSON: {exc}") from exc

    body = obj.get("body") if isinstance(obj, dict) else None
    if not isinstance(body, list) or not body:
        raise NoSubtitleError("Subtitle body empty or invalid")

    lines: List[str] = []
    seq = 0
    for item in body:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "") or ""
        if not content:
            continue  # 跳过空内容，序号不递增（保持 SRT 序号连续）
        seq += 1
        from_sec = float(item.get("from", 0) or 0)
        to_sec = float(item.get("to", 0) or 0)
        lines.append(str(seq))
        lines.append(f"{_seconds_to_srt_time(from_sec)} --> {_seconds_to_srt_time(to_sec)}")
        lines.append(content)
        lines.append("")  # SRT 条目间空行

    if not lines:
        raise NoSubtitleError("Subtitle body has no valid entries")

    return "\n".join(lines) + "\n"


def _seconds_to_srt_time(seconds: float) -> str:
    """秒数转 SRT 时间格式 HH:MM:SS,mmm。"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"
