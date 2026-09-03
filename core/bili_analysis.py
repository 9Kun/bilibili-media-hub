"""B 站视频分析材料准备：平台字幕优先，无字幕时回退本地 ASR。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .bili_downloader import BiliDownloader, Clip, VideoInfo
from .bili_subtitle_downloader import BiliSubtitleDownloader, classify_subtitle_source
from .bili_transcriber import BiliTranscriber
from .exceptions import NoSubtitleError
from .output_layout import BiliProjectLayout, subtitle_filename, transcript_filename


@dataclass(frozen=True)
class AnalysisPreparationResult:
    manifest_path: Path
    report_path: Path
    success_count: int
    failure_count: int
    pages: List[dict]


class BiliAnalysisPreparer:
    """准备宿主 Agent 总结所需的元数据与逐 P 时间戳文本。"""

    def __init__(
        self,
        downloader: BiliDownloader,
        subtitle_downloader: BiliSubtitleDownloader,
        transcriber: Optional[BiliTranscriber],
    ):
        self.downloader = downloader
        self.subtitle_downloader = subtitle_downloader
        self.transcriber = transcriber

    def prepare(
        self,
        bvid: str,
        layout: BiliProjectLayout,
        pages: Optional[List[int]] = None,
        lang: Optional[str] = None,
        info: Optional[VideoInfo] = None,
    ) -> AnalysisPreparationResult:
        info = info or self.downloader.get_video_info(bvid)
        target_clips = _select_clips(info.clips, pages)
        page_results = [
            self._prepare_clip(info, clip, layout, lang)
            for clip in target_clips
        ]
        success_count = sum(item["status"] == "ready" for item in page_results)
        failure_count = len(page_results) - success_count

        report_path = (layout.analysis_dir / "report.md").resolve()
        manifest_path = (layout.analysis_dir / "analysis-input.json").resolve()
        manifest = {
            "schema_version": 2,
            "run_dir": str(layout.run_dir.resolve()),
            "video": {
                "bvid": info.bvid,
                "title": info.title,
                "description": info.desc,
                "owner": info.owner_name,
                "cover": info.cover,
            },
            "pages": page_results,
            "summary": {
                "requested_pages": [clip.page for clip in target_clips],
                "success_count": success_count,
                "failure_count": failure_count,
            },
            "report_path": str(report_path),
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return AnalysisPreparationResult(
            manifest_path=manifest_path,
            report_path=report_path,
            success_count=success_count,
            failure_count=failure_count,
            pages=page_results,
        )

    def _prepare_clip(
        self,
        info: VideoInfo,
        clip: Clip,
        layout: BiliProjectLayout,
        lang: Optional[str],
    ) -> dict:
        temporary = layout.subtitles_dir / f"P{clip.page:02d}.srt"
        try:
            output, used_lang = self.subtitle_downloader.download_subtitle(
                info.bvid,
                temporary,
                page=clip.page,
                lang=lang,
                info=info,
            )
            final = layout.subtitles_dir / subtitle_filename(clip.page, used_lang)
            output.replace(final)
            return _ready_page(
                clip,
                classify_subtitle_source(used_lang),
                "srt",
                final,
                used_lang,
            )
        except NoSubtitleError:
            return self._fallback_to_asr(
                info,
                clip,
                layout.transcripts_dir / transcript_filename(clip.page),
            )
        except Exception as exc:
            return _failed_page(clip, "subtitle", exc)

    def _fallback_to_asr(
        self, info: VideoInfo, clip: Clip, output_path: Path
    ) -> dict:
        if self.transcriber is None:
            return _failed_page(clip, "transcription", "未配置 FunASR 转录器")
        try:
            output = self.transcriber.transcribe(
                info.bvid,
                output_path,
                page=clip.page,
                info=info,
                clip=clip,
            )
            return _ready_page(clip, "asr", "timestamped_text", output, None)
        except Exception as exc:
            return _failed_page(clip, "transcription", exc)


def _select_clips(clips: List[Clip], pages: Optional[List[int]]) -> List[Clip]:
    if not clips:
        raise ValueError("视频没有可分析的分P")
    if pages is None:
        return list(clips)

    requested = sorted({int(page) for page in pages if int(page) > 0})
    if not requested:
        raise ValueError("页码列表为空")
    by_page = {clip.page: clip for clip in clips}
    missing = [page for page in requested if page not in by_page]
    if missing:
        raise ValueError(f"页码不存在：{missing}")
    return [by_page[page] for page in requested]


def _ready_page(
    clip: Clip,
    source_type: str,
    source_format: str,
    source_path: Path,
    language: Optional[str],
) -> dict:
    return {
        "page": clip.page,
        "part_title": clip.part,
        "status": "ready",
        "source_type": source_type,
        "source_format": source_format,
        "source_path": str(Path(source_path).resolve()),
        "language": language,
        "error_stage": None,
        "error": None,
    }


def _failed_page(clip: Clip, stage: str, error: object) -> dict:
    return {
        "page": clip.page,
        "part_title": clip.part,
        "status": "failed",
        "source_type": None,
        "source_format": None,
        "source_path": None,
        "language": None,
        "error_stage": stage,
        "error": str(error) or error.__class__.__name__,
    }
