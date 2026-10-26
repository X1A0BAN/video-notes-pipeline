# -*- coding: utf-8 -*-
"""排班表的测试。

这里有一条专门的回归测试：旧的 livestream-agent 把排班写成
{0,2,3,6}（漏了周二 1），而规则文档写的是「周日、周二、周四」，
两者不一致且无人发现。下面 test_all_documented_weekdays_are_covered
就是守着这个坑。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vnotes.schedule import Schedule, ScheduleError, weekday_cn  # noqa: E402

CONFIG = Path(__file__).resolve().parents[1] / "config" / "schedule.json"


@pytest.fixture
def sched() -> Schedule:
    return Schedule.load(CONFIG)


def test_loads_shipped_schedule(sched):
    assert len(sched.lecturers) == 2
    names = sched.known_names()
    assert "张明" in names and "李伟" in names


def test_all_documented_weekdays_are_covered(sched):
    """回归：张明=周日/周二/周四，李伟=周一/周三。"""
    expected = {
        "张明": {6, 1, 3},
        "李伟": {0, 2},
    }
    actual = {lec.name: set(lec.weekdays) for lec in sched.lecturers}
    assert actual == expected


def test_tuesday_is_assigned(sched):
    """旧实现漏了周二——单独钉住这一格。"""
    tue = date(2026, 9, 1)
    assert tue.weekday() == 1
    lec = sched.for_date(tue)
    assert lec is not None, "周二没有排任何讲师（旧 livestream-agent 的缺陷）"
    assert lec.name == "张明"


@pytest.mark.parametrize("day,expected_name", [
    (date(2026, 8, 30), "张明"),   # 周日
    (date(2026, 8, 31), "李伟"),  # 周一
    (date(2026, 9, 1), "张明"),    # 周二
    (date(2026, 9, 2), "李伟"),  # 周三
    (date(2026, 9, 3), "张明"),    # 周四
    (date(2026, 9, 5), None),     # 周六：不排
])
def test_for_date(sched, day, expected_name):
    lec = sched.for_date(day)
    assert (lec.name if lec else None) == expected_name


def test_holiday_overrides_schedule(sched):
    holiday = date(2026, 10, 1)
    s = Schedule(lecturers=sched.lecturers, holidays=frozenset({holiday}))
    assert s.for_date(holiday) is None


def test_conflicting_assignment_raises(tmp_path):
    raw = {
        "lecturers": [
            {"name": "甲", "weekdays": [1, 2]},
            {"name": "乙", "weekdays": [2, 3]},
        ]
    }
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ScheduleError, match="同时排给"):
        Schedule.load(path)


def test_invalid_weekday_raises(tmp_path):
    raw = {"lecturers": [{"name": "甲", "weekdays": [7]}]}
    path = tmp_path / "bad_wd.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ScheduleError, match="非法"):
        Schedule.load(path)


def test_missing_schedule_file(tmp_path):
    with pytest.raises(ScheduleError, match="不存在"):
        Schedule.load(tmp_path / "nope.json")


def test_empty_lecturers_rejected(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text('{"lecturers": []}', encoding="utf-8")
    with pytest.raises(ScheduleError, match="没有任何讲师"):
        Schedule.load(path)


def test_by_name_and_aliases(sched):
    assert sched.by_name("> 讲师：张明（投顾编号：A113）").name == "张明"
    assert sched.by_name("讲师：张老师").name == "张明"
    assert sched.by_name("完全无关的文本") is None


def test_weekday_cn():
    assert weekday_cn(date(2026, 10, 26)) == "一"  # 2026-10-26 是周一
