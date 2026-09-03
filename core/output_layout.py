"""B 站任务输出目录和文件名规范。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


PROJECT_DIR_NAME = "bili-project"
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_path_component(value: str, fallback: str = "bilibili-video") -> str:
    """生成可用于 Windows 文件/目录名的安全片段。"""
    cleaned = _INVALID_CHARS.sub("_", str(value or "")).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".", 1)[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:100].rstrip(" .") or fallback


def titled_path_component(value: str, fallback: str = "bilibili-video") -> str:
    """生成统一带书名号的视频标题片段，避免重复包裹。"""
    cleaned = sanitize_path_component(value, fallback)
    inner = cleaned.removeprefix("《").removesuffix("》")
    return f"《{inner}》"


def media_stem(title: str, bvid: str, page: int) -> str:
    return f"{titled_path_component(title)}-{sanitize_path_component(bvid)}-P{page:02d}"


def media_filename(title: str, bvid: str, page: int, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{media_stem(title, bvid, page)}{ext}"


def subtitle_filename(page: int, language: Optional[str] = None) -> str:
    lang = sanitize_path_component(language or "unknown", "unknown")
    return f"P{page:02d}.{lang}.srt"


def transcript_filename(page: int) -> str:
    return f"P{page:02d}.asr.txt"


@dataclass(frozen=True)
class BiliProjectLayout:
    """一次调用对应的独立输出目录。"""

    workspace_dir: Path
    project_root: Path
    run_dir: Path
    media_dir: Path
    subtitles_dir: Path
    transcripts_dir: Path
    analysis_dir: Path

    @classmethod
    def create(
        cls,
        workspace_dir: Path,
        title: str,
        bvid: str,
        now: Optional[datetime] = None,
    ) -> "BiliProjectLayout":
        workspace = Path(workspace_dir).expanduser().resolve()
        project_root = workspace / PROJECT_DIR_NAME
        timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        base_name = (
            f"{titled_path_component(title)} "
            f"[{sanitize_path_component(bvid)}] {timestamp}"
        )
        run_dir = _unique_run_dir(project_root, base_name)
        media_dir = run_dir / "media"
        subtitles_dir = run_dir / "subtitles"
        transcripts_dir = run_dir / "transcripts"
        analysis_dir = run_dir / "analysis"
        for directory in (media_dir, subtitles_dir, transcripts_dir, analysis_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return cls(
            workspace, project_root, run_dir, media_dir,
            subtitles_dir, transcripts_dir, analysis_dir,
        )


def _unique_run_dir(project_root: Path, base_name: str) -> Path:
    project_root.mkdir(parents=True, exist_ok=True)
    candidate = project_root / base_name
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = project_root / f"{base_name}-{suffix:02d}"
    candidate.mkdir()
    return candidate.resolve()
