class DownloadException(Exception):
    """Base exception for downloader core."""


class InvalidBvidError(DownloadException):
    pass


class NetworkError(DownloadException):
    pass


class FfmpegNotFoundError(DownloadException):
    pass


class FfmpegExecutionError(DownloadException):
    pass


class CoreApiError(DownloadException):
    pass


class TranscribeError(DownloadException):
    """转录流程基础异常。"""


class TranscribeDependencyError(TranscribeError):
    """缺少 funasr 等转录依赖。"""


class TranscribeModelLoadError(TranscribeError):
    """FunASR 模型加载失败。"""


class NoSubtitleError(DownloadException):
    """视频无可用平台字幕（人工/UP主CC或AI智能字幕）或字幕URL不可用。"""

