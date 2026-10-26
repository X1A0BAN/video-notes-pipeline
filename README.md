# video-notes-pipeline

把一场直播或课程的视频，变成一份可检索、可分享的课堂笔记。

```bash
vnotes check      # 环境自检
vnotes scan       # 看看会处理哪些视频
vnotes run        # 转写 → 笔记 → docx → 推送
```

输入是**磁盘上已经存在的视频文件**；输出是每场一份 markdown 笔记 + 一份排版好的 docx。

---

## 它解决什么问题

一场两小时的直播，事后想找「老师对创业板怎么看」这句话，只有一个办法：拖进度条。
课程的真正价值在内容里，但内容的载体——视频——是线性、不可检索、不可引用的。

这条流水线把视频里的语音抽出来，按语义整理成结构化笔记：
有标题、有风险提示、有风险指数、有板块梳理、有次日预案，可以搜、可以发群、可以归档。

| 阶段 | 做什么 | 产物 |
|---|---|---|
| 采集 | 从目录里发现视频文件 | —— |
| 转写 | SenseVoice + Silero VAD | `transcript.txt`（`[MM.SS] 文本`） |
| 笔记 | 大模型按规则整理 | `*.课堂笔记.md` |
| 排版 | markdown → docx | `*.课堂笔记.docx` |
| 推送 | 企业微信群机器人 | 摘要 + 附件 |

---

## 数据从哪来

**这条流水线不负责获取视频。** 它只读 `config/settings.json` 里 `paths.video_dir`
指向的目录，认 `.mp4/.mkv/.mov/.ts/.flv/...` 这些扩展名，其余一概不管。

视频怎么进入那个目录——录屏、平台导出、课程方提供的文件——属于上游的事，
不在本项目的职责和代码范围内。这样分层的好处是：流水线可以对着任何一个视频文件跑，
不需要账号、不需要网络鉴权、不需要和任何平台的接口约定。

> 如果你想接一个「自动把视频放进 video_dir」的上游，那是一个独立项目，
> 通过文件系统与这里解耦。本项目不提供、也不包含这类适配器。

---

## 快速开始

### 1. 依赖

```bash
pip install -e ".[dev]"        # sherpa-onnx / numpy / python-docx / openai / pytest
```

还需要系统里有 **ffmpeg**（抽音频用）：

```bash
# macOS
brew install ffmpeg
# Ubuntu / Debian
sudo apt install ffmpeg
# Windows
winget install Gyan.FFmpeg
```

### 2. 模型

SenseVoice 与 Silero VAD 两个 onnx 模型需要手动下载，放到 `models/` 下。
完整步骤（含校验和与目录结构）见 **[docs/SETUP.md](docs/SETUP.md)**。

```bash
vnotes check    # 会明确告诉你缺哪个文件、应该多大
```

### 3. 配置

```bash
cp config/settings.example.json config/settings.json
```

按需改三个地方：`paths.video_dir`（视频放哪）、`paths.output_dir`（笔记写哪）、
`llm.model`（用哪个模型）。排班表在 `config/schedule.json`，
笔记规则在 `config/note_rules.md` —— 后者整篇会进 system prompt，改它等于改模型行为。

### 4. 密钥

只通过环境变量注入，不写进配置文件：

```bash
export MOONSHOT_API_KEY=sk-...        # 生成笔记用
export WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...   # 推送用（可选）
```

没有密钥也想跑通流程？加 `--dry-run`，会用占位笔记代替模型输出：

```bash
vnotes run --dry-run
```

---

## 用法

```bash
vnotes scan                              # 列出视频 + 归到哪个目录 + 排班讲师
vnotes run                               # 处理全部
vnotes run --days 3                      # 只处理最近 3 天
vnotes run --only 行情                    # 只处理文件名含「行情」的
vnotes run --force                       # 清除状态重做（产物还在就不重跑对应阶段）
vnotes transcribe                        # 只转写，先看识别质量
vnotes check                             # 自检
```

产物目录按日期分：

```
output/
└── 26.9.1/
    ├── transcript.txt          # 带时间戳的转写稿
    ├── 示例课程.课堂笔记.md
    ├── 示例课程.课堂笔记.docx
    ├── state.json              # 状态机，断点续跑用
    └── run.log
```

### 附加材料

**同一个视频目录**里的 `.txt / .md / .csv` 会被自动当成附加材料，一并喂给模型——
讲师的盘后笔记、当日新闻、自选股清单都可以放这里，用来写「次日预案」。
流水线自己的产物会被排除，不会自己在自己身上滚雪球。

---

## 设计要点

**幂等 + 断点续跑。** 每场的状态存在 `state.json`，五个阶段
`discovered → transcribed → noted → rendered → delivered`。
每个阶段只做「还没做完的那件事」，产物存在就跳过；源视频换了（大小/时间变了）才重置。
失败**不回退阶段**，只记 `failed_step` 与 `last_error`，下次从断点继续。

**单场失败不拖垮整批。** 一场音频坏了，其余场次照常处理完，退出码为 1 供 cron 告警。

**程序化校验，不信任模型自查。** 笔记生成后由代码检查：标题格式、抬头讲师是否在排班表、
日期与星期是否相符、**讲师与排班是否一致**、风险指数是否在 0–100、有无免责声明。
不过就带着错误清单定向重试一次。

**摘要不二次生成。** 推群的摘要字段从笔记正文里**程序化抽取**，
不让模型再写一遍——二次生成是幻觉的主要入口，也会和正文自相矛盾。

**排班是数据，不是代码。** 放在 `config/schedule.json`，
构造时校验「同一星期不能排给两个人」，并由单元测试守着覆盖完整性。

**配置错误一次报全。** `check` 和 `require_files()` 会把所有缺失文件一次列出来，
而不是跑到一半才说少个模型。

---

## 项目结构

```
src/vnotes/
├── config.py      配置加载、合并、校验（未知键会报错，不静默忽略）
├── models.py      VideoMeta 解析、Session 状态机、目录发现
├── schedule.py    排班表：日期 → 讲师
├── asr.py         音频抽取 + SenseVoice 转写
├── note_writer.py 提示词组装 → 模型调用 → 输出校验
├── md2docx.py     只为笔记写的最小 markdown 渲染器
├── notifier.py    企微推送 + 摘要程序化抽取
├── pipeline.py    编排（幂等、续跑、失败隔离）
└── cli.py         命令行
config/            配置、排班表、笔记规则
tests/             110 项单元测试，不依赖模型/网络/ffmpeg
docs/              安装说明、架构说明
```

## 测试

```bash
pytest                       # 110 项，约 2 秒
pytest --cov=vnotes          # 覆盖率
```

测试不需要模型、不需要网络、不需要 ffmpeg——所有外部依赖都被替换。
这样它们才能在 CI 和任何人的机器上跑得一样。

## 实测

| 项 | 结果 | 环境 |
|---|---|---|
| 转写速度 | **39.9× 实时**（5 分钟音频 7.5 秒） | 2 线程 CPU，SenseVoice int8 |
| 领域词 | 「创业板」「见顶」「见底」均正确 | 同上 |
| VAD 静音裁剪 | 5 分钟切 66 段，说话 3.1 分钟（62%） | 同上 |

## 已知限制

- 不做实时转写，只处理已经落盘的文件。
- 不做多租户和 Web 界面，面向单人/单团队。
- 转写稿的谐音（尤其个股名）无法根除，规则里要求原样保留并提示核对，而不是猜。
- 视频获取不在范围内（见上）。
