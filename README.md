# video-notes-pipeline

将投顾直播视频自动转换成结构化课堂笔记，支持全流程自动化：转写 → 笔记生成 → 文档排版 → 群推送。

```bash
vnotes check      # 环境自检
vnotes scan       # 扫描待处理视频
vnotes run        # 完整流水线：转写 → 笔记 → docx → 推送
```

**输入**：磁盘上的视频文件（`.mp4/.mkv/.mov` 等）  
**输出**：每场一份 markdown 笔记 + 排版好的 Word 文档 + 企微群消息

---

## 问题陈述

投顾直播往往时长 1-2 小时，事后想精准定位某个观点或数据点，就得拖动进度条反复查找——耗时且低效。

这条流水线的目标是：
- **把视频语音转写成结构化笔记**，支持快速搜索和引用
- **一致的笔记格式**，包含风险提示、板块分析、操作建议、次日预案等明确分段
- **自动校验和质量控制**，确保笔记逻辑完整、风险指数准确
- **一键推送**到企业微信群，方便团队分享和存档

---

## 核心流程

| 阶段 | 功能 | 输出 |
|------|------|------|
| 采集 | 扫描视频目录，按日期分类 | 任务队列 |
| 转写 | 语音 → 文本（SenseVoice + 静音检测） | `transcript.txt`（带时间戳） |
| 笔记 | LLM 按既定规则整理转写稿 | `*.课堂笔记.md` |
| 排版 | Markdown → Word 文档 | `*.课堂笔记.docx` |
| 推送 | 摘要 + 附件 → 企微群机器人 | 群消息 + 文件 |

---

## 数据隐私与设计原则

**这个项目不负责视频获取。** 输入契约是"磁盘上已经存在的视频文件"，不与任何直播平台集成，不处理账号认证或网络下载。

这样做有两个好处：
1. **隐私保护**：视频完全在本地处理，没有三方依赖或网络传输
2. **独立性**：流水线对视频来源无感知，可以处理任何来源的本地视频文件

---

## 快速开始

### 1. 依赖安装

```bash
pip install -e ".[dev]"
```

需要的包：sherpa-onnx（离线转写）、numpy、python-docx、openai（兼容 API）、pytest

系统依赖：**ffmpeg**（音频提取）

```bash
# macOS
brew install ffmpeg
# Ubuntu / Debian
sudo apt install ffmpeg
# Windows
winget install Gyan.FFmpeg
```

### 2. 下载转写模型

两个 ONNX 模型需要手动下载。完整步骤和校验见 **[docs/SETUP.md](docs/SETUP.md)**。

```bash
vnotes check    # 会列出缺失文件及其大小
```

### 3. 配置

```bash
cp config/settings.example.json config/settings.json
```

修改以下字段：
- `paths.video_dir`：视频放在哪个目录
- `paths.output_dir`：笔记输出到哪里
- `llm.model`：选择 LLM 模型（默认支持 Moonshot）

排班表 `config/schedule.json` 和笔记规则 `config/note_rules.md` 也需要按实际情况调整。

### 4. API 密钥

通过环境变量注入（不写进代码）：

```bash
export MOONSHOT_API_KEY=sk-...                    # LLM API
export WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...   # 企微推送（可选）
```

**没有密钥也能测试**：加 `--dry-run` 会用示例笔记代替模型调用

```bash
vnotes run --dry-run
```

---

## 用法示例

```bash
vnotes scan                    # 列出待处理视频
vnotes run                     # 处理全部
vnotes run --days 3            # 只处理最近 3 天的
vnotes run --only 关键词        # 只处理文件名含该词的
vnotes run --force             # 清除进度，重新处理
vnotes transcribe              # 仅转写，先检查识别质量
vnotes check                   # 环境和配置自检
```

### 产物目录结构

```
output/
└── 26.9.1/                    # 日期子目录
    ├── transcript.txt         # 带时间戳的转写稿
    ├── 视频标题.课堂笔记.md
    ├── 视频标题.课堂笔记.docx
    ├── state.json             # 状态文件（支持断点续跑）
    └── run.log
```

