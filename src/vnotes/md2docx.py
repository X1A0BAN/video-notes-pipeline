#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""md2docx.py — 课堂笔记 markdown → 排版好的 docx。

只支持笔记实际会用到的那一小撮 markdown（标题/列表/表格/引用/分割线/粗斜体）。
刻意不引入通用 markdown 库：通用库会把样式交给默认样式表，
这套版式（红底表头、红色序号、灰斜体引用）需要逐元素控制。

样式规范：
    中文微软雅黑 / 西文 Calibri；正文 11pt
    一级标题 20pt 居中加粗主题红，二级 15pt 红，三级 12.5pt 加粗
    表格红底白字表头 + Table Grid
    引用块灰色斜体缩进；--- 转红色下边框分割线
"""
from __future__ import annotations

import re
from pathlib import Path

EAST_FONT = "微软雅黑"
LATIN_FONT = "Calibri"
# emoji（📒 ⚠️ ⭐）不属于中文字体也不属于西文字体。Word 缺省会用替代字体渲染，
# 但在 Linux/LibreOffice 下常常变成一个宽度异常的方块，把后面的文字挤走。
# 显式指定顺序后，有 emoji 字体的机器正常显示，没有的按顺序降级。
EMOJI_FONTS = "Segoe UI Emoji,Apple Color Emoji,Noto Color Emoji,Segoe UI Symbol"

# 转义干净利落的常量
COLOR_DARK = (0x1F, 0x1F, 0x1F)
COLOR_ACCENT = (0xC0, 0x39, 0x2B)
COLOR_GRAY = (0x59, 0x59, 0x59)
COLOR_WHITE = (0xFF, 0xFF, 0xFF)

_TABLE_SEP_RE = re.compile(r"^:?-{2,}:?$")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\d{1,3})[.)、]\s*(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*([^*\n]+?)\*")


def _rgb(t):
    from docx.shared import RGBColor
    return RGBColor(*t)


def _set_run(run, size=11.0, bold=False, color=COLOR_DARK, italic=False):
    from docx.oxml.ns import qn
    from docx.shared import Pt
    run.font.name = LATIN_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = _rgb(color)
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        from docx.oxml import OxmlElement
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), EAST_FONT)
    fonts.set(qn("w:hint"), "eastAsia")
    # 给 emoji 一个明确的降级链；cs（复杂文种）位也一并设上，
    # 免得某些渲染器拿它来画符号。
    fonts.set(qn("w:cs"), EMOJI_FONTS)


def _is_emoji(ch: str) -> bool:
    """判断是否 emoji / 符号区码位。

    这些字符不会被中文字体或西文字体覆盖，必须单独给字体。
    """
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF      # 各类 emoji（📒 ✅ 🎯）
        or 0x2600 <= cp <= 0x27BF      # 杂项符号、装饰符（⚠ ✅ ✳）
        or 0x2B00 <= cp <= 0x2BFF      # 更多符号（⭐ ⬆ ⭕）
        or cp in (0xFE0F, 0x20E3)      # 变体选择符、组合键帽
        or 0x2190 <= cp <= 0x21FF      # 箭头
    )


def _split_emoji(text: str) -> list[tuple[str, bool]]:
    """把文本切成 (片段, 是否 emoji) 的序列，相邻同类合并。"""
    if not text:
        return []
    out: list[tuple[str, bool]] = []
    buf, flag = text[0], _is_emoji(text[0])
    for ch in text[1:]:
        cur = _is_emoji(ch)
        if cur == flag:
            buf += ch
        else:
            out.append((buf, flag))
            buf, flag = ch, cur
    out.append((buf, flag))
    return out


def _add_inline(paragraph, text, size=11.0, bold=False, color=COLOR_DARK, italic=False):
    """解析 **粗体** 与 *斜体*，逐段加 run。

    emoji 单独成 run 并单独指定字体：否则在没有 emoji 字体的环境里，
    一个缺失字形就可能吞掉紧邻的汉字（实测「课堂笔记」被挤成「谨笔记」）。
    """
    for i, chunk in enumerate(_BOLD_RE.split(text)):
        if not chunk:
            continue
        chunk_bold = bold or (i % 2 == 1)
        for j, sub in enumerate(_ITALIC_RE.split(chunk)):
            if not sub:
                continue
            sub_italic = italic or (j % 2 == 1)
            for piece, is_emoji in _split_emoji(sub):
                run = paragraph.add_run(piece)
                if is_emoji:
                    _set_emoji_run(run, size=size, bold=chunk_bold,
                                   color=color, italic=sub_italic)
                else:
                    _set_run(run, size=size, bold=chunk_bold, color=color,
                             italic=sub_italic)


def _set_emoji_run(run, size=11.0, bold=False, color=COLOR_DARK, italic=False):
    """emoji 专用 run：不设 eastAsia，只给 emoji 字体链。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = "Segoe UI Emoji"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = _rgb(color)
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:ascii"), "Segoe UI Emoji")
    fonts.set(qn("w:hAnsi"), "Segoe UI Emoji")
    fonts.set(qn("w:cs"), EMOJI_FONTS)
    # 刻意不设 w:eastAsia——设了反而会让渲染器用中文字体去画 emoji


