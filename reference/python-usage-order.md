# Agent 首读说明

## 1) 入口与顺序
1. `scripts/bilibili_download_skill.py`
   - 对话入口：状态机、分P范围确认、异步回调、转录入口、字幕下载入口。
2. `core/bili_downloader.py`
   - 下载核心：WBI签名、画质qn选择、播放地址获取、Range探测、重试/CDN切换、FFmpeg合并与ffprobe验收。
3. `core/bili_transcriber.py`
   - 转录核心：复用 `BiliDownloader` 下载音频，调用 FunASR Paraformer-zh 识别，
     输出带字级时间戳的文字稿。模型延迟加载，纯下载场景零开销。
4. `core/bili_subtitle_downloader.py`
   - 平台字幕下载核心：复用 `BiliDownloader` 的 http/signer/指纹cookie，
     调用 `/x/player/wbi/v2` 查询字幕 URL，拉取 JSON 转 SRT 格式。
5. `core/bili_analysis.py`
   - 分析材料准备：逐P优先下载人工/UP主CC或AI智能字幕，仅在 `NoSubtitleError` 时回退
     `BiliTranscriber`，最后生成 `analysis/analysis-input.json` 并区分 `cc`、`ai_subtitle`、`asr`。
6. `core/output_layout.py`
   - 把 `save_dir` 解释为工作文件夹，并为每次任务建立 `bili-project/任务目录/`
     以及 `media`、`subtitles`、`transcripts`、`analysis` 四个子目录。

## 2) 最小调用链
单P调用链：
`download_bilibili_media(bvid, media_type)`
-> 查询信息后识别为单P
-> `_start_download_async()`
-> `BiliProjectLayout.create(save_dir, title, bvid)`
-> `BiliDownloader.download(bvid, path, None, media_type)`
-> `_FileDownloader.download(...)`
-> `_FfmpegMerger.*(...)`
-> `reply("下载完成...")`

多P调用链（范围确认）：
`download_bilibili_media(bvid, media_type)`
-> 查询信息后识别为多P
-> `state = WAIT_CONFIRM`
-> `confirm_download("全部" | "1,3")`
-> `_start_download_async()`
-> `BiliDownloader.download_all_pages(...)`
-> `reply("多P下载完成...")`

显式多P调用链：
`download_bilibili_multi_p(bvid, media_type, pages, quality, all_pages)`
-> `pages=None` 时进入范围确认态（`confirm_download("全部" | "1,3")`）
-> `pages=[1,3]` 或 `all_pages=True` 时视为范围已确认并立即执行

转录调用链：
`transcribe_bilibili_media(bvid, pages)`
-> 单P或 `pages` 已指定：直接 `_start_download_async()`（`pending_action="transcribe"`）
   -> `_start_transcribe_async()`
   -> `BiliTranscriber.transcribe(bvid, output_path, page=1)`
     -> `BiliDownloader.download_by_page(bvid, tmp, page, None, "audio")`
     -> `_ensure_model()` 首次加载 FunASR Paraformer
     -> `model.generate(input=audio, batch_size_s=300)`
     -> 写入 `.txt`，清理临时音频
   -> `reply("转录完成...")`
-> 多P且未指定 pages：进入 `WAIT_CONFIRM`，由 `confirm_download("全部" | "1,3")` 决定范围
   -> `BiliTranscriber.transcribe_multi_p(bvid, layout.transcripts_dir, pages)`
   -> `reply("多P转录完成...")`

字幕下载调用链（从 Java 版 CCDownloader + AbstractBaseParser.getVideoSubtitleLink 迁移）：
`download_bilibili_subtitle(bvid, pages, lang)`
-> 单P或 `pages` 已指定：直接 `_start_download_async()`（`pending_action="subtitle"`，复用 `DOWNLOADING` 状态）
   -> `_start_subtitle_async()`
   -> `BiliSubtitleDownloader.download_subtitle(bvid, output_path, page=1, lang)`
     -> 复用上游一次获取的 `VideoInfo` / `cid`，避免逐P重复请求元数据
     -> `_query_subtitle_url(bvid, cid, lang)`
        -> `BiliDownloader.signer.enc_wbi("/x/player/wbi/v2?bvid=&cid=&isGaiaAvoided=false")`
        -> `BiliDownloader.http.get_json(url, headers, with_fingerprint_cookie=True)`
        -> 从 `data.subtitle.subtitles[]` 按 `lang` 选择（精确匹配，否则取第一个平台字幕）
        -> 补 `https:` 前缀
     -> `_fetch_subtitle_text(sub_url)` 拉取字幕 JSON
     -> `_json_to_srt()` 转为标准 SRT 格式（`HH:MM:SS,mmm`）
     -> 写入 `.srt` 文件
   -> `reply("字幕下载完成...（语言：xxx）")`