### 附加材料

同目录下的 `.txt`, `.md`, `.csv` 文件会自动作为补充材料提供给模型，可用于：
- 讲师自己的要点总结
- 市场新闻摘要
- 自选股票清单
- 其他参考资料

这些文件会进入笔记的"次日预案"等部分，帮助生成更完整的分析。

---

## 设计特点

### 幂等性与断点续跑
每场视频有 `state.json` 跟踪进度，分为五个阶段：
```
discovered → transcribed → noted → rendered → delivered
```

- **产物存在就跳过**：若笔记已生成，下次运行不再重新调用 LLM
- **源文件变化检测**：视频大小或修改时间变了才重置进度
- **失败不回退**：记录错误阶段和日志，下次从断点继续

### 单场隔离，全批容错
一场视频处理失败（坏音频、API 超时等）不会中断其他视频，整批处理完成后返回错误码供 cron 告警。

### 程序化质量控制
笔记生成后由代码自动检查：
- 标题格式是否符合规范
- 讲师是否在排班表中
- 日期与星期是否对应
- 风险指数是否在 0-100 范围
- 是否包含免责声明

检查未过则**定向重试一次**，而非直接放行。

### 摘要不二次生成
推送到企微的摘要从笔记正文**程序化抽取**，避免 LLM 再写一遍导致幻觉或自相矛盾。

### 排班作为数据
讲师排班表存在 `config/schedule.json`，通过结构化数据管理而非硬编码。构造时验证"同一星期不能排给多人"，通过单元测试确保完整性。

### 配置错误一次报全
运行前的 `check` 命令会一次列出所有缺失文件、模型、环境问题，而不是逐个报错。

---

## 项目结构

```
src/vnotes/
├── config.py      # 配置加载、合并、校验
├── models.py      # 视频元数据、状态机、目录发现
├── schedule.py    # 讲师排班：日期 → 排班讲师
├── asr.py         # 音频提取 + 离线转写
├── note_writer.py # 提示词组装、LLM 调用、输出校验
├── md2docx.py     # Markdown → Word 转换
├── notifier.py    # 企微推送、摘要抽取
├── pipeline.py    # 编排逻辑：幂等、续跑、失败隔离
└── cli.py         # 命令行接口

config/
├── settings.example.json   # 配置模板
├── schedule.json           # 讲师排班表
├── note_rules.md           # 笔记生成规则（进入 LLM prompt）

docs/
├── SETUP.md                # 详细安装说明
└── ARCHITECTURE.md         # 系统设计文档

tests/                       # 115 项单元测试
```

---

## 测试

```bash
pytest                    # 115 项测试，约 1.5 秒
pytest --cov=vnotes      # 覆盖率报告
```

测试不依赖外部服务：
- 没有 LLM 调用（用 mock 替代）
- 没有网络请求
- 没有 ffmpeg（音频处理用 mock）
- 没有模型文件加载

这确保了测试在任何环境都能快速、可靠地运行。

---

## 实测性能

| 指标 | 结果 | 环境 |
|------|------|------|
| 转写速度 | 39.9× 实时（5 分钟音频耗时 7.5 秒） | 2 线程 CPU，SenseVoice int8 |
| 领域词识别 | 投资常用词汇正确率高 | 同上 |
| VAD 检测 | 5 分钟音频切成 66 段，实际说话时长 3.1 分钟 | 同上 |

---

## 已知限制

- **不做实时转写**：仅处理已保存到磁盘的视频文件
- **不支持多租户**：设计用于单人或单团队使用
- **不自动纠正谐音**：如股票代码等容易被识别为谐音，保留原样并提示人工核查
- **不处理视频获取**：输入必须是磁盘上的本地文件

---

## 许可

MIT

---

## 贡献

欢迎提交 Issue 和 PR。关于笔记规则和排班表的改进，请先在 `tests/` 添加对应的测试用例。
