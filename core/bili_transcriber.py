"""基于 FunASR Paraformer-zh 的 B 站视频转录核心。

设计要点：
- 复用 BiliDownloader 下载音频，不引入外部下载器。
- 模型延迟加载：__init__ 不触发 FunASR import，首次 transcribe 时才加载，
  避免影响纯下载场景的启动开销。
- 输出带字级时间戳的文字稿，按句号/换行分组，每句一行 [MM:SS] 文本。
"""

import tempfile
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from .bili_downloader import BiliDownloader, Clip, VideoInfo
from .exceptions import (
    TranscribeDependencyError,
    TranscribeError,
    TranscribeModelLoadError,
)
from .output_layout import transcript_filename


# Paraformer 默认配置
DEFAULT_MODEL = "paraformer-zh"
DEFAULT_VAD = "fsmn-vad"
DEFAULT_PUNC = "ct-punc"
DEFAULT_MAX_SEGMENT_MS = 30000  # 30 秒切片，避免长视频 OOM
DEFAULT_BATCH_SIZE_S = 300  # 5 分钟批处理


class BiliTranscriber:
    """B 站视频转录器：下载音频 -> Paraformer 识别 -> 输出带时间戳文字稿。"""

    def __init__(
        self,
        downloader: BiliDownloader,
        model_name: str = DEFAULT_MODEL,
        device: str = "cpu",
        max_single_segment_time: int = DEFAULT_MAX_SEGMENT_MS,
        batch_size_s: int = DEFAULT_BATCH_SIZE_S,
    ):
        self.downloader = downloader
        self._model = None  # 延迟加载
        self._model_name = model_name
        self._device = device
        self._max_segment_ms = int(max_single_segment_time)
        self._batch_size_s = int(batch_size_s)

    def transcribe(
        self,
        bvid: str,
        output_path: Path,
        page: int = 1,
        keep_audio: bool = False,
        info: Optional[VideoInfo] = None,
        clip: Optional[Clip] = None,
    ) -> Path:
        """下载指定分P音频并转录为带时间戳的文字稿。

        :param bvid: B 站视频 BV 号
        :param output_path: 文字稿输出路径（.txt）
        :param page: 分P 页码，从 1 开始
        :param keep_audio: 是否保留临时音频文件（默认清理）
        :return: 文字稿文件路径
        """
        info = info or self.downloader.get_video_info(bvid)
        target_page = int(page)
        clip = clip or next(
            (item for item in info.clips if item.page == target_page), None
        )
        if clip is None:
            raise TranscribeError(f"Page {target_page} not found for bvid: {info.bvid}")
        audio_path = self._download_audio(info, clip)
        try:
            text, timestamps = self._run_paraformer(audio_path)
            content = self._build_transcript(
                info.title, info.owner_name, info.bvid, clip.page, text, timestamps
            )
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
            return output_path.resolve()
        finally:
            if not keep_audio:
                self._safe_remove_temp_audio(audio_path)

    def transcribe_multi_p(
        self,
        bvid: str,
        output_dir: Path,
        pages: Optional[List[int]] = None,
        keep_audio: bool = False,
        info: Optional[VideoInfo] = None,
    ) -> List[Path]:
        """多P批量转录，每P输出一个 .txt 文件。

        :param pages: 指定页码列表；None 表示全部
        :return: 各P文字稿路径列表
        """
        info = info or self.downloader.get_video_info(bvid)
        if not info.clips:
            raise TranscribeError(f"No clips for bvid: {bvid}")

        if pages is None:
            target_pages = [clip.page for clip in info.clips]
        else:
            clip_pages = {clip.page for clip in info.clips}
            target_pages = sorted({int(p) for p in pages if int(p) > 0})
            missing = [p for p in target_pages if p not in clip_pages]
            if missing:
                raise TranscribeError(f"Pages not found: {missing}")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: List[Path] = []
        clip_by_page = {clip.page: clip for clip in info.clips}
        for page in target_pages:
            out_file = output_dir / transcript_filename(page)
            self.transcribe(
                info.bvid,
                out_file,
                page=page,
                keep_audio=keep_audio,
                info=info,
                clip=clip_by_page[page],
            )
            outputs.append(out_file)
        return outputs

    # ---------- 内部实现 ----------

    def _download_audio(self, info: VideoInfo, clip: Clip) -> Path:
        """复用 BiliDownloader 下载指定分P的音频到临时目录。"""
        tmp_dir = Path(tempfile.mkdtemp(prefix="bili_transcribe_"))
        # download_by_page 输出 .m4a 或 .mp3，文件名形如 {title}-p{n}.m4a
        output = self.downloader.download_clip(
            info, clip, str(tmp_dir), None, "audio"
        )
        if not output.exists():
            raise TranscribeError(f"Audio download missing: {output}")
        return output

    def _run_paraformer(self, audio_path: Path) -> Tuple[str, List[List[int]]]:
        """调用 Paraformer 转录，返回 (text, timestamps)。

        text: 空格分隔的字符（如 "欢 迎 大 家"）
        timestamps: 每字符 [start_ms, end_ms]
        """
        self._ensure_model()
        try:
            result = self._model.generate(
                input=str(audio_path),
                batch_size_s=self._batch_size_s,
                language="zh",
                use_itn=True,
            )
        except Exception as exc:
            raise TranscribeError(f"Paraformer inference failed: {exc}") from exc

        if not result:
            raise TranscribeError("Paraformer returned empty result")
        first = result[0]
        text = first.get("text", "") or ""
        timestamps = first.get("timestamp", []) or []
        if not text:
            raise TranscribeError("Paraformer returned empty text")
        return text, timestamps

    def _ensure_model(self) -> None:
        """首次转录时加载 FunASR 模型（约 1.5GB，缓存到 ~/.cache/funasr）。"""
        if self._model is not None:
            return
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise TranscribeDependencyError(
                "缺少 funasr 依赖，请执行: pip install funasr"
            ) from exc
        try:
            self._model = AutoModel(
                model=self._model_name,
                vad_model=DEFAULT_VAD,
                punc_model=DEFAULT_PUNC,
                vad_kwargs={"max_single_segment_time": self._max_segment_ms},
                device=self._device,
                disable_update=True,
            )
        except Exception as exc:
            raise TranscribeModelLoadError(f"FunASR model load failed: {exc}") from exc

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            if path and path.exists():
                path.unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _safe_remove_temp_audio(path: Path) -> None:
        try:
            parent = path.parent
            if parent.name.startswith("bili_transcribe_"):
                shutil.rmtree(parent, ignore_errors=True)
            else:
                BiliTranscriber._safe_unlink(path)
        except Exception:
            pass

    @staticmethod
    def _build_transcript(
        title: str,
        owner: str,
        bvid: str,
        page: int,
        text: str,
        timestamps: List[List[int]],
    ) -> str:
        """组装带时间戳的文字稿内容。"""
        header = (
            f"# {title}\n\n"
            f"**UP主**: {owner or '未知'} | **BV号**: {bvid} | **分P**: P{page}\n\n"
            "---\n\n"
        )
        body = _format_text_with_timestamps(text, timestamps)
        footer = "\n"
        return header + body + footer