-> 多P且未指定 pages：进入 `WAIT_CONFIRM`，由 `confirm_download("全部" | "1,3")` 决定范围
   -> `BiliSubtitleDownloader.download_subtitle_multi_p(bvid, layout.subtitles_dir, pages, lang)`

分析调用链：
`analyze_bilibili_media(bvid, pages, lang)`
-> 单P或已指定 pages：直接 `_start_analysis_async()`（`pending_action="analyze"`）
-> `BiliAnalysisPreparer.prepare(...)`
   -> 每P调用 `BiliSubtitleDownloader.download_subtitle(...)`，复用同一份 `VideoInfo`
   -> 仅捕获 `NoSubtitleError` 并回退 `BiliTranscriber.transcribe(...)`
   -> 其他错误按P写入清单，不静默改走 ASR
   -> 写入 `layout.analysis_dir/analysis-input.json`
-> 宿主 Agent 读取清单与时间戳文本，按 `reference/analysis-report.md` 生成报告
-> 多P且未指定 pages：进入 `WAIT_CONFIRM`，确认后准备全部或指定分P
   -> `reply("多P字幕下载完成...")`

## 3) 分支规则
- `media_type="video"` -> 输出 `.mp4`；可传 `quality=<qn>` 严格指定档位
- `media_type="audio"` -> 优先 `.m4a`，失败回退 `.mp3`
- `pages=None` -> 多P范围待确认（可选全部或指定）
- `pages=[1,3]` / `all_pages=True` -> 显式范围直接执行，不二次确认
- `transcribe_bilibili_media(bvid, pages)` -> 输出 `transcripts/Pxx.asr.txt`（带 [MM:SS] 时间戳）
- `download_bilibili_subtitle(bvid, pages, lang)` -> 输出 `subtitles/Pxx.{语言}.srt`
- `analyze_bilibili_media(bvid, pages, lang)` -> 输出分析清单；报告由宿主 Agent 写入清单中的 `report_path`

## 4) 修改边界（必须遵守）
- 改对话流程：只动 `scripts/bilibili_download_skill.py`
- 改下载能力：只动 `core/bili_downloader.py`
- 改转录能力：只动 `core/bili_transcriber.py`
- 改字幕下载能力：只动 `core/bili_subtitle_downloader.py`
- 改分析材料准备：只动 `core/bili_analysis.py`
- 改分析报告结构：只动 `reference/analysis-report.md` 与对应 `SKILL.md` 路由说明
- 不要在 `scripts/` 重复实现下载、转录或字幕下载逻辑

## 5) 运行前提
- 运行方需自行实例化 `BilibiliDownloadSkill` 并注入：
  - `BiliDownloader`（必需）
  - `save_dir`（用户选择的工作文件夹；实际输出位于其 `bili-project` 子目录）
  - `reply(message)` 回调
  - `BiliTranscriber`（可选；不传则转录入口返回未配置提示）
  - `BiliSubtitleDownloader`（可选；不传则字幕入口返回未配置提示）
  - `BiliAnalysisPreparer`（可选；分析入口必需，内部复用字幕器与转录器）
- 媒体任务要求 `ffmpeg` 与 `ffprobe` 均可执行；显式指定ffmpeg文件时，ffprobe优先从同目录推导，不存在则回退PATH查找。

## 6) 登录态边界
- Skill 不接收、不保存完整 Cookie，也不写入本地文件。
- 登录态优先由环境变量 `BILI_COOKIES` 注入（格式 `key1=val1;key2=val2`）；`BiliDownloader(cookies_path=...)` 作为宿主环境显式注入的兼容入口。
- Skill 只消费已初始化的下载器实例。
- 未提供登录态配置时，按未登录流程运行。
- 未登录时请求会走受限分支（含 `try_look=1`），画质能力按未登录口径执行。

