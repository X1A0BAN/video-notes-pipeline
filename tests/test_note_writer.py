# -*- coding: utf-8 -*-
"""提示词组装与输出校验——不调网络，纯字符串逻辑。"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vnotes.models import VideoMeta  # noqa: E402
from vnotes.note_writer import build_prompt, validate  # noqa: E402
from vnotes.schedule import Schedule  # noqa: E402

CONFIG = Path(__file__).resolve().parents[1] / "config" / "schedule.json"


@pytest.fixture
def sched() -> Schedule:
    return Schedule.load(CONFIG)


@pytest.fixture
def meta(tmp_path) -> VideoMeta:
    return VideoMeta(path=tmp_path / "2026-09-01_课.mp4", title="课",
                     recorded_on=date(2026, 9, 1))


GOOD_NOTE = """# 📒 课堂笔记 · 震荡市的结构机会

> 讲师：张明（投顾编号：A0000000000001）｜ 日期：2026-09-01（周二晚）｜ 场次：直播课堂

**主线仍是量能修复，指数震荡但不改结构性机会。**

## ⚠️ 风险提示（本场前置）

风险指数 45/100（中风险，谨慎做多，控仓低吸）

- 上证 3100 一线若不破可低吸，破则降仓
- 半导体设备分支拥挤度偏高

## 大框架

- ⭐ 成交额回到 9000 亿才算健康
- 权重与题材轮动，不宜追高

## 板块梳理

| 板块 | 判断 | 看点 |
|---|---|---|
| 半导体设备 | 主线 | 分歧中能扛 |
| 低位低价小盘 | 情绪线 | 只做龙头 |

## 个股与操作

- 标的名称系语音转写谐音，请自行核对代码

## 次日预案

1. 关注量能能否延续
2. 核心票分歧日低吸

## 操作纪律

- 不追加速票

*备注：本笔记为直播观点整理，非投资建议。*
"""


def test_build_prompt_includes_rules_and_transcript(meta, sched, tmp_path):
    pair = build_prompt(meta, "【转写稿】今天讲的是量能", "规则A\n规则B", sched)
    assert "规则A" in pair.system and "规则B" in pair.system
    assert "今天讲的是量能" in pair.user
    assert sched.session_label in pair.system


def test_build_prompt_derives_lecturer_from_schedule(meta, sched):
    """2026-09-01 是周二，排班应给出张明。"""
    pair = build_prompt(meta, "x", "r", sched)
    assert "张明" in pair.user
    assert date(2026, 9, 1).isoformat() in pair.user


def test_build_prompt_no_scheduled_lecturer(tmp_path, sched):
    m = VideoMeta(path=tmp_path / "2026-09-05.mp4", title="x",
                  recorded_on=date(2026, 9, 5))  # 周六
    pair = build_prompt(m, "x", "r", sched)
    assert "不在固定排班内" in pair.user


def test_build_prompt_includes_extras(meta, sched, tmp_path):
    f = tmp_path / "盘后笔记.txt"
    pair = build_prompt(meta, "x", "r", sched, extras=[(f, "原文摘录")])
    assert "盘后笔记.txt" in pair.user
    assert "原文摘录" in pair.user


def test_build_prompt_without_extras(meta, sched):
    pair = build_prompt(meta, "x", "r", sched)
    assert "无" in pair.user.split("【附加材料】")[1]


# --- 校验 -----------------------------------------------------------

def test_validate_accepts_good_note(sched):
    rep = validate(GOOD_NOTE, sched, min_length=100)
    assert rep.ok, rep.errors


def test_validate_flags_missing_title(sched):
    rep = validate(GOOD_NOTE.replace("# 📒 课堂笔记", "# 笔记"), sched, min_length=100)
    assert any("标题" in e for e in rep.errors)


def test_validate_flags_missing_header(sched):
    rep = validate(GOOD_NOTE.replace("> 讲师：", "讲师："), sched, min_length=100)
    assert any("抬头" in e for e in rep.errors)


def test_validate_rejects_unknown_lecturer(sched):
    md = GOOD_NOTE.replace("讲师：张明", "讲师：张三")
    rep = validate(md, sched, min_length=100)
    assert any("不在排班表内" in e for e in rep.errors)


def test_validate_rejects_weekday_mismatch(sched):
    md = GOOD_NOTE.replace("2026-09-01（周二晚）", "2026-09-01（周三晚）")
    rep = validate(md, sched, min_length=100)
    assert any("日期与星期不符" in e for e in rep.errors)


def test_validate_checks_lecturer_against_schedule(sched):
    """2026-09-01 周二应为张明；写成李伟必须报错。"""
    md = GOOD_NOTE.replace("讲师：张明（投顾编号：A0000000000001）",
                           "讲师：李伟（投顾编号：A0000000000002）")
    rep = validate(md, sched, min_length=100)
    assert any("讲师与排班不符" in e for e in rep.errors)


def test_validate_flags_missing_risk_section(sched):
    md = GOOD_NOTE.replace("## ⚠️ 风险提示（本场前置）", "## 风险")
    rep = validate(md, sched, min_length=100)
    assert any("风险提示" in e for e in rep.errors)


def test_validate_flags_missing_risk_index(sched):
    md = GOOD_NOTE.replace("风险指数 45/100", "风险情况见正文")
    rep = validate(md, sched, min_length=100)
    assert any("风险指数" in e for e in rep.errors)


def test_validate_flags_out_of_range_index(sched):
    md = GOOD_NOTE.replace("风险指数 45/100", "风险指数 195/100")
    rep = validate(md, sched, min_length=100)
    assert any("超出" in e for e in rep.errors)


def test_validate_flags_missing_disclaimer(sched):
    md = GOOD_NOTE.replace("非投资建议", "仅供参考")
    rep = validate(md, sched, min_length=100)
    assert any("非投资建议" in e for e in rep.errors)


def test_validate_length_is_warning_not_error(sched):
    rep = validate(GOOD_NOTE, sched, min_length=100000)
    assert rep.ok
    assert any("篇幅偏短" in w for w in rep.warnings)


def test_validate_warns_on_date_mismatch_only(sched):
    rep = validate(GOOD_NOTE, sched, min_length=100, expected_date=date(2026, 9, 2))
    assert rep.ok
    assert any("与文件名推断" in w for w in rep.warnings)


def test_feedback_lists_errors(sched):
    rep = validate("空", sched, min_length=1)
    assert "缺少" in rep.as_feedback()
    assert rep.as_feedback().startswith("- ")
