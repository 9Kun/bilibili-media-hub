import threading
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Set

from core import (
    BiliAnalysisPreparer,
    BiliDownloader,
    BiliProjectLayout,
    BiliSubtitleDownloader,
    BiliTranscriber,
    VideoInfo,
    QualityOption,
    classify_subtitle_source,
    DownloadException,
    NoSubtitleError,
)
from core.output_layout import subtitle_filename, transcript_filename


class State(Enum):
    IDLE = "IDLE"
    WAIT_CONFIRM = "WAIT_CONFIRM"
    DOWNLOADING = "DOWNLOADING"
    TRANSCRIBING = "TRANSCRIBING"
    ANALYZING = "ANALYZING"


class BilibiliDownloadSkill:
    YES_WORDS = {"是", "好", "确认", "下载", "yes", "y", "ok"}
    NO_WORDS = {"否", "不", "取消", "no", "n"}
    ALL_P_WORDS = {"全部", "全部分p", "所有", "all", "all-pages", "all pages"}

    def __init__(
        self,
        downloader: BiliDownloader,
        save_dir: Path,
        reply: Callable[[str], None],
        transcriber: Optional[BiliTranscriber] = None,
        subtitle_downloader: Optional[BiliSubtitleDownloader] = None,
        analysis_preparer: Optional[BiliAnalysisPreparer] = None,
    ):
        self.downloader = downloader
        self.save_dir = Path(save_dir).expanduser().resolve()
        self.reply = reply
        self.transcriber = transcriber  # 可选；为 None 时转录入口返回提示
        self.subtitle_downloader = subtitle_downloader  # 可选；为 None 时字幕入口返回提示
        self.analysis_preparer = analysis_preparer  # 可选；只准备材料，宿主 Agent 负责总结

        self.state = State.IDLE
        self.pending_bvid: Optional[str] = None
        self.pending_title: Optional[str] = None
        self.pending_media_type: str = "video"
        self.pending_scope: str = "single"
        self.pending_pages: Optional[List[int]] = None
        self.pending_available_pages: Optional[Set[int]] = None
        self.pending_total_pages: int = 0
        self.pending_quality: Optional[int] = None
        self.pending_info: Optional[VideoInfo] = None
        # 动作上下文：取值 "download" / "transcribe" / "subtitle" / "analyze"
        self.pending_action: str = "download"
        # 字幕语言偏好（subtitle / analyze 动作使用）
        self.pending_subtitle_lang: Optional[str] = None
        self.pending_layout: Optional[BiliProjectLayout] = None
        self._lock = threading.RLock()

    def download_bilibili_media(
        self,
        bvid: str,
        media_type: str = "video",
        quality: Optional[int] = None,
    ) -> str:
        """开始下载单P媒体；多P未给范围时进入确认态。"""
        with self._lock:
            busy = self._busy_prompt()
            if busy:
                return busy
            if self.state == State.WAIT_CONFIRM:
                return self._wait_confirm_prompt()

            try:
                info = self.downloader.get_video_info(bvid)
                quality_options = self._quality_options(info, media_type)
                quality_error = self._quality_error(
                    media_type, quality, quality_options
                )
                if quality_error:
                    return quality_error
                quality_hint = self._quality_hint(media_type, quality_options)
                target = "音频" if media_type == "audio" else "视频"
                total = len(info.clips)

                self._set_download_pending(info, media_type, quality)
                if total <= 1:
                    self.pending_scope = "single"
                    self.pending_pages = None
                    self._start_download_async()
                    return (
                        f"已找到{target}：《{self.pending_title}》\n"
                        f"{quality_hint}\n"
                        f"目标档位：{quality if quality is not None else '自动选择最高可用'}\n"
                        "该视频为单P，已直接开始下载，完成后将通知结果。"
                    )

                self.pending_scope = "need_choice"
                self.pending_pages = None
                self.state = State.WAIT_CONFIRM
                return (
                    f"已找到{target}：《{self.pending_title}》（共{total}P）\n"
                    f"{quality_hint}\n"
                    f"目标档位：{quality if quality is not None else '自动选择最高可用'}\n"
                    "请确认下载范围：回复“全部”下载全部分P，或回复“1,3”下载指定分P；回复“否”取消。"
                )
            except Exception as exc:
                return self._to_friendly_error(exc)

    def download_bilibili_multi_p(
        self,
        bvid: str,
        media_type: str = "video",
        pages: Optional[List[int]] = None,
        quality: Optional[int] = None,
        all_pages: bool = False,
    ) -> str:
        """下载多P；显式 pages/all_pages 已代表用户确认范围。"""
        with self._lock:
            busy = self._busy_prompt()
            if busy:
                return busy
            if self.state == State.WAIT_CONFIRM:
                return self._wait_confirm_prompt()

            try:
                info = self.downloader.get_video_info(bvid)
                quality_options = self._quality_options(info, media_type)
                quality_error = self._quality_error(
                    media_type, quality, quality_options
                )
                if quality_error:
                    return quality_error
                quality_hint = self._quality_hint(media_type, quality_options)
                total = len(info.clips)
                available_pages = {clip.page for clip in info.clips}
                self._set_download_pending(info, media_type, quality)

                if pages is None and not all_pages:
                    self.pending_scope = "need_choice"
                    self.pending_pages = None
                    self.state = State.WAIT_CONFIRM
                    page_hint = "待确认"
                    action_hint = "请确认下载范围：回复“全部”下载全部分P，或回复“1,3”下载指定分P；回复“否”取消。"
                elif all_pages:
                    self.pending_scope = "all"
                    self.pending_pages = None
                    page_hint = f"全部分P（共{total}P）"
                    self._start_download_async()
                    action_hint = "已按显式 --all-pages 开始下载，完成后将通知结果。"
                else:
                    normalized_pages = sorted({int(page) for page in pages if int(page) > 0})
                    if not normalized_pages:
                        self._reset_pending()
                        return "页码列表为空，请至少提供一个大于 0 的页码。"
                    missing = [page for page in normalized_pages if page not in available_pages]
                    if missing:
                        self._reset_pending()
                        return f"页码不存在：{missing}，可用范围为 1 到 {total}。"
                    self.pending_scope = "selected"
                    self.pending_pages = normalized_pages
                    page_hint = f"指定分P：{normalized_pages}"
                    self._start_download_async()
                    action_hint = "已按显式分P范围开始下载，完成后将通知结果。"

                target = "音频" if media_type == "audio" else "视频"
                return (
                    f"已找到{target}：《{self.pending_title or info.title}》\n"
                    f"下载范围：{page_hint}\n"
                    f"{quality_hint}\n"
                    f"目标档位：{quality if quality is not None else '自动选择最高可用'}\n"
                    f"{action_hint}"
                )
            except Exception as exc:
                self._reset_pending()
                return self._to_friendly_error(exc)

    def transcribe_bilibili_media(
        self,
        bvid: str,
        pages: Optional[List[int]] = None,
        all_pages: bool = False,
    ) -> str:
        """开始转录 B 站视频为带时间戳的文字稿。

        :param bvid: B 站视频 BV 号
        :param pages: 指定分P；None 时若为单P直接转录，多P进入范围确认
        """
        with self._lock:
            if self.transcriber is None:
                return "未配置转录器，请在构造 Skill 时传入 BiliTranscriber 实例。"
            busy = self._busy_prompt()
            if busy:
                return busy
            if self.state == State.WAIT_CONFIRM:
                return self._wait_confirm_prompt()

            try:
                info = self.downloader.get_video_info(bvid)
                total = len(info.clips)
                available_pages = {clip.page for clip in info.clips}

                self.pending_bvid = info.bvid
                self.pending_title = info.title
                self.pending_media_type = "audio"  # 转录内部需要音频
                self.pending_action = "transcribe"
                self.pending_info = info
                self.pending_available_pages = available_pages
                self.pending_total_pages = total

                if total <= 1 or pages is not None:
                    if pages is None:
                        target_pages = None
                        scope = "single"
                    else:
                        normalized_pages = sorted({int(page) for page in pages if int(page) > 0})
                        if not normalized_pages:
                            return "页码列表为空，请至少提供一个大于 0 的页码。"
                        missing = [p for p in normalized_pages if p not in available_pages]
                        if missing:
                            return f"页码不存在：{missing}，可用范围为 1 到 {total}。"
                        target_pages = normalized_pages
                        scope = "selected"

                    self.pending_scope = scope
                    self.pending_pages = target_pages
                    self._start_download_async()
                    range_hint = "单P" if scope == "single" else f"指定分P：{target_pages}"
                    return (
                        f"已找到视频：《{self.pending_title}》\n"
                        f"转录范围：{range_hint}\n"
                        "已开始转录，完成后将通知结果（首次加载模型约 30 秒）。"
                    )

                self.pending_scope = "need_choice"
                self.pending_pages = None
                self.state = State.WAIT_CONFIRM
                return (
                    f"已找到视频：《{self.pending_title}》（共{total}P）\n"
                    "请确认转录范围：回复“全部”转录全部分P，或回复“1,3”转录指定分P；回复“否”取消。"
                )
            except Exception as e:
                return self._to_friendly_error(e)

    def download_bilibili_subtitle(
        self,
        bvid: str,
        pages: Optional[List[int]] = None,
        lang: Optional[str] = None,
        all_pages: bool = False,
    ) -> str:
        """开始下载 B 站视频的平台字幕为 .srt 文件。

        :param bvid: B 站视频 BV 号
        :param pages: 指定分P；None 时若为单P直接下载字幕，多P进入范围确认
        :param lang: 字幕语言代码（如 "zh-CN"、"ai-zh"）；None 表示取第一个可用字幕
        """
        with self._lock:
            if self.subtitle_downloader is None:
                return "未配置字幕下载器，请在构造 Skill 时传入 BiliSubtitleDownloader 实例。"
            busy = self._busy_prompt()
            if busy:
                return busy
            if self.state == State.WAIT_CONFIRM:
                return self._wait_confirm_prompt()

            try:
                info = self.downloader.get_video_info(bvid)
                total = len(info.clips)
                available_pages = {clip.page for clip in info.clips}

                self.pending_bvid = info.bvid
                self.pending_title = info.title
                self.pending_media_type = "video"  # 字幕下载不使用 media_type
                self.pending_action = "subtitle"
                self.pending_subtitle_lang = lang
                self.pending_info = info
                self.pending_available_pages = available_pages
                self.pending_total_pages = total

                if total <= 1 or pages is not None or all_pages:
                    if total <= 1:
                        target_pages = None
                        scope = "single"
                    elif all_pages:
                        target_pages = None
                        scope = "all"
                    else:
                        normalized_pages = sorted({int(page) for page in pages if int(page) > 0})
                        if not normalized_pages:
                            return "页码列表为空，请至少提供一个大于 0 的页码。"
                        missing = [p for p in normalized_pages if p not in available_pages]
                        if missing:
                            return f"页码不存在：{missing}，可用范围为 1 到 {total}。"
                        target_pages = normalized_pages
                        scope = "selected"

                    self.pending_scope = scope
                    self.pending_pages = target_pages
                    self._start_download_async()
                    if scope == "single":
                        range_hint = "单P"
                    elif scope == "all":
                        range_hint = f"全部分P（共{total}P）"
                    else:
                        range_hint = f"指定分P：{target_pages}"
                    lang_hint = f"，语言：{lang}" if lang else "，语言：自动选择"
                    return (
                        f"已找到视频：《{self.pending_title}》\n"
                        f"字幕下载范围：{range_hint}{lang_hint}\n"
                        "已开始下载字幕，完成后将通知结果。"
                    )

                self.pending_scope = "need_choice"
                self.pending_pages = None
                self.state = State.WAIT_CONFIRM
                return (
                    f"已找到视频：《{self.pending_title}》（共{total}P）\n"
                    "请确认字幕下载范围：回复“全部”下载全部分P字幕，或回复“1,3”下载指定分P字幕；回复“否”取消。"
                )
            except Exception as e:
                return self._to_friendly_error(e)

    def analyze_bilibili_media(
        self,
        bvid: str,
        pages: Optional[List[int]] = None,
        lang: Optional[str] = None,
        all_pages: bool = False,
    ) -> str:
        """准备字幕/转录材料，供宿主 Agent 生成中文分析报告。"""
        with self._lock:
            if self.analysis_preparer is None:
                return "未配置分析准备器，请在构造 Skill 时传入 BiliAnalysisPreparer 实例。"
            busy = self._busy_prompt()
            if busy:
                return busy
            if self.state == State.WAIT_CONFIRM:
                return self._wait_confirm_prompt()

            try:
                info = self.downloader.get_video_info(bvid)
                total = len(info.clips)
                available_pages = {clip.page for clip in info.clips}

                self.pending_bvid = info.bvid
                self.pending_title = info.title
                self.pending_media_type = "audio"
                self.pending_action = "analyze"
                self.pending_subtitle_lang = lang
                self.pending_info = info
                self.pending_available_pages = available_pages
                self.pending_total_pages = total

                if total <= 1 or pages is not None or all_pages:
                    if total <= 1:
                        target_pages = None
                        scope = "single"
                    elif all_pages:
                        target_pages = None
                        scope = "all"
                    else:
                        normalized_pages = sorted({int(page) for page in pages if int(page) > 0})
                        if not normalized_pages:
                            return "页码列表为空，请至少提供一个大于 0 的页码。"
                        missing = [page for page in normalized_pages if page not in available_pages]
                        if missing:
                            return f"页码不存在：{missing}，可用范围为 1 到 {total}。"
                        target_pages = normalized_pages
                        scope = "selected"

                    self.pending_scope = scope
                    self.pending_pages = target_pages
                    self._start_download_async()
                    if scope == "single":
                        range_hint = "单P"
                    elif scope == "all":
                        range_hint = f"全部分P（共{total}P）"
                    else:
                        range_hint = f"指定分P：{target_pages}"
                    return (
                        f"已找到视频：《{self.pending_title}》\n"
                        f"分析范围：{range_hint}\n"
                        "已开始准备分析材料：优先使用B站平台已有的人工/UP主CC或AI智能字幕，"
                        "平台无可用字幕时回退本地FunASR转录。"
                    )

                self.pending_scope = "need_choice"
                self.pending_pages = None
                self.state = State.WAIT_CONFIRM
                return (
                    f"已找到视频：《{self.pending_title}》（共{total}P）\n"
                    "请确认分析范围：回复“全部”分析全部分P，或回复“1,3”分析指定分P；回复“否”取消。"
                )
            except Exception as exc:
                return self._to_friendly_error(exc)

    def confirm_download(self, message: str) -> str:
        with self._lock:
            busy = self._busy_prompt()
            if busy:
                return busy
            if self.state != State.WAIT_CONFIRM:
                return "当前没有待确认的任务。"

            is_transcribe = self.pending_action == "transcribe"
            is_subtitle = self.pending_action == "subtitle"
            is_analyze = self.pending_action == "analyze"
            if is_analyze:
                verb = "分析"
            elif is_transcribe:
                verb = "转录"
            elif is_subtitle:
                verb = "字幕下载"
            else:
                verb = "下载"
            text = (message or "").strip().lower()
            if self.pending_scope == "need_choice":
                if text in self.NO_WORDS:
                    self._cleanup_pending_parts()
                    self._reset_pending()
                    return f"已取消{verb}。你可以重新发起新的请求。"

                if text in self.ALL_P_WORDS:
                    self.pending_scope = "all"
                    self.pending_pages = None
                    self._start_download_async()
                    return f"已确认{verb}全部分P，开始{verb}，完成后将通知结果。"

                parsed_pages = self._try_parse_page_selection(text)
                if parsed_pages is None:
                    return f"请回复“全部”{verb}全部分P，或回复“1,3”{verb}指定分P；回复“否”取消。"

                available = self.pending_available_pages or set()
                missing = [page for page in parsed_pages if page not in available]
                if missing:
                    total = self.pending_total_pages or len(available)
                    return f"页码不存在：{missing}，可用范围为 1 到 {total}。"

                self.pending_scope = "selected"
                self.pending_pages = parsed_pages
                self._start_download_async()
                return f"已确认{verb}指定分P：{parsed_pages}，开始{verb}，完成后将通知结果。"

            if text in self.YES_WORDS:
                self._start_download_async()
                return f"开始{verb}，完成后将通知结果。"
            if text in self.NO_WORDS:
                self._cleanup_pending_parts()
                self._reset_pending()
                return f"已取消{verb}。你可以重新发起新的请求。"
            return f"请回复“是”继续{verb}，或回复“否”取消。"

    def _start_download_async(self) -> None:
        layout = self._ensure_pending_layout()
        action = self.pending_action
        if action == "analyze":
            self.state = State.ANALYZING
            self._start_analysis_async()
            return
        if action == "transcribe":
            self.state = State.TRANSCRIBING
            self._start_transcribe_async()
            return
        if action == "subtitle":
            # 字幕下载是文件下载的特例，复用 DOWNLOADING 状态
            self.state = State.DOWNLOADING
            self._start_subtitle_async()
            return

        self.state = State.DOWNLOADING
        bvid = self.pending_bvid
        media_type = self.pending_media_type
        scope = self.pending_scope
        pages = self.pending_pages
        quality = self.pending_quality
        info = self.pending_info

        def _worker() -> None:
            try:
                # 静默下载：不输出开始提示、不输出进度百分比
                if scope == "single":
                    output = self.downloader.download(
                        bvid,
                        str(layout.media_dir),
                        None,
                        media_type,
                        quality=quality,
                        info=info,
                    )
                    validation = self.downloader.get_validation(output)
                    validation_hint = (
                        f"\nffprobe验收：{validation.summary()}" if validation else ""
                    )
                    self.reply(f"下载完成，文件已保存到：{output}{validation_hint}")
                else:
                    outputs = self.downloader.download_all_pages(
                        bvid,
                        str(layout.media_dir),
                        pages,
                        None,
                        media_type,
                        quality=quality,
                        info=info,
                    )
                    lines = []
                    for output in outputs:
                        validation = self.downloader.get_validation(output)
                        suffix = f"（{validation.summary()}）" if validation else ""
                        lines.append(f"{output}{suffix}")
                    self.reply(
                        f"多P下载完成并通过数量验收，共{len(outputs)}个文件：\n"
                        + "\n".join(lines)
                    )
            except Exception as e:
                self.downloader.cleanup_temporary_files(layout.media_dir)
                self.reply(self._to_friendly_error(e))
            finally:
                with self._lock:
                    self._reset_pending()

        t = threading.Thread(target=_worker, name="Skill-Bili-Download", daemon=True)
        t.start()

    def _start_subtitle_async(self) -> None:
        bvid = self.pending_bvid
        scope = self.pending_scope
        pages = self.pending_pages
        lang = self.pending_subtitle_lang
        info = self.pending_info
        sub_dl = self.subtitle_downloader
        save_dir = self.pending_layout.subtitles_dir

        def _worker() -> None:
            try:
                if scope == "single":
                    temporary = save_dir / "P01.srt"
                    out, used_lang = sub_dl.download_subtitle(
                        bvid, temporary, page=1, lang=lang, info=info
                    )
                    final = save_dir / subtitle_filename(1, used_lang)
                    out.replace(final)
                    source_label = (
                        "AI智能字幕"
                        if classify_subtitle_source(used_lang) == "ai_subtitle"
                        else "人工/UP主CC字幕"
                    )
                    self.reply(
                        f"字幕下载完成，文件已保存到：{final.resolve()}"
                        f"（来源：{source_label}，语言：{used_lang}）"
                    )
                else:
                    outs = sub_dl.download_subtitle_multi_p(
                        bvid, save_dir, pages=pages, lang=lang, info=info
                    )
                    lines = []
                    for path, used_lang in outs:
                        source_label = (
                            "AI智能字幕"
                            if classify_subtitle_source(used_lang) == "ai_subtitle"
                            else "人工/UP主CC字幕"
                        )
                        lines.append(
                            f"{path}（来源：{source_label}，语言：{used_lang}）"
                        )
                    self.reply("多P字幕下载完成，文件已保存到：\n" + "\n".join(lines))
            except Exception as e:
                self.reply(self._to_friendly_error(e))
            finally:
                with self._lock:
                    self._reset_pending()

        t = threading.Thread(target=_worker, name="Skill-Bili-Subtitle", daemon=True)
        t.start()

    def _start_transcribe_async(self) -> None:
        bvid = self.pending_bvid
        scope = self.pending_scope
        pages = self.pending_pages
        transcriber = self.transcriber
        save_dir = self.pending_layout.transcripts_dir
        info = self.pending_info

        def _worker() -> None:
            try:
                if scope == "single":
                    out = transcriber.transcribe(
                        bvid,
                        save_dir / transcript_filename(1),
                        page=1,
                        info=info,
                        clip=info.clips[0] if info and info.clips else None,
                    )
                    self.reply(f"转录完成，文件已保存到：{out}")
                else:
                    outs = transcriber.transcribe_multi_p(
                        bvid, save_dir, pages=pages, info=info
                    )
                    self.reply("多P转录完成，文件已保存到：\n" + "\n".join(str(p) for p in outs))
            except Exception as e:
                self.reply(self._to_friendly_error(e))
            finally:
                with self._lock:
                    self._reset_pending()

        t = threading.Thread(target=_worker, name="Skill-Bili-Transcribe", daemon=True)
        t.start()

    def _start_analysis_async(self) -> None:
        bvid = self.pending_bvid
        scope = self.pending_scope
        pages = self.pending_pages
        lang = self.pending_subtitle_lang
        preparer = self.analysis_preparer
        layout = self.pending_layout
        info = self.pending_info

        def _worker() -> None:
            try:
                target_pages = None if scope in {"single", "all"} else pages
                result = preparer.prepare(
                    bvid,
                    layout,
                    pages=target_pages,
                    lang=lang,
                    info=info,
                )
                failed = [item for item in result.pages if item["status"] == "failed"]
                if result.success_count == 0:
                    details = "\n".join(
                        f"- P{item['page']} {item['part_title']}：{item['error']}"
                        for item in failed
                    )
                    self.reply(
                        "分析材料准备失败，没有可供宿主 Agent 总结的字幕或文字稿。\n"
                        f"{details}\n分析清单：{result.manifest_path}"
                    )
                    return

                failure_hint = ""
                if failed:
                    failure_hint = "\n失败分P：\n" + "\n".join(
                        f"- P{item['page']} {item['part_title']}：{item['error']}"
                        for item in failed
                    )
                self.reply(
                    "分析材料准备完成。请宿主 Agent 读取清单及其中的时间戳文本，"
                    "生成中文分析报告并在对话中展示。\n"
                    f"分析清单：{result.manifest_path}\n"
                    f"报告保存路径：{result.report_path}\n"
                    f"成功：{result.success_count}P，失败：{result.failure_count}P"
                    f"{failure_hint}"
                )
            except Exception as exc:
                self.reply(self._to_friendly_error(exc))
            finally:
                with self._lock:
                    self._reset_pending()

        thread = threading.Thread(target=_worker, name="Skill-Bili-Analyze", daemon=True)
        thread.start()

    def _set_download_pending(
        self, info: VideoInfo, media_type: str, quality: Optional[int]
    ) -> None:
        self.pending_bvid = info.bvid
        self.pending_title = info.title
        self.pending_media_type = media_type
        self.pending_action = "download"
        self.pending_quality = quality
        self.pending_info = info
        self.pending_available_pages = {clip.page for clip in info.clips}
        self.pending_total_pages = len(info.clips)

    def _quality_options(
        self, info: VideoInfo, media_type: str
    ) -> List[QualityOption]:
        if media_type == "audio":
            return []
        return self.downloader.get_available_quality_options(info.bvid, info=info)

    @staticmethod
    def _quality_error(
        media_type: str,
        quality: Optional[int],
        options: List[QualityOption],
    ) -> Optional[str]:
        if quality is None:
            return None
        if media_type == "audio":
            return "--quality 仅适用于视频下载；音频任务会自动选择最高可用音质。"
        available = {option.qn for option in options}
        if available and int(quality) not in available:
            display = BiliDownloader.format_quality_options(options)
            return f"请求的清晰度 {quality} 当前不可用。可用档位：{display}"
        return None

    @staticmethod
    def _quality_hint(media_type: str, options: List[QualityOption]) -> str:
        if media_type == "audio":
            return "音频质量：自动选择最高可用音质"
        return "可用画质：" + BiliDownloader.format_quality_options(options)

    def _busy_prompt(self) -> Optional[str]:
        """当前是否有任务在跑；有则返回提示，否则返回 None。"""
        if self.state == State.DOWNLOADING:
            return "当前已有下载任务正在进行中，请稍候…"
        if self.state == State.TRANSCRIBING:
            return "当前已有转录任务正在进行中，请稍候…"
        if self.state == State.ANALYZING:
            return "当前已有视频分析任务正在进行中，请稍候…"
        return None

    def _wait_confirm_prompt(self) -> str:
        """处于 WAIT_CONFIRM 时新请求的引导提示，按动作类型区分文案。"""
        if self.pending_action == "analyze":
            verb = "分析"
        elif self.pending_action == "transcribe":
            verb = "转录"
        elif self.pending_action == "subtitle":
            verb = "下载字幕"
        else:
            verb = "下载"
        return (
            f"请先完成当前待确认任务：回复“全部”{verb}全部分P，"
            f"或回复“1,3”{verb}指定分P；回复“否”取消。"
        )

    @staticmethod
    def _try_parse_page_selection(text: str) -> Optional[List[int]]:
        normalized = (
            (text or "")
            .replace("，", ",")
            .replace("、", ",")
            .replace(" ", ",")
            .replace(";", ",")
            .replace("；", ",")
            .strip(",")
        )
        if not normalized:
            return None

        pages: List[int] = []
        for token in normalized.split(","):
            part = token.strip()
            if not part:
                continue
            if not part.isdigit():
                return None
            num = int(part)
            if num <= 0:
                return None
            pages.append(num)

        if not pages:
            return None
        return sorted(set(pages))

    def _reset_pending(self) -> None:
        self.pending_bvid = None
        self.pending_title = None
        self.pending_media_type = "video"
        self.pending_action = "download"
        self.pending_scope = "single"
        self.pending_pages = None
        self.pending_available_pages = None
        self.pending_total_pages = 0
        self.pending_quality = None
        self.pending_info = None
        self.pending_subtitle_lang = None
        self.pending_layout = None
        self.state = State.IDLE

    def _cleanup_pending_parts(self) -> None:
        if self.pending_layout is not None and self.pending_action == "download":
            self.downloader.cleanup_temporary_files(self.pending_layout.media_dir)

    def _ensure_pending_layout(self) -> BiliProjectLayout:
        if self.pending_layout is None:
            self.pending_layout = BiliProjectLayout.create(
                self.save_dir,
                self.pending_title or self.pending_bvid or "bilibili-video",
                self.pending_bvid or "unknown-bvid",
            )
        return self.pending_layout

    @staticmethod
    def _to_friendly_error(e: Exception) -> str:
        msg = str(e) if str(e) else e.__class__.__name__
        low = msg.lower()

        if isinstance(e, DownloadException):
            # 保持 Java 行为：仍按关键字映射，不新增额外分支文案
            pass

        if "invalid bvid" in low or "未识别" in low or "empty pagelist" in low:
            return "BV号无效或不存在，请检查后重试。"

        if (
            "cannot run program" in low
            or "createprocess error=2" in low
            or "ffmpeg failed" in low
            or "ffmpeg.exe" in low
            or "ffprobe" in low
            or "no such file or directory" in low and "ffmpeg" in low
        ):
            return "FFmpeg/ffprobe 未找到、执行失败或媒体验收未通过，请确认二者位于同一目录，或在BiliDownloader构造时显式指定路径。"

        if "timed out" in low or "unknownhost" in low or "http" in low:
            return f"网络请求失败，请检查网络连接后重试。详细错误：{msg}"

        # 转录相关错误
        if "no module named 'funasr'" in low or "no module named \"funasr\"" in low:
            return "未安装转录依赖 funasr，请执行：pip install funasr"
        if "ssl" in low and ("certificate" in low or "verify" in low):
            return "FunASR 模型下载证书校验失败，请检查网络或手动下载模型到 ~/.cache/funasr"
        if "cuda" in low and "out of memory" in low:
            return "显存不足，请在 BiliTranscriber 构造时指定 device='cpu'"
        if "paraformer" in low or "funasr" in low or "模型加载" in low or "model load" in low:
            return f"转录失败：{msg}"

        # 字幕相关错误
        if (
            isinstance(e, NoSubtitleError)
            or "no cc subtitle" in low
            or "no platform subtitle" in low
            or "subtitle" in low and "empty" in low
        ):
            return "未找到该视频可用的人工/UP主CC字幕或AI智能字幕；可提供登录态后重试，或改用本地FunASR转录。"

        return f"发生错误：{msg}"


