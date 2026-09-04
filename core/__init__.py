from .bili_downloader import BiliDownloader, VideoInfo, Clip, MediaValidation, QualityOption
from .text_safety import install_video_info_title_safety

# Video titles come from remote metadata. Keep their raw value for filenames and
# structured data, but make f-string rendering safe for Markdown-like messages.
install_video_info_title_safety(VideoInfo)

from .bili_transcriber import BiliTranscriber
from .bili_subtitle_downloader import BiliSubtitleDownloader, classify_subtitle_source
from .bili_analysis import BiliAnalysisPreparer, AnalysisPreparationResult
from .output_layout import BiliProjectLayout
from .exceptions import (
    DownloadException,
    InvalidBvidError,
    NetworkError,
    FfmpegNotFoundError,
    FfmpegExecutionError,
    CoreApiError,
    TranscribeError,
    TranscribeDependencyError,
    TranscribeModelLoadError,
    NoSubtitleError,
)

__all__ = [
    "BiliDownloader",
    "VideoInfo",
    "Clip",
    "MediaValidation",
    "QualityOption",
    "BiliTranscriber",
    "BiliSubtitleDownloader",
    "classify_subtitle_source",
    "BiliAnalysisPreparer",
    "AnalysisPreparationResult",
    "BiliProjectLayout",
    "DownloadException",
    "InvalidBvidError",
    "NetworkError",
    "FfmpegNotFoundError",
    "FfmpegExecutionError",
    "CoreApiError",
    "TranscribeError",
    "TranscribeDependencyError",
    "TranscribeModelLoadError",
    "NoSubtitleError",
]