## 7) 画质能力口径
- 未登录态：最高 `720p` 视频，`Hires` 音频。
- 登录态：最高 `8K` 视频，`Hires` 音频。
- 最终可用画质/音质以视频本身、账号权限与接口实时返回为准。
- 运行时将 `accept_quality` 与 `accept_description` 配对展示；静态兜底映射见 `SKILL.md`。
- `quality=None` 自动取最高可用；`quality=<qn>` 严格请求指定档位，不可用时停止并列出档位，不静默降级。
- 下载使用Range探测文件大小；单个CDN最多重试3次后切换备用URL。同任务重试保留分片续传，全部失败或取消后清理分片。
- 输出经ffprobe校验有效媒体流、时长和大小；多P额外校验输出数量。

## 8) 转录依赖
- `pip install funasr`（首次使用转录功能时安装；纯下载与字幕下载不需要）。
- 首次转录会自动下载 Paraformer-zh + fsmn-vad + ct-punc 模型到 `~/.cache/funasr`（约 1.5GB）。
- 默认 `device="cpu"`；如需 GPU 加速，`BiliTranscriber(downloader, device="cuda")`。

## 9) 平台字幕下载说明
- 字幕下载**无需额外依赖**（复用 BiliDownloader 的 http/signer），区别于转录功能。
- 接口：`https://api.bilibili.com/x/player/wbi/v2?bvid=&cid=&isGaiaAvoided=false` + WBI 签名。
- 响应路径：`data.subtitle.subtitles[].lan` / `subtitle_url`。
- 输出 SRT 标准格式（`HH:MM:SS,mmm`，逗号分隔毫秒），优于 Java 版的 `%.2f`。
- 语言选择策略：精确匹配 `lang` 参数 -> 否则取 `subtitles[0]`。
- `lan` 以 `ai-` 开头时记录为 `ai_subtitle`，其他平台字幕记录为 `cc`；本地FunASR记录为 `asr`。
- 优先获取平台已有的人工/UP主CC或AI智能字幕；平台无可用字幕时分析流程才回退FunASR。
- 部分视频无平台字幕或需登录获取AI智能字幕，会抛 `NoSubtitleError`。

## 10) 接入示例
```python
from pathlib import Path
from core import BiliAnalysisPreparer, BiliDownloader, BiliSubtitleDownloader, BiliTranscriber
from scripts.bilibili_download_skill import BilibiliDownloadSkill

downloader = BiliDownloader(ffmpeg_path=r"D:\Data\Desktop\BilibiliDown-6.40\release\ffmpeg.exe")
transcriber = BiliTranscriber(downloader)  # 模型延迟加载，不立即触发
subtitle_downloader = BiliSubtitleDownloader(downloader)  # 无需额外依赖
analysis_preparer = BiliAnalysisPreparer(downloader, subtitle_downloader, transcriber)
skill = BilibiliDownloadSkill(
    downloader, Path("."), print,
    transcriber=transcriber,
    subtitle_downloader=subtitle_downloader,
    analysis_preparer=analysis_preparer,
)

# 单P将直接下载；若目标为多P，会进入范围确认态。
print(skill.download_bilibili_media("BV1bPQFB3EmH", "video", quality=120))
print(skill.confirm_download("全部"))

print(skill.download_bilibili_multi_p("BV1bPQFB3EmH", "video", pages=[1, 3], quality=120))
print(skill.download_bilibili_multi_p("BV1bPQFB3EmH", "video", all_pages=True))

# 转录示例
print(skill.transcribe_bilibili_media("BV1bPQFB3EmH"))           # 单P直接转录
print(skill.transcribe_bilibili_media("BV1bPQFB3EmH", pages=[1, 3]))  # 多P指定分P转录

# 字幕下载示例
print(skill.download_bilibili_subtitle("BV1bPQFB3EmH"))                      # 单P，自动语言
print(skill.download_bilibili_subtitle("BV1bPQFB3EmH", lang="zh-CN"))        # 单P，指定语言
print(skill.download_bilibili_subtitle("BV1bPQFB3EmH", pages=[1, 3]))        # 多P指定分P

# 分析材料：平台CC/AI字幕优先，无平台字幕时回退转录；清单区分cc/ai_subtitle/asr
print(skill.analyze_bilibili_media("BV1bPQFB3EmH", pages=[1, 3]))
```
