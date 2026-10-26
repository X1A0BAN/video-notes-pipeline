#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""note_writer.py — 转写稿 → 结构化课堂笔记 markdown。

拆成三块，每块都能单独测：
    build_prompt()     组装 system / user 消息（纯字符串，无副作用）
    generate()         调模型（OpenAI 兼容协议，可换 base_url/model）
    validate()         程序化校验输出契约（不依赖模型自查）

设计取舍：不做多轮 tool-use agent。任务是确定性的文档转换，
        「单次结构化生成 + 校验 + 定向重试」的失败模式更明确、成本可控。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import VideoMeta
from .schedule import Schedule, weekday_cn

SYSTEM_CONTRACT = """你是一名直播复盘笔记整理员，把直播语音转写稿整理成「课堂笔记」markdown。

【硬性规则】（以下全文引用自项目的笔记整理规则，必须逐条遵守）
{rules}

【输出契约】
- 只输出笔记 markdown 正文：不要前言、解释、后记，不要用 ``` 代码围栏包裹
- 标题格式：# 📒 课堂笔记 · <主题>
- 第二行抬头引用块：> 讲师：<姓名>（投顾编号：<如有>）｜ 日期：<YYYY-MM-DD>（周X晚）｜ 场次：{session_label}
- 正文结构（按当场内容增删合并，但风险提示与风险指数不可省）：
  一句话主旨（加粗）→ ## ⚠️ 风险提示（本场前置，含风险指数）→ 大框架/市场环境 →
  板块梳理 → 个股与操作 → 次日预案 → 操作纪律
- 风险指数 0–100，五个维度各 0–20：指数趋势 / 量能 / 情绪分化 / 监管 / 外部事件；
  0–30 低风险（可积极）｜31–60 中风险（谨慎做多，控仓低吸）｜61–100 高风险（防守为主），
  指数后附一句话操作含义
- 数字、日期、点位照抄原文，不得脑补；⭐ 标核心结论；关键对比用 markdown 表格
- 个股名称后注明「标的名称系语音转写谐音，请自行核对代码」
- 文末单独一行：*备注：本笔记为直播观点整理，非投资建议。*
- 转写稿里明确没讲到的内容，宁可不写，也不要补全"""


@dataclass
class PromptPair:
    system: str
    user: str


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_feedback(self) -> str:
        return "\n".join(f"- {e}" for e in self.errors)


def _prior_for(meta: VideoMeta, schedule: Schedule) -> str:
    """文件名只能给出「先验」，最终以转写内容为准。"""
    d = meta.recorded_on
    wd_cn = weekday_cn(d)
    lec = schedule.for_date(d)
    if lec:
        return (f"按文件名推断的录制日期：{d.isoformat()}（周{wd_cn}）"
                f"→ 按排班表应为：{lec.name}"
                + (f"（投顾编号：{lec.registration_id}）" if lec.registration_id else "")
                + "（这只是先验，若转写内容给出了不同日期，以内容为准）")
    return (f"按文件名推断的录制日期：{d.isoformat()}（周{wd_cn}）"
            "→ 该日不在固定排班内（可能是休市或临时加播），请完全依据转写内容判断")


def build_prompt(
    meta: VideoMeta,
    transcript: str,
    rules: str,
    schedule: Schedule,
    extras: list[tuple[Path, str]] | None = None,
) -> PromptPair:
    system = SYSTEM_CONTRACT.format(rules=rules.strip(), session_label=schedule.session_label)

    parts = [
        "【本场元数据】",
        f"视频文件名：{meta.path.name}",
        _prior_for(meta, schedule),
        f"排班表：{_schedule_digest(schedule)}",
        "（讲师必须按**识别出的直播日期**的星期几对照排班表确定，不得凭话术风格猜讲师。）",
        "",
        "【附加材料】",
    ]
    if extras:
        for path, text in extras:
            parts.append(f"--- {path.name} ---")
            parts.append(text)
    else:
        parts.append("无")
    parts += ["", "【转写稿全文】", transcript]
    return PromptPair(system=system, user="\n".join(parts))


