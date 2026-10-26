# 安装与模型准备

## 1. Python 与依赖

需要 **Python 3.10+**。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

装完验证：

```bash
python -c "import sherpa_onnx, docx, openai; print('ok')"
```

## 2. ffmpeg

`asr.py` 用 ffmpeg 把任意音视频转成 16kHz 单声道 wav，`probe_duration()` 用 ffprobe 读时长。
两个都要在 PATH 里：

| 平台 | 命令 |
|---|---|
| Ubuntu / Debian | `sudo apt install ffmpeg` |
| RHEL / CentOS | `sudo dnf install ffmpeg` （可能需要 RPM Fusion） |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` |

装在非标准位置时，用环境变量指路：

```bash
export FFMPEG=/opt/ffmpeg/bin/ffmpeg
export FFPROBE=/opt/ffmpeg/bin/ffprobe
```

## 3. 模型文件

两个 onnx 模型，手动下载。目录结构必须是：

```
models/
├── sensevoice/
│   ├── model.int8.onnx      # 约 228 MB
│   └── tokens.txt           # 约 300 KB
└── vad/
    └── silero_vad.onnx      # 约 630 KB
```

### SenseVoice

从 sherpa-onnx 的模型发布页取 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`：

```bash
cd /tmp
curl -L -O https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
tar xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 \
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx \
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tokens.txt

mkdir -p models/sensevoice
cp sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx models/sensevoice/
cp sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/tokens.txt models/sensevoice/
```

> **这个包约 1 GB，下载容易中断。** 中断后 `tar` 会解出一个体积偏小的
> `model.int8.onnx`，然后在推理时报 `Protobuf parsing failed`。
> 本项目会在加载前校验体积（< 150 MB 直接报错并提示重下），
> 但更省事的做法是下载完先看一眼大小：
>
> ```bash
> ls -lh models/sensevoice/model.int8.onnx    # 应该是 ~228M
> ```
>
> 用 `curl -C -` 续传可以避免重下整个包。

### Silero VAD

```bash
curl -L -o models/vad/silero_vad.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
```

大小约 630 KB，明显小于这个数就是没下完。

## 4. 配置文件

```bash
cp config/settings.example.json config/settings.json
```

三个路径按需改：

| 键 | 说明 |
|---|---|
| `paths.video_dir` | 视频放在哪。相对路径以项目根为基准 |
| `paths.output_dir` | 笔记写在哪。按日期自动分子目录 |
| `asr.model_dir` / `asr.vad_model` | 模型位置，默认就是上面的目录结构 |

`asr.threads` 建议设成 **物理核数的一半**。实测 2 线程已能跑到 40× 实时，
再高收益递减且会和别的服务抢 CPU。

### 配置项会被严格校验

拼错的键会直接报错，不会被静默忽略：

```
ConfigError: 未知配置项：llm.modle（可用项：['api_key_env', 'base_url', ...]）
```

这是刻意的——拼错的配置带来的「行为 silently 不对」比启动失败难查得多。

## 5. 环境变量

密钥只走环境变量，不进配置文件：

| 变量 | 用途 | 必需 |
|---|---|---|
| `MOONSHOT_API_KEY` | 生成笔记（OpenAI 兼容协议） | 是（`--dry-run` 时否） |
| `WECOM_WEBHOOK` | 企微群机器人 | 否（`notify.enabled=true` 时必需） |
| `VNOTES_CONFIG` | 指定配置文件路径 | 否 |
| `FFMPEG` / `FFPROBE` | 指定二进制路径 | 否 |

变量名本身可在配置里改（`llm.api_key_env` / `notify.webhook_env`），
方便换一家 OpenAI 兼容的服务。

## 6. 验证

```bash
vnotes check
```

期望输出末尾是：

```
全部就绪 ✅
```

它会检查：ffmpeg/ffprobe、排班表（并打印覆盖了哪些星期）、笔记规则文件、
两个模型文件（含体积）、sherpa-onnx 与 python-docx 是否可导入、密钥是否设置、有几个待处理视频。

任何一项缺失都会进「发现 N 个问题」列表，**一次报全**，不用来回试。

## 7. 第一次跑

先只转写，看识别质量是否符合预期：

```bash
vnotes transcribe --only <文件名关键词>
head -20 output/*/transcript.txt
```

转写没问题再跑全链路：

```bash
vnotes run --only <文件名关键词>
```

## 8. 定时任务

`run` 是幂等的，失败退出码为 1，适合直接挂 cron 或 systemd timer：

```cron
# 每晚 22:30 处理当天新增
30 22 * * * cd /opt/video-notes-pipeline && /opt/video-notes-pipeline/.venv/bin/vnotes run --days 1 >> /var/log/vnotes.log 2>&1
```

`--days 1` 只处理最近一天，历史场次不会重复扫描；
已经 `delivered` 的场次即使被扫到也会立刻跳过。
