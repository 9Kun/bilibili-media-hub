---
name: bilibili-media-hub
description: >
  通过 BV 号处理 B 站视频：按指定画质下载视频/音频、转录文字稿、下载平台已有的人工/UP主CC或AI智能字幕，或基于时间戳文本生成摘要、总结与章节时间线。
  当用户说"下载B站视频""B站转录""下载CC字幕""分析B站视频""总结B站视频"等时使用。
metadata:
  version: "1.3.1"
---

# bilibili-media-hub

> **这个技能是什么？** 一个帮你把 B 站视频搬回家的助手。它会下载视频/音频、
> 把视频里的语音转成带时间戳的文字稿、拉取 B 站已有的人工/UP主CC或AI智能字幕，或生成带章节时间线的中文分析报告。
>
> **它能帮你解决什么？**
> - 看不了的想留着：把视频/音频下到本地
> - 听不清的想读出来：视频转文字稿，带 [MM:SS] 时间戳
> - 字幕想单独用：直接下载 B 站平台已有字幕为标准 SRT 文件
> - 视频太长不想逐段看：生成摘要、详细总结、核心要点和章节时间线
>
> **遇到问题？** → 跳到 [错误速查表](#错误速查表) 或 [常见问题 FAQ](#常见问题-faq)

---

## 快速开始

**直接把 BV 号发过来就行。** 几个典型开场白：

```
下载这个B站视频 BV1xx411c7mD
把 BV1xx411c7mD 的音频下下来
帮我转录 BV1xx411c7mD 这个视频
下载 BV1xx411c7mD 的字幕
分析并总结 BV1xx411c7mD
```

**最小命令（CLI）：**

```powershell
Push-Location "d:\Data\Desktop\BilibiliDown-6.40\BilibiliDown"
# 下载视频
python -u ".\scripts\skill_runner.py" --bvid "BV1xx411c7mD" --save-dir "D:\Data\Desktop\科大讯飞skill 26年夏" --ffmpeg-path "..\release\ffmpeg.exe"
# 指定 4K（qn=120）并只下载 P1、P3；显式范围无需二次确认
python -u ".\scripts\skill_runner.py" --bvid "BV1xx411c7mD" --quality 120 --pages "1,3" --ffmpeg-path "..\release\ffmpeg.exe"
# 下载音频
python -u ".\scripts\skill_runner.py" --bvid "BV1xx411c7mD" --media-type audio --ffmpeg-path "..\release\ffmpeg.exe"
# 转录视频
python -u ".\scripts\skill_runner.py" --bvid "BV1xx411c7mD" --transcribe --ffmpeg-path "..\release\ffmpeg.exe"
# 下载人工/UP主CC或AI智能字幕
python -u ".\scripts\skill_runner.py" --bvid "BV1xx411c7mD" --subtitle --ffmpeg-path "..\release\ffmpeg.exe"
# 准备分析材料（宿主 Agent 随后生成并保存报告）
python -u ".\scripts\skill_runner.py" --bvid "BV1xx411c7mD" --analyze --ffmpeg-path "..\release\ffmpeg.exe"
Pop-Location
```

> 首次用转录功能需先 `pip install funasr`，并会自动下载约 1.5GB 模型（仅一次）。
> 下载和字幕功能只需 `pip install httpx`。
> 媒体下载需要同目录下可用的 `ffmpeg` 与 `ffprobe`；前者合并/转码，后者自动验收结果。

### 输出目录规范

`--save-dir` 表示用户选取的**工作文件夹**，不是最终文件目录。每次真正开始任务时统一创建：

```text
{工作文件夹}\bili-project\《{安全标题}》 [{BV号}] {YYYYMMDD-HHmmss}\
├─ media\
├─ subtitles\
├─ transcripts\
└─ analysis\
```

- 每次调用创建一个独立任务目录；同一秒重名时追加 `-02`、`-03`。
- Windows 非法字符、结尾空格/句点及 `CON`、`NUL` 等保留名称会自动安全化。
- 仅等待多P确认时不创建目录；确认启动后一次性创建四个子目录。
- 视频/音频：`media\《{安全标题}》-{BV号}-P01.mp4|m4a|mp3`
- 同一用户任务要求多个画质/音质版本时，只创建一个任务目录，全部放入同一个 `media\`；文件名追加 `-480p`、`-最高画质`、`-最高音质` 等后缀，禁止为每个版本另建任务目录或相互覆盖。
- 平台字幕：`subtitles\P01.{实际语言}.srt`；清单来源区分 `cc` 与 `ai_subtitle`
- ASR 文字稿：`transcripts\P01.asr.txt`
- 分析清单与报告：`analysis\analysis-input.json`、`analysis\report.md`

---

## 四大能力

### 01 · 下载视频 / 音频

**触发方式：** `下载B站视频` / `下载B站音频` / `保存BV` / `bilibili下载`

| 需求 | 命令参数 | 输出 |
|------|---------|------|
| 单P视频 | `--media-type video` | `media\{安全标题}-{BV号}-P01.mp4` |
| 单P音频 | `--media-type audio` | `media\{安全标题}-{BV号}-P01.m4a`（不支持时回退 `.mp3`） |
| 指定画质 | `--quality 120` | 严格请求 qn=120（4K），不可用时列出实际档位并停止 |
| 指定分P | `--pages "1,3"` | 每P一个文件 |
| 全部分P | `--all-pages` | 每P一个文件 |

**示例：**

```
用户：下载 BV1xx411c7mD 的第 1 和第 3 P
Agent：python skill_runner.py --bvid BV1xx411c7mD --quality 120 --pages "1,3" --ffmpeg-path ..\release\ffmpeg.exe
```

- 显式给出 `--pages` 或 `--all-pages` 即代表范围已经确认，CLI 直接执行；只有多P且未给范围时才进入对话确认。
- 未给 `--quality` 时自动选择当前账号可用的最高画质；给出后严格请求该 qn，不静默降级。
- 完成后自动运行 `ffprobe`，核验媒体流、时长、文件大小；多P还核验计划数量与实际文件数量。
- 同一任务内网络重试会复用 `.part` 分片；主CDN连续失败后切换备用CDN。任务取消或全部重试失败后清理临时分片。

### 02 · 视频转录为文字稿

**触发方式：** `B站转录` / `提取文字稿` / `视频转文字` / `bilibili转录`

| 需求 | 命令参数 | 输出 |
|------|---------|------|
| 单P转录 | `--transcribe` | `transcripts\P01.asr.txt` |
| 指定分P | `--transcribe --pages "1,3"` | 每P一个 `.txt` |
| 全部分P | `--transcribe --all-pages` | 每P一个 `.txt` |
| GPU 加速 | `--transcribe --device cuda` | 同上（需 CUDA 环境） |

**输出格式：**

```
# 视频标题

**UP主**: 作者名 | **BV号**: BV1xxxxx | **分P**: P1

---

[00:00] 欢迎大家来体验。
[00:05] 这是一个测试视频。
[00:12] 主要演示转录功能。
```

**说明：**
- 转录用 FunASR **Paraformer-zh** 模型，带**字级时间戳**
- 首次运行自动下载模型到 `~/.cache/funasr`（约 1.5GB，仅一次）
- 默认 CPU 运行；GPU 加速加 `--device cuda`
- 内部会先下载音频到临时目录，转录完自动清理

### 03 · 下载平台字幕（CC / AI智能字幕）

**触发方式：** `下载B站字幕` / `下载CC字幕` / `下载AI字幕` / `保存字幕` / `bilibili字幕下载` / `字幕srt`

| 需求 | 命令参数 | 输出 |
|------|---------|------|
| 单P字幕 | `--subtitle` | `subtitles\P01.{实际语言}.srt` |
| 指定语言 | `--subtitle --subtitle-lang "zh-CN"` | 同上 |
| 指定分P | `--subtitle --pages "1,3"` | 每P一个 `.srt` |

**输出格式（标准 SRT）：**

```
1
00:00:00,500 --> 00:00:02,300
大家好

2
00:00:02,300 --> 00:00:05,000
欢迎来看
```

**说明：**
- 字幕下载**无需 funasr 依赖**，只复用下载器的 HTTP/签名能力
- 优先获取B站平台已有字幕，包括人工/UP主CC字幕和AI智能字幕；平台无可用字幕时，分析流程才回退本地FunASR转录
- `--subtitle-lang` 留空时自动取接口返回的第一个可用平台字幕
- 找不到指定语言时回退取第一个，并在结果中提示实际语言
- `ai-*` 语言代码记录为 `ai_subtitle`，其他平台字幕记录为 `cc`；本地转录记录为 `asr`
- 部分视频无平台字幕或AI字幕需要登录，会返回友好提示

---

### 04 · 分析、总结视频并生成章节时间线

**触发方式：** `分析B站视频` / `总结B站视频` / `生成视频摘要` / `提炼视频要点` / `视频章节时间线`

| 需求 | 命令参数 | 输出 |
|------|---------|------|
| 单P分析 | `--analyze` | `analysis\analysis-input.json` + `analysis\report.md` |
| 指定分P | `--analyze --pages "1,3"` | 逐P分析并附全视频总览 |
| 全部分P | `--analyze --all-pages` | 逐P分析并附全视频总览 |
| 指定字幕语言 | `--analyze --subtitle-lang "zh-CN"` | 优先该语言，实际语言写入清单 |

**宿主 Agent 必须完成的后续步骤：**

1. 运行 `--analyze`，等待“分析材料准备完成”；不要提前总结。
2. 读取输出的 `analysis\analysis-input.json`。每P优先使用B站平台已有的人工/UP主CC或AI智能字幕；只有平台确实无可用字幕时，Python 才会回退FunASR转录。
3. 若 `success_count` 为 0，向用户说明失败原因，不生成报告；否则读取所有 `status=ready` 的时间戳文本。
4. 按 [分析报告规范](./reference/analysis-report.md) 生成中文报告，写入清单的 `report_path`，并在对话中返回同一份完整报告。

**边界：**
- Python 不调用任何 LLM；摘要、总结和章节划分由当前宿主 Agent 完成。
- 不下载视频、不取帧、不分析画面；只依据标题、简介、平台字幕或ASR文字稿。
- 章节起点必须来自 SRT 或 `[MM:SS]` 的真实时间戳，禁止推测。

---

## 登录态与画质

下载画质取决于登录态。**想要更高画质，需要提供 cookies。**

| 状态 | 视频最高画质 | 音频最高音质 |
|------|-------------|-------------|
| 未登录 | 720p | hires |
| 已登录 | 8K | hires |

### 视频 qn 档位

运行时优先使用播放接口返回的 `accept_quality` + `accept_description`；下表仅作接口未返回描述时的兜底，不需要 Agent 再读源码探索。

| qn | 档位 |
|---:|------|
| 127 | 8K 超高清 |
| 126 | 杜比视界 |
| 125 | HDR 真彩色 |
| 120 | 4K 超清 |
| 116 | 1080P 60帧 |
| 112 | 1080P 高码率 |
| 80 | 1080P |
| 74 | 720P 60帧 |
| 64 | 720P |
| 32 | 480P |
| 16 | 360P |
| 6 | 240P |

- `--quality <qn>` 仅用于直接视频下载；音频、字幕、转录和分析任务不接受该参数。
- 多P任务逐P请求同一 qn；任一分P不支持时明确报告该P实际可用档位，不静默换档。
- 多P开始前只请求一次视频元数据并在各分P间复用；画质仍以各分P播放接口的实际响应为准。

**如何提供 cookies（用于更高画质 / AI 字幕）：**
- 通过环境变量 `BILI_COOKIES` 注入，需包含以下字段：
  `sid=xxx; DedeUserID__ckMd5=yyy; DedeUserID=zzz; bili_jct=aaa; SESSDATA=bbb`
- 不写入本地文件，避免会话凭据落盘
- 最终可用画质以视频本身、账号权限与接口实时返回为准

> 字幕下载同理：部分AI智能字幕需要登录才能获取，未登录时可能只能拿到人工/UP主CC字幕。

---

## 能力边界

### ✅ 擅长处理

- 通过 BV 号下载视频/音频（单P、指定分P、全部分P）
- 视频转录为带时间戳的文字稿（中文字级时间戳）
- 下载 B 站已有的人工/UP主CC或AI智能字幕为标准 SRT 文件
- 音频无损抽取（优先 `.m4a`，不支持时回退 `.mp3`）

### ⚠️ 需要你提供

- **BV 号**：必须有，从视频链接或分享文本里提取
- **cookies**（可选）：想要 720p 以上画质或获取 AI 字幕时需要
- **FFmpeg 路径**（可选）：默认使用PATH中的 `ffmpeg`/`ffprobe`；显式指定 `ffmpeg.exe` 时优先取同目录的 `ffprobe.exe`，不存在则回退PATH查找

### ❌ 超出能力范围

- **不能下载会员专享视频**：即使登录也需要账号有相应权限
- **不能转录无音频的视频**：纯图片/文字类视频没有音轨可转
- **不能凭空生成平台字幕**：字幕下载只能拉B站已有的CC/AI字幕；平台没字幕时分析流程才走本地FunASR
- **不能并发处理**：同一时间只能跑一个下载/转录/字幕任务

> 💡 **分不清转录和字幕下载？** 转录是用 AI 模型识别音频生成文字稿（视频没字幕也能用）；
> 字幕下载是直接拉B站已有的人工/UP主CC或AI智能字幕文件（视频得有平台字幕才能用）。

---

## 常见问题 FAQ

**Q1：转录和字幕下载有什么区别？用哪个？**

转录用本地AI模型识别音频，准确率受口音/噪声影响。字幕下载直接拉B站已有的人工/UP主CC或AI智能字幕。**优先获取平台字幕，平台无可用字幕时再走FunASR。**

**Q2：第一次转录很慢？**

正常。首次会下载 Paraformer-zh 模型（约 1.5GB），之后都从本地缓存加载。模型缓存到 `~/.cache/funasr`，删了会重新下。

**Q3：下载的视频画质只有 720p？**

未登录态最高 720p。想要更高画质，设置环境变量 `BILI_COOKIES`（格式 `SESSDATA=xxx;bili_jct=yyy`）后重试，无需写入本地文件。

**Q4：字幕下载提示“未找到平台字幕”？**

三种可能：没有人工/UP主CC字幕 / AI智能字幕需要登录 / 视频本身无平台字幕。可提供登录态后重试，或改用FunASR转录。

**Q5：多P视频怎么指定下载哪几P？**

用 `--pages "1,3,5"` 参数，逗号分隔页码。`--all-pages` 下载全部；两者都视为范围已确认并直接执行。不传参数时单P直接下，多P才会询问范围。

**Q6：GPU 加速怎么开？**

转录命令加 `--device cuda`。需要 CUDA 环境。显存不足会报错，改回 `--device cpu` 即可。

---

## 错误速查表

| 症状 | 原因 | 解决 |
|------|------|------|
| `BV号无效或不存在` | BV 号拼错或视频被删 | 检查 BV 号，从视频链接重新复制 |
| `FFmpeg 未找到或执行失败` | 缺 FFmpeg 或路径不对 | 确认 `release/ffmpeg.exe` 存在，或用 `--ffmpeg-path` 指定 |
| `网络请求失败` | 网络不通或被风控 | 检查网络，必要时提供 cookies |
| `Requested quality ... unavailable` | 指定 qn 在该分P不可用 | 从返回的可用档位中重新选择，或省略 `--quality` 自动取最高 |
| `ffprobe validation failed` | 输出无有效媒体流、时长或大小异常 | 检查FFmpeg/ffprobe路径并重试 |
| `未安装转录依赖 funasr` | 没装 funasr | `pip install funasr` |
| `FunASR 模型下载证书失败` | 下载模型时 SSL 校验失败 | 检查网络，或手动下载模型到 `~/.cache/funasr` |
| `显存不足` | GPU 显存不够 | 改用 `--device cpu` |
| `未找到平台字幕` | 视频无CC/AI字幕或AI字幕需登录 | 提供cookies后重试，或改用FunASR转录 |
| `转录失败：{msg}` | 转录过程其他错误 | 看具体错误信息，通常是模型加载问题 |

---

## 反模式

| ❌ 不推荐的做法 | ✅ 更好的做法 |
|--------------|------------|
| 视频有平台CC/AI字幕还硬走转录 | 先获取平台字幕，没有再用FunASR |
| 一次启动多个下载/转录任务 | 一个跑完再跑下一个 |
| 想要高画质但不设置 BILI_COOKIES 环境变量 | 通过环境变量 `BILI_COOKIES` 注入会话凭据 |
| 为绕过 funasr 引入外部转录工具/模型 | 直接用本 skill 的转录功能 |
| 字幕下载自己实现 WBI 签名 | 复用 `BiliDownloader.http` / `BiliDownloader.signer` |
| 把平台字幕和本地转录搞混 | `cc`/`ai_subtitle`=拉已有文件，`asr`=本地识别音频 |

---

## 技术参考

> 以下是 Agent 内部实现参考，普通用户可忽略。

### 入口文件

- `scripts/skill_runner.py` — CLI 运行入口
- `scripts/bilibili_download_skill.py` — 状态编排器（含 `ANALYZING`）
- `core/bili_downloader.py` — 下载器（WBI 签名、FFmpeg 合并）
- `core/bili_transcriber.py` — 转录器（FunASR Paraformer-zh）
- `core/bili_subtitle_downloader.py` — 平台字幕下载器（CC/AI → SRT）
- `core/bili_analysis.py` — 分析材料准备器（平台字幕优先、ASR回退、来源分类）
- `core/exceptions.py` — 异常定义
- `reference/python-usage-order.md` — Agent 首读说明
- `reference/analysis-report.md` — 宿主 Agent 总结与章节时间线规范

### 调用入口

| 能力 | Skill 方法 | 底层调用 |
|------|-----------|---------|
| 下载 | `download_bilibili_media(bvid, media_type, quality)` | `BiliDownloader.download(..., quality=)` |
| 多P下载 | `download_bilibili_multi_p(bvid, media_type, pages, quality, all_pages)` | `BiliDownloader.download_all_pages(...)` |
| 转录 | `transcribe_bilibili_media(bvid, pages)` | `BiliTranscriber.transcribe(...)` |
| 字幕 | `download_bilibili_subtitle(bvid, pages, lang)` | `BiliSubtitleDownloader.download_subtitle(...)` |
| 分析 | `analyze_bilibili_media(bvid, pages, lang)` | `BiliAnalysisPreparer.prepare(...)` + 宿主 Agent |

### 状态机

- `IDLE` → 解析 BV → 启动异步任务
- `WAIT_CONFIRM` → 多P未指定范围时等待用户确认
- `DOWNLOADING` → 媒体下载与字幕下载（复用此状态）
- `TRANSCRIBING` → 转录中
- `ANALYZING` → 准备分析材料；总结仍由宿主 Agent 在准备完成后执行

### BV 解析

正则 `BV[0-9A-Za-z]+`（大小写不敏感），前缀标准化为大写 `BV`。

### 错误映射（完整版）

| 错误类型 | 匹配关键词 | 友好提示 |
|---------|-----------|---------|
| BV 无效 | `invalid bvid` / `empty pagelist` | BV号无效或不存在，请检查后重试。 |
| FFmpeg | `cannot run program` / `createprocess error=2` / `ffmpeg failed` | FFmpeg 未找到或执行失败，请确认路径。 |
| 网络 | `timed out` / `unknownhost` / `http` | 网络请求失败，请检查网络。详细错误：{msg} |
| 转录依赖缺失 | `no module named 'funasr'` | 未安装转录依赖 funasr，请执行：pip install funasr |
| 模型证书 | `ssl` + `certificate` | FunASR 模型下载证书校验失败，检查网络或手动下载模型 |
| 显存不足 | `cuda` + `out of memory` | 显存不足，请指定 device='cpu' |
| 转录其他 | `paraformer` / `funasr` / `model load` | 转录失败：{msg} |
| 字幕不存在 | `NoSubtitleError` / `no platform subtitle` | 未找到人工/UP主CC或AI智能字幕；提供登录态或改用FunASR。 |
| 兜底 | — | 发生错误：{msg} |