def _schedule_digest(schedule: Schedule) -> str:
    rows = []
    for lec in schedule.lecturers:
        days = "、".join(f"周{weekday_cn(date(2024, 1, 1 + wd))}" for wd in sorted(lec.weekdays))
        rows.append(f"{lec.name}={days}")
    return "；".join(rows)


# --- 校验 -----------------------------------------------------------

_TITLE_RE = re.compile(r"^#\s*📒\s*课堂笔记", re.M)
_HEADER_RE = re.compile(r"^>\s*讲师：(.+)$", re.M)
_DATE_IN_HEADER_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[（(]周([一二三四五六日])")
_RISK_RE = re.compile(r"风险指数[^\d]{0,10}(\d{1,3})")


def validate(
    md: str,
    schedule: Schedule,
    *,
    min_length: int = 1500,
    max_length: int = 20000,
    expected_date: date | None = None,
) -> ValidationReport:
    rep = ValidationReport()

    if not _TITLE_RE.search(md):
        rep.errors.append("缺少标题：「# 📒 课堂笔记 · …」")

    m = _HEADER_RE.search(md)
    if not m:
        rep.errors.append("缺少抬头引用块：「> 讲师：…」")
    else:
        head = m.group(1)
        lec = schedule.by_name(head)
        if lec is None:
            rep.errors.append(
                f"抬头的讲师不在排班表内（已知：{'、'.join(schedule.known_names())}）：{head[:40]}"
            )
        dm = _DATE_IN_HEADER_RE.search(head)
        if not dm:
            rep.errors.append("抬头日期格式不符（应为 YYYY-MM-DD（周X晚））")
        else:
            try:
                d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            except ValueError:
                rep.errors.append(f"抬头日期不是合法日期：{dm.group(0)}")
            else:
                if weekday_cn(d) != dm.group(4):
                    rep.errors.append(f"抬头的日期与星期不符：{dm.group(0)}")
                # 排班与讲师一致性：日期定的讲师应等于抬头写的讲师
                expect = schedule.for_date(d)
                if lec and expect and expect.name != lec.name:
                    rep.errors.append(
                        f"讲师与排班不符：{d.isoformat()}（周{weekday_cn(d)}）"
                        f"应为 {expect.name}，抬头写的是 {lec.name}"
                    )
                if expected_date and d != expected_date:
                    rep.warnings.append(
                        f"抬头日期 {d.isoformat()} 与文件名推断的 {expected_date.isoformat()} 不同"
                        "（若转写内容明示了另一日期，这是正确的）"
                    )

    if "风险提示" not in md:
        rep.errors.append("缺少「风险提示」小节")
    rm = _RISK_RE.search(md)
    if not rm:
        rep.errors.append("缺少风险指数数字")
    else:
        score = int(rm.group(1))
        if not 0 <= score <= 100:
            rep.errors.append(f"风险指数 {score} 超出 0–100")

    if "非投资建议" not in md:
        rep.errors.append("缺少文末「非投资建议」备注")

    n = len(md)
    if n < min_length:
        rep.warnings.append(f"篇幅偏短：{n} 字（< {min_length}），建议人工复核")
    elif n > max_length:
        rep.warnings.append(f"篇幅偏长：{n} 字（> {max_length}），建议人工复核")

    return rep


# --- 生成 -----------------------------------------------------------

class NoteGenError(Exception):
    """模型调用或校验连续失败。"""


def log(msg: str) -> None:
    print(f"[notes] {msg}", flush=True)


def _strip_fence(md: str) -> str:
    md = re.sub(r"^```(?:markdown|md)?\s*\n", "", md)
    md = re.sub(r"\n```\s*$", "", md)
    return md.strip()


def _call(client, model: str, prompt: PromptPair, cfg: dict) -> str:
    """流式调用。长文本非流式会被网关整段缓冲，容易假死。"""
    t0 = time.time()
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        temperature=cfg.get("temperature", 0.3),
        max_tokens=cfg.get("max_tokens", 30000),
        stream=True,
    )
    chunks: list[str] = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            chunks.append(delta.content)
        total = sum(len(c) for c in chunks)
        if total and total % 3000 < 40:
            log(f"  生成中 {total} 字 / {time.time()-t0:.0f}s")
    md = "".join(chunks).strip()
    log(f"模型返回 {len(md)} 字（{time.time()-t0:.0f}s）")
    return md


