# -*- coding: utf-8 -*-
"""摘要抽取是纯函数，重点测它——推送本身靠 mock。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vnotes.notifier import (  # noqa: E402
    MAX_MARKDOWN_BYTES,
    NotifyError,
    WeComNotifier,
    build_summary,
)

NOTE = """# 📒 课堂笔记 · 震荡市

> 讲师：张明（投顾编号：A0000000000001）｜ 日期：2026-09-01（周二晚）｜ 场次：直播课堂

**主线仍是量能修复，不改结构性机会。**

## ⚠️ 风险提示（本场前置）

风险指数 45/100（中风险，谨慎做多）

- 3100 一线破位则降仓
- 半导体拥挤度偏高

## 次日预案

1. 关注量能能否延续
2. 核心票分歧日低吸
3. 不追加速票
4. 第四条应被截断

*备注：本笔记为直播观点整理，非投资建议。*
"""


def test_summary_has_header():
    s = build_summary(NOTE)
    assert s.startswith("## 📒 张明")


def test_summary_band_captures_nested_detail():
    """回归：括号内的说明必须完整带出，不能只留「（中风险）」。"""
    s = build_summary(NOTE)
    assert "（中风险，谨慎做多）" in s


def test_summary_band_brackets_balanced():
    """任何输入下都不该出现「)）」这种半截括号。"""
    for md in (
        "风险指数 45/100（中风险，谨慎做多，控仓低吸）",
        "风险指数 80/100（高风险）",
        "风险指数 20/100 (低风险, 可积极)",
        "风险指数 50/100 中风险",
    ):
        s = build_summary(md)
        assert s.count("（") == s.count("）"), s


def test_summary_footer_always_last():
    """回归：超长摘要被截断后，footer 不能被卷进正文中间。"""
    md = "> 讲师：张明\n\n## 次日预案\n" + "\n".join(
        f"- 第 {i} 条要点" * 10 for i in range(80))
    s = build_summary(md)
    assert s.endswith("完整 docx 见下方文件消息。")


def test_summary_has_risk_index():
    s = build_summary(NOTE)
    assert "风险指数：45/100" in s
    assert "中风险" in s


def test_summary_has_gist():
    s = build_summary(NOTE)
    assert "一句话主旨" in s
    assert "量能修复" in s


def test_summary_plans_limited_to_three():
    s = build_summary(NOTE)
    assert "关注量能" in s
    assert "分歧日低吸" in s
    assert "不追加速票" in s
    assert "第四条应被截断" not in s


def test_summary_includes_top_risk():
    s = build_summary(NOTE)
    assert "3100" in s


def test_summary_skips_disclaimer_as_gist():
    """文末 *备注：… 非投资建议* 不该被当成一句话主旨。"""
    md = "> 讲师：张明\n\n*备注：本笔记为直播观点整理，非投资建议。*\n"
    s = build_summary(md)
    assert "一句话主旨" not in s


def test_summary_without_header_or_risk():
    s = build_summary("随便一段没有结构的文字")
    assert s.startswith("## 📒 课堂笔记")
    assert "风险指数：?/100" in s


def test_summary_truncated_to_byte_limit():
    md = "> 讲师：张明\n\n**主旨**\n\n## 次日预案\n" + "\n".join(
        f"- 第 {i} 条要点，内容比较长" * 20 for i in range(50))
    s = build_summary(md)
    assert len(s.encode("utf-8")) <= MAX_MARKDOWN_BYTES
    assert s.endswith("完整 docx 见下方文件消息。")


def test_truncation_never_splits_multibyte():
    from vnotes.notifier import _truncate_utf8
    s = "中文" * 1000
    out = _truncate_utf8(s, 100)
    assert len(out.encode("utf-8")) <= 100
    out.encode("utf-8").decode("utf-8")  # 不抛异常 = 没切断汉字


def test_webhook_without_key_rejected():
    with pytest.raises(NotifyError, match="key"):
        WeComNotifier("https://qyapi.weixin.qq.com/cgi-bin/webhook/send")


def test_webhook_key_parsed():
    n = WeComNotifier("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123")
    assert n.key == "abc-123"


def test_post_raises_on_errcode(monkeypatch):
    n = WeComNotifier("https://x/send?key=k")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"errcode": 93000, "errmsg": "invalid webhook url"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: FakeResp())
    with pytest.raises(NotifyError, match="93000"):
        n._post("https://x", {})


def test_retry_then_success(monkeypatch):
    n = WeComNotifier("https://x/send?key=k")
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise NotifyError("临时失败")
        return {"errcode": 0}

    monkeypatch.setattr("time.sleep", lambda s: None)
    out = n._with_retry(flaky, "测试")
    assert out == {"errcode": 0}
    assert calls["n"] == 3


def test_retry_exhausted_raises(monkeypatch):
    n = WeComNotifier("https://x/send?key=k")

    def always_fail(*a, **kw):
        raise NotifyError("一直失败")

    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(NotifyError, match="已重试"):
        n._with_retry(always_fail, "测试")
