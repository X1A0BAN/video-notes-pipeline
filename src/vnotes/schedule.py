#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""schedule.py — 讲师排班表：由「直播日期是星期几」推「该由谁讲」。

为什么把它做成数据文件而不是代码里的 dict：
    旧实现把 {0,2,3,6: ...} 写死在脚本里，漏了周二（1），
    而规则文档明确写了周日/周二/周四都是同一位讲师——代码与文档不一致，
    且没有任何测试会发现。排班是会变的事（换人、调班、加人），
    放在 JSON 里可以改而不动代码，并用单元测试守着「覆盖完整、无冲突」。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

WEEKDAY_CN = "一二三四五六日"


class ScheduleError(Exception):
    """排班表本身有问题（重复覆盖、非法 weekday 等）。"""


@dataclass(frozen=True)
class Lecturer:
    name: str
    registration_id: str
    weekdays: tuple[int, ...]
    aliases: tuple[str, ...] = ()

    def matches_name(self, text: str) -> bool:
        if self.name in text:
            return True
        return any(a in text for a in self.aliases)


@dataclass
class Schedule:
    lecturers: tuple[Lecturer, ...]
    session_label: str = "直播课堂"
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        seen: dict[int, str] = {}
        for lec in self.lecturers:
            for wd in lec.weekdays:
                if not 0 <= wd <= 6:
                    raise ScheduleError(f"{lec.name} 的 weekday={wd} 非法（应为 0–6）")
                if wd in seen and seen[wd] != lec.name:
                    raise ScheduleError(
                        f"星期{WEEKDAY_CN[wd]} 同时排给了 {seen[wd]} 和 {lec.name}"
                    )
                seen[wd] = lec.name

    def for_date(self, day: date) -> Lecturer | None:
        if day in self.holidays:
            return None
        for lec in self.lecturers:
            if day.weekday() in lec.weekdays:
                return lec
        return None

    def by_name(self, text: str) -> Lecturer | None:
        hits = [lec for lec in self.lecturers if lec.matches_name(text)]
        # 名字可能有前缀重叠，取最长匹配
        return max(hits, key=lambda l: len(l.name)) if hits else None

    def known_names(self) -> tuple[str, ...]:
        return tuple(lec.name for lec in self.lecturers)

    @classmethod
    def load(cls, path: Path) -> "Schedule":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise ScheduleError(f"排班表不存在：{path}") from e
        except json.JSONDecodeError as e:
            raise ScheduleError(f"排班表不是合法 JSON：{e}") from e

        lecturers = []
        for item in raw.get("lecturers", []):
            try:
                lecturers.append(Lecturer(
                    name=item["name"],
                    registration_id=item.get("registration_id", ""),
                    weekdays=tuple(int(w) for w in item.get("weekdays", [])),
                    aliases=tuple(item.get("aliases", [])),
                ))
            except KeyError as e:
                raise ScheduleError(f"排班表条目缺少字段 {e}") from e
        if not lecturers:
            raise ScheduleError(f"排班表 {path} 里没有任何讲师")

        holidays = set()
        for h in raw.get("holidays", []):
            try:
                holidays.add(date.fromisoformat(h))
            except ValueError as e:
                raise ScheduleError(f"节假日日期格式错误：{h}") from e

        return cls(
            lecturers=tuple(lecturers),
            session_label=raw.get("session_label", "直播课堂"),
            holidays=frozenset(holidays),
        )


def weekday_cn(day: date) -> str:
    return WEEKDAY_CN[day.weekday()]