def generate(
    transcript: str,
    meta: VideoMeta,
    out_md: Path,
    rules: str,
    schedule: Schedule,
    llm_cfg: dict,
    notes_cfg: dict,
    extras: list[tuple[Path, str]] | None = None,
) -> ValidationReport:
    """生成并落盘笔记。返回最终校验报告（warnings 可能非空）。"""
    api_key_env = llm_cfg.get("api_key_env", "MOONSHOT_API_KEY")

    if llm_cfg.get("dry_run"):
        # 无密钥也能跑通全链路（用于开发与 CI）：用转写稿拼一份占位笔记
        out_md.parent.mkdir(parents=True, exist_ok=True)
        stub = _dry_run_note(meta, schedule, transcript)
        out_md.write_text(stub, encoding="utf-8")
        log("dry_run：已写入占位笔记")
        return validate(stub, schedule, min_length=0)

    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise NoteGenError(f"缺少环境变量 {api_key_env}")

    try:
        from openai import OpenAI
    except ImportError as e:
        raise NoteGenError("缺少依赖 openai：pip install openai") from e

    client = OpenAI(
        api_key=api_key,
        base_url=llm_cfg.get("base_url", "https://api.moonshot.cn/v1"),
        timeout=llm_cfg.get("timeout", 600),
    )
    model = llm_cfg.get("model", "kimi-k3")
    prompt = build_prompt(meta, transcript, rules, schedule, extras)

    log(f"输入：转写 {len(transcript)} 字，附件 {len(extras or [])} 份，模型 {model}")
    md = _strip_fence(_call(client, model, prompt, llm_cfg))
    rep = validate(md, schedule, expected_date=meta.recorded_on)

    retries = int(llm_cfg.get("max_retries", 1))
    for attempt in range(1, retries + 1):
        if rep.ok:
            break
        log(f"校验未过（{len(rep.errors)} 项），第 {attempt} 次定向重试：{'; '.join(rep.errors[:3])}")
        retry_prompt = PromptPair(
            system=prompt.system,
            user=prompt.user + (
                "\n\n【上次输出未通过校验，请逐条修正后重新输出完整笔记】\n"
                + rep.as_feedback()
            ),
        )
        candidate = _strip_fence(_call(client, model, retry_prompt, llm_cfg))
        cand_rep = validate(candidate, schedule, expected_date=meta.recorded_on)
        if len(cand_rep.errors) < len(rep.errors):
            md, rep = candidate, cand_rep

    if not rep.ok:
        raise NoteGenError("笔记校验未通过：" + "; ".join(rep.errors))

    out_md.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_md.with_suffix(out_md.suffix + ".tmp")
    tmp.write_text(md + "\n", encoding="utf-8")
    os.replace(tmp, out_md)
    log(f"笔记已写出：{out_md.name}（{len(md)} 字）")
    for w in rep.warnings:
        log(f"  提示：{w}")
    return rep


def _dry_run_note(meta: VideoMeta, schedule: Schedule, transcript: str) -> str:
    lec = schedule.for_date(meta.recorded_on)
    name = lec.name if lec else "未确定"
    rid = f"（投顾编号：{lec.registration_id}）" if lec and lec.registration_id else ""
    excerpt = transcript.strip().splitlines()[:3]
    return (
        f"# 📒 课堂笔记 · {meta.title}\n"
        f"> 讲师：{name}{rid}｜ 日期：{meta.recorded_on.isoformat()}"
        f"（周{weekday_cn(meta.recorded_on)}晚）｜ 场次：{schedule.session_label}\n\n"
        f"**这是一份 dry_run 占位笔记，用于在没有模型密钥时打通流程。**\n\n"
        f"## ⚠️ 风险提示（本场前置）\n风险指数 50/100（中风险，仅供流程验证）\n\n"
        f"## 转写摘录\n" + "\n".join(excerpt) + "\n\n"
        f"*备注：本笔记为直播观点整理，非投资建议。*\n"
    )
