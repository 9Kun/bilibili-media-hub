import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
	sys.path.insert(0, str(ROOT))

from core import (
	BiliAnalysisPreparer,
	BiliDownloader,
	BiliSubtitleDownloader,
	BiliTranscriber,
)
from scripts.bilibili_download_skill import BilibiliDownloadSkill, State


def _parse_pages(raw: str) -> Optional[List[int]]:
	text = (raw or "").strip()
	if not text:
		return None

	pages = []
	for token in text.split(","):
		token = token.strip()
		if not token:
			continue
		pages.append(int(token))
	return pages or None


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="BilibiliDown skill runner")
	parser.add_argument("--bvid", required=True, help="BV id, such as BV1xx411c7mD")
	parser.add_argument(
		"--save-dir",
		default=".",
		help="Workspace directory; outputs are created under <workspace>/bili-project",
	)
	parser.add_argument("--media-type", default="video", choices=["video", "audio"], help="Media type")
	page_group = parser.add_mutually_exclusive_group()
	page_group.add_argument("--pages", default="", help="Comma-separated page list, such as 1,3,5")
	page_group.add_argument("--all-pages", action="store_true", help="Process all pages without another confirmation")
	parser.add_argument(
		"--quality",
		type=int,
		default=None,
		help="Requested Bilibili video qn, such as 120 for 4K; omitted means highest available",
	)
	parser.add_argument("--ffmpeg-path", default="", help="Custom ffmpeg executable path")
	action_group = parser.add_mutually_exclusive_group()
	action_group.add_argument(
		"--transcribe",
		action="store_true",
		help="Transcribe video to text transcript (requires funasr)",
	)
	action_group.add_argument(
		"--subtitle",
		action="store_true",
		help="Download available Bilibili CC/AI subtitle as .srt file",
	)
	action_group.add_argument(
		"--analyze",
		action="store_true",
		help="Prepare timestamped material for host-Agent video analysis",
	)
	parser.add_argument(
		"--subtitle-lang",
		default="",
		help="Subtitle language code (e.g. zh-CN, ai-zh); empty for auto",
	)
	parser.add_argument(
		"--device",
		default="cpu",
		help="FunASR device for transcription (cpu or cuda)",
	)
	return parser


def main(argv: Optional[List[str]] = None) -> int:
	args = build_parser().parse_args(argv)

	ffmpeg_path = args.ffmpeg_path.strip() if args.ffmpeg_path else None
	downloader = BiliDownloader(ffmpeg_path=ffmpeg_path)

	transcriber = None
	subtitle_downloader = None
	if args.transcribe or args.analyze:
		transcriber = BiliTranscriber(downloader, device=args.device)
	if args.subtitle or args.analyze:
		subtitle_downloader = BiliSubtitleDownloader(downloader)
	analysis_preparer = None
	if args.analyze:
		analysis_preparer = BiliAnalysisPreparer(
			downloader,
			subtitle_downloader,
			transcriber,
		)

	skill = BilibiliDownloadSkill(
		downloader,
		Path(args.save_dir),
		print,
		transcriber=transcriber,
		subtitle_downloader=subtitle_downloader,
		analysis_preparer=analysis_preparer,
	)

	pages = None if args.all_pages else _parse_pages(args.pages)
	if args.quality is not None and args.quality <= 0:
		raise SystemExit("--quality must be a positive Bilibili qn value")
	if args.quality is not None and (args.transcribe or args.subtitle or args.analyze):
		raise SystemExit("--quality is only valid for direct video downloads")
	if args.quality is not None and args.media_type == "audio":
		raise SystemExit("--quality is only valid for video downloads")

	if args.analyze:
		sub_lang = args.subtitle_lang.strip() if args.subtitle_lang else None
		first = skill.analyze_bilibili_media(
			args.bvid, pages=pages, lang=sub_lang, all_pages=args.all_pages
		)
	elif args.transcribe:
		first = skill.transcribe_bilibili_media(
			args.bvid, pages=pages, all_pages=args.all_pages
		)
	elif args.subtitle:
		sub_lang = args.subtitle_lang.strip() if args.subtitle_lang else None
		first = skill.download_bilibili_subtitle(
			args.bvid, pages=pages, lang=sub_lang, all_pages=args.all_pages
		)
	else:
		if args.all_pages or pages is not None:
			first = skill.download_bilibili_multi_p(
				args.bvid,
				args.media_type,
				pages,
				quality=args.quality,
				all_pages=args.all_pages,
			)
		else:
			first = skill.download_bilibili_media(
				args.bvid, args.media_type, quality=args.quality
			)
	print(first)

	deadline = time.time() + 3600
	while skill.state in (State.DOWNLOADING, State.TRANSCRIBING, State.ANALYZING) and time.time() < deadline:
		time.sleep(0.2)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
