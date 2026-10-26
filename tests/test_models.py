# -*- coding: utf-8 -*-
"""VideoMeta 解析、发现、幂等键与状态机。"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vnotes.models import Session, VideoMeta, discover, workdir_for  # noqa: E402


def _touch(path: Path, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


# --- 文件名解析 -----------------------------------------------------

@pytest.mark.parametrize("name,expect_date,expect_title", [
    ("2026-10-12-19-30_行情复盘.mp4", date(2026, 10, 12), "行情复盘"),
    ("2026-10-12_行情复盘.mp4", date(2026, 10, 12), "行情复盘"),
    ("2026-09-15-20-00 市场解读.MP4", date(2026, 9, 15), "市场解读"),
    ("2026.9.13 复盘.mkv", date(2026, 9, 13), "复盘"),
    ("2026年9月2日 课堂.ts", date(2026, 9, 2), "课堂"),
])
def test_parse_filename(tmp_path, name, expect_date, expect_title):
    p = _touch(tmp_path / name)
    meta = VideoMeta.parse(p)
    assert meta.recorded_on == expect_date
    assert meta.title == expect_title


def test_parse_filename_with_time(tmp_path):
    p = _touch(tmp_path / "2026-10-12-19-30_行情.mp4")
    meta = VideoMeta.parse(p)
    assert meta.recorded_at is not None
    assert meta.recorded_at.hour == 19 and meta.recorded_at.minute == 30


def test_parse_falls_back_to_mtime(tmp_path):
    p = _touch(tmp_path / "没有日期的视频.mp4")
    meta = VideoMeta.parse(p)
    assert meta.title == "没有日期的视频"
    assert meta.recorded_on == date.fromtimestamp(p.stat().st_mtime)


def test_parse_invalid_date_falls_back(tmp_path):
    """2026-13-45 是假日期，不该崩，应退回 mtime。"""
    p = _touch(tmp_path / "2026-13-45_假日期.mp4")
    meta = VideoMeta.parse(p)
    assert meta.recorded_on == date.fromtimestamp(p.stat().st_mtime)


def test_weekday(tmp_path):
    p = _touch(tmp_path / "2026-09-01_周二.mp4")
    assert VideoMeta.parse(p).weekday == 1


# --- 发现 -----------------------------------------------------------

def test_discover_filters_and_sorts(tmp_path):
    _touch(tmp_path / "2026-10-13_b.mp4")
    _touch(tmp_path / "2026-10-12_a.mp4")
    _touch(tmp_path / "2026-10-12_c.mkv")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    _touch(tmp_path / ".hidden.mp4")

    metas = discover(tmp_path)
    assert [m.recorded_on for m in metas] == [date(2026, 10, 12), date(2026, 10, 12),
                                              date(2026, 10, 13)]
    assert all(m.path.suffix.lower() in (".mp4", ".mkv") for m in metas)
    assert not any(m.path.name.startswith(".") for m in metas)


def test_discover_missing_dir():
    with pytest.raises(FileNotFoundError):
        discover(Path("/definitely/not/here"))


def test_discover_lookback(tmp_path):
    _touch(tmp_path / "2020-01-01_old.mp4")
    today = date.today().isoformat()
    _touch(tmp_path / f"{today}_new.mp4")
    metas = discover(tmp_path, lookback_days=3)
    assert len(metas) == 1
    assert metas[0].title == "new"


def test_workdir_layout(tmp_path):
    meta = VideoMeta(path=tmp_path / "x.mp4", title="x", recorded_on=date(2026, 1, 5))
    assert workdir_for(meta, tmp_path / "out") == tmp_path / "out" / "26.1.5"


# --- 状态机 ---------------------------------------------------------

def _session(tmp_path) -> Session:
    video = _touch(tmp_path / "in" / "2026-10-12_课.mp4", size=32)
    meta = VideoMeta.parse(video)
    return Session(meta, tmp_path / "out" / "26.10.12")


def test_session_initial_state(tmp_path):
    s = _session(tmp_path)
    assert s.stage == "discovered"
    assert s.stage_index() == 0
    assert not s.is_done()
    assert s.note_md.name == "课.课堂笔记.md"


def test_state_persists_and_reloads(tmp_path):
    s = _session(tmp_path)
    s.advance("transcribed")
    s2 = Session(s.meta, s.workdir)
    assert s2.stage == "transcribed"


def test_failure_keeps_progress(tmp_path):
    """回归：旧实现把 status 改成 failed_asr，续跑会丢掉 transcript 阶段的进度。"""
    s = _session(tmp_path)
    s.advance("transcribed")
    s.fail("notes", "模型超时")
    assert s.stage == "transcribed", "失败后阶段被回退了"
    assert s.state["failed_step"] == "notes"
    assert "模型超时" in s.state["last_error"]

    s2 = Session(s.meta, s.workdir)
    assert s2.stage == "transcribed"
    assert s2.stage_index() == 1


def test_advance_clears_failure(tmp_path):
    s = _session(tmp_path)
    s.fail("asr", "boom")
    s.advance("transcribed")
    assert "failed_step" not in s.state
    assert "last_error" not in s.state


def test_source_change_resets_state(tmp_path):
    s = _session(tmp_path)
    s.advance("noted")
    # 换掉源视频（大小变了）
    time.sleep(0.01)
    s.meta.path.write_bytes(b"\1" * 999)
    s2 = Session(s.meta, s.workdir)
    assert s2.stage == "discovered"


def test_extra_materials_excludes_own_outputs(tmp_path):
    s = _session(tmp_path)
    src_dir = s.meta.path.parent
    (src_dir / "盘后笔记.txt").write_text("材料", encoding="utf-8")
    (src_dir / "自选股.csv").write_text("a,b", encoding="utf-8")
    (src_dir / "无关.png").write_bytes(b"x")
    s.transcript.write_text("[00.00] hi\n", encoding="utf-8")
    s.note_md.write_text("# 笔记", encoding="utf-8")

    names = {p.name for p in s.extra_materials()}
    assert names == {"盘后笔记.txt", "自选股.csv"}, names


def test_is_done_requires_docx(tmp_path):
    s = _session(tmp_path)
    s.advance("delivered")
    assert not s.is_done(), "没有 docx 时不应算完成"
    s.note_docx.write_bytes(b"PK")
    assert s.is_done()


def test_bump_attempt_counts(tmp_path):
    s = _session(tmp_path)
    assert s.bump_attempt("asr") == 1
    assert s.bump_attempt("asr") == 2
    assert s.bump_attempt("notes") == 1


def test_corrupt_state_file_recovers(tmp_path):
    s = _session(tmp_path)
    s.state_path.write_text("{ not json", encoding="utf-8")
    s2 = Session(s.meta, s.workdir)
    assert s2.stage == "discovered"