# ---------- 模块级辅助函数 ----------


def _format_text_with_timestamps(text: str, timestamps: List[List[int]]) -> str:
    """将 Paraformer 输出整理为按句分段的文字稿。

    Paraformer text 形如 "欢 迎 大 家 。 这 是 测 试 。"
    timestamps 与字符一一对应：[[0,200],[200,400],...]
    输出：
        [00:00] 欢迎大家。
        [00:05] 这是测试。
    """
    if not text:
        return ""

    # 按空格拆分字符，与 timestamps 对齐
    chars = text.split(" ")
    # 处理 text 末尾可能多出的空字符串
    if chars and chars[-1] == "":
        chars = chars[:-1]

    # 若 timestamps 长度与字符数不匹配，按字符数截断
    pair_count = min(len(chars), len(timestamps))

    # 按句子断点（。！？!?,，）聚合
    sentence_breaks = set("。！？!?，,")

    lines: List[str] = []
    current_chars: List[str] = []
    current_start_ms: Optional[int] = None
    last_end_ms: int = 0

    def _flush() -> None:
        nonlocal current_chars, current_start_ms
        if current_chars and current_start_ms is not None:
            sentence = "".join(current_chars).strip()
            if sentence:
                lines.append(f"{_ms_to_stamp(current_start_ms)} {sentence}")
        current_chars = []
        current_start_ms = None

    for i in range(pair_count):
        ch = chars[i]
        if not ch:
            continue
        ts = timestamps[i] if i < len(timestamps) else [last_end_ms, last_end_ms]
        try:
            start_ms = int(ts[0]) if ts and ts[0] is not None else last_end_ms
            end_ms = int(ts[1]) if len(ts) > 1 and ts[1] is not None else start_ms
        except (TypeError, ValueError):
            start_ms = last_end_ms
            end_ms = last_end_ms

        if current_start_ms is None:
            current_start_ms = start_ms
        current_chars.append(ch)
        last_end_ms = end_ms

        if ch in sentence_breaks:
            _flush()

    _flush()
    return "\n".join(lines) + "\n"


def _ms_to_stamp(ms: int) -> str:
    """毫秒 -> [MM:SS]"""
    total_s = int(ms) // 1000
    minutes, secs = divmod(total_s, 60)
    return f"[{minutes:02d}:{secs:02d}]"
