# -*- coding: utf-8 -*-
"""markdown → docx 转换。需要 python-docx。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("docx")

from vnotes.md2docx import convert  # noqa: E402

SAMPLE = """# 📒 课堂笔记 · 测试

> 讲师：张明｜ 日期：2026-09-01（周二晚）｜ 场次：直播课堂

**一句话主旨。**

## ⚠️ 风险提示（本场前置）

风险指数 45/100（中风险）

- 第一条
- 第二条，**加粗**与*斜体*

## 板块

| 板块 | 判断 |
|---|---|
| 半导体 | 主线 |
| 小盘 | 情绪 |

## 次日预案

1. 关注量能
2. 分歧低吸

---

*备注：本笔记为直播观点整理，非投资建议。*
"""


@pytest.fixture
def docx_path(tmp_path) -> Path:
    src = tmp_path / "note.md"
    src.write_text(SAMPLE, encoding="utf-8")
    return convert(src, tmp_path / "note.docx")


def test_creates_file(docx_path):
    assert docx_path.is_file()
    assert docx_path.stat().st_size > 1000


def test_paragraph_content(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    texts = [p.text for p in doc.paragraphs]
    assert any("课堂笔记" in t for t in texts)
    assert any("讲师：张明" in t for t in texts)
    assert any("风险指数 45/100" in t for t in texts)


def test_table_rendered(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert len(t.rows) == 3  # 表头 + 2 行
    assert len(t.columns) == 2
    assert t.rows[0].cells[0].text == "板块"
    assert t.rows[1].cells[0].text == "半导体"


def test_separator_not_leaked_as_table_row(docx_path):
    """回归：旧实现会把 |---|---| 当成数据行塞进表格。"""
    from docx import Document
    doc = Document(str(docx_path))
    cells = [c.text for row in doc.tables[0].rows for c in row.cells]
    assert not any("---" in c for c in cells)


def test_inline_bold_applied(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    bold_runs = [
        r.text
        for p in doc.paragraphs for r in p.runs if r.bold and r.text.strip()
    ]
    assert any("加粗" in t for t in bold_runs)


def test_heading_levels(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    sizes = {round(r.font.size.pt, 1) for p in doc.paragraphs for r in p.runs if r.font.size}
    assert 20.0 in sizes   # H1
    assert 15.0 in sizes   # H2


def test_ordered_list_numbering(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    texts = [p.text for p in doc.paragraphs]
    assert any(t.startswith("1. 关注量能") for t in texts)
    assert any(t.startswith("2. 分歧低吸") for t in texts)


def test_emoji_split_into_own_run(docx_path):
    """emoji 必须单独成 run 并单独给字体。

    回归：混在同一个 run 里时，缺 emoji 字体的渲染器会用一个宽方块顶替，
    从而挤掉紧邻的汉字（实测「📒 课堂笔记」显示成「谨笔记」）。
    """
    from docx import Document
    doc = Document(str(docx_path))
    first = doc.paragraphs[0]
    emoji_runs = [r for r in first.runs if "📒" in r.text]
    assert emoji_runs, "标题里的 emoji 没有单独成 run"
    for r in emoji_runs:
        assert "课堂笔记" not in r.text, "emoji 与中文混在同一个 run 里"
        assert r.font.name == "Segoe UI Emoji"


def test_cjk_text_kept_in_main_run(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    first = doc.paragraphs[0]
    cjk = [r for r in first.runs if "课堂笔记" in r.text]
    assert cjk, "标题中文应保留在自己的 run 里"
    for r in cjk:
        assert "📒" not in r.text


def test_split_emoji_helper():
    from vnotes.md2docx import _split_emoji

    assert _split_emoji("📒 课堂笔记") == [("📒", True), (" 课堂笔记", False)]
    assert _split_emoji("纯中文") == [("纯中文", False)]
    assert _split_emoji("") == []
    # ⭐ 与 ⚠️（含变体选择符）都算 emoji
    parts = _split_emoji("⭐ 量能")
    assert parts[0][1] is True and "⭐" in parts[0][0]


def test_warning_emoji_in_heading(docx_path):
    from docx import Document
    doc = Document(str(docx_path))
    for p in doc.paragraphs:
        if "风险提示" in p.text:
            assert any("⚠" in r.text for r in p.runs)
            assert not any("⚠" in r.text and "风险" in r.text for r in p.runs)
            break
    else:
        pytest.fail("没找到风险提示小节")


def test_table_emoji_does_not_break_table(docx_path):
    """表格里的 ⭐ 不该打乱列结构。"""
    from docx import Document
    doc = Document(str(docx_path))
    for t in doc.tables:
        for row in t.rows:
            assert len(row.cells) == len(t.columns)


def test_missing_dependency_message(tmp_path, monkeypatch):
    """python-docx 缺失时应给出可照做的提示，而不是 ImportError 堆栈。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("docx"):
            raise ImportError("no docx")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    src = tmp_path / "x.md"
    src.write_text("# t", encoding="utf-8")
    with pytest.raises(RuntimeError, match="python-docx"):
        convert(src, tmp_path / "x.docx")