def _add_hr(doc):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    p = doc.add_paragraph()
    ppr = p._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "C0392B")
    pbdr.append(bottom)
    ppr.append(pbdr)


def _shade(cell, hex_fill):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_fill)
    tcpr.append(shd)


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(_TABLE_SEP_RE.match(c or "-") for c in cells)


def _flush_table(doc, rows: list[str]):
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    header = _split_row(rows[0])
    ncols = len(header)
    table = doc.add_table(rows=1, cols=ncols)
    table.style = "Table Grid"
    table.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for j, text in enumerate(header):
        cell = table.rows[0].cells[j]
        _shade(cell, "C0392B")
        _add_inline(cell.paragraphs[0], text, size=10.5, bold=True, color=COLOR_WHITE)

    for row in rows[1:]:
        cells = _split_row(row)
        new_row = table.add_row().cells
        for j in range(min(len(cells), ncols)):
            _add_inline(new_row[j].paragraphs[0], cells[j], size=10.5)
    doc.add_paragraph()


def convert(src: Path, dst: Path) -> Path:
    """把 src(md) 转成 dst(docx)，返回 dst。"""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as e:
        raise RuntimeError("缺少依赖 python-docx：pip install python-docx") from e

    doc = Document()
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(2.4)
        section.right_margin = Cm(2.4)

    lines = Path(src).read_text(encoding="utf-8").splitlines()
    table_rows: list[str] = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        # 表格先攒着，遇到非表格行再一次性 flush
        if stripped.startswith("|"):
            if _is_separator(stripped):
                continue
            table_rows.append(stripped)
            continue
        if table_rows:
            _flush_table(doc, table_rows)
            table_rows = []

        if not stripped:
            continue

        if stripped in ("---", "***", "___"):
            _add_hr(doc)
            continue

        m = _HEADING_RE.match(stripped)
        if m:
            level, text = len(m.group(1)), m.group(2)
            p = doc.add_paragraph()
            fmt = p.paragraph_format
            if level == 1:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                fmt.space_before, fmt.space_after = Pt(6), Pt(14)
                _add_inline(p, text, size=20, bold=True, color=COLOR_ACCENT)
            elif level == 2:
                fmt.space_before, fmt.space_after = Pt(14), Pt(6)
                _add_inline(p, text, size=15, bold=True, color=COLOR_ACCENT)
            else:
                fmt.space_before, fmt.space_after = Pt(10), Pt(4)
                _add_inline(p, text, size=12.5, bold=True)
            continue

        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            p.paragraph_format.space_after = Pt(4)
            _add_inline(p, stripped.lstrip("> ").strip(), size=10.5,
                        color=COLOR_GRAY, italic=True)
            continue

        m = _BULLET_RE.match(stripped)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(2)
            _add_inline(p, m.group(1))
            continue

        m = _ORDERED_RE.match(stripped)
        if m:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.space_after = Pt(2)
            num = p.add_run(f"{m.group(1)}. ")
            _set_run(num, bold=True, color=COLOR_ACCENT)
            _add_inline(p, m.group(2))
            continue

        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(4)
        _add_inline(p, stripped)

    if table_rows:
        _flush_table(doc, table_rows)

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dst))
    return dst
