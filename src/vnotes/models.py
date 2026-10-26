#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""models.py — 场次（Session）与状态机。

一场视频 = 一个 Session。产物落在 output_dir/<YY.M.D>/ 下，命名与源视频同名。

状态机：
    discovered → transcribed → noted → rendered → delivered

每个阶段只做「下一件还没做完的事」，产物存在即视为自洽（幂等）。
状态写 <工作目录>/state.json，先写 .tmp 再 os.replace，保证不会读到半个文件。

与旧实现的关键差异：失败不再把 status 整体改写为 failed_*（那会让续跑时
丢失已完成进度），而是保留 status、单独记录 failed_step 与 last_error。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".ts", ".flv", ".m4v", ".webm", ".wmv", ".mpg", ".mpeg",
}
READABLE_EXT = {".txt", ".md", ".csv"}

STAGES = ["discovered", "transcribed", "noted", "rendered", "delivered"]
_STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}

# 文件名里可选的时间前缀：2026-10-12-19-30_标题.mp4 或 2026-10-12_标题.mp4
_DATE_RE = re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[-_](?P<hh>\d{2})[-:：]?(?P<mm>\d{2}))?")
# 宽松格式：2026.9.13 / 2026年9月2日 / 26.9.2 之外的 20xx 形式
_LOOSE_DATE_RE = re.compile(
    r"(?P<y>20\d{2})\s*[.\-_年]\s*(?P<m>\d{1,2})\s*[.\-_月]\s*(?P<d>\d{1,2})\s*日?")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class VideoMeta:
    """从文件名/文件系统能确定的、不含模型推理的事实。"""

    path: Path
    title: str            # 去掉日期前缀与扩展名的标题
    recorded_on: date     # 推断的录制日期（文件名优先，其次 mtime）
    recorded_at: datetime | None = None

    @property
    def weekday(self) -> int:
        return self.recorded_on.weekday()

    @classmethod
    def parse(cls, path: Path) -> "VideoMeta":
        stem = path.stem
        m = _DATE_RE.search(stem)
        recorded_at: datetime | None = None
        if m:
            try:
                y, mo, d = int(m.group("y")), int(m.group("m")), int(m.group("d"))
                day = date(y, mo, d)
                if m.group("hh"):
                    recorded_at = datetime(y, mo, d, int(m.group("hh")), int(m.group("mm") or 0))
                title = _strip_prefix(stem, m)
                return cls(path=path, title=title or stem, recorded_on=day, recorded_at=recorded_at)
            except ValueError:
                pass  # 2026-13-45 这种假日期，退回 mtime

        lm = _LOOSE_DATE_RE.search(stem)
        if lm:
            try:
                day = date(int(lm.group("y")), int(lm.group("m")), int(lm.group("d")))
                return cls(path=path, title=_strip_prefix(stem, lm) or stem, recorded_on=day)
            except ValueError:
                pass

        mt = datetime.fromtimestamp(path.stat().st_mtime)
        return cls(path=path, title=stem, recorded_on=mt.date(), recorded_at=mt)


def _strip_prefix(stem: str, m: re.Match) -> str:
    tail = stem[m.end():].lstrip("-_ .　")
    return tail.strip()


@dataclass
class Session:
    """一场待处理视频及其全部产物路径。"""

    meta: VideoMeta
    workdir: Path
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workdir / "state.json"
        self.transcript = self.workdir / "transcript.txt"
        self.note_md = self.workdir / f"{self.meta.title}.课堂笔记.md"
        self.note_docx = self.workdir / f"{self.meta.title}.课堂笔记.docx"
        self.runlog = self.workdir / "run.log"
        self._load()

    # --- 状态持久化 -------------------------------------------------
    def _load(self) -> None:
        key = self.idempotency_key()
        state: dict[str, Any] = {}
        if self.state_path.is_file():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        if state.get("key") != key:
            # 源视频被替换（重下/重新导出）→ 旧产物作废，从头来
            if state:
                log(f"{self.meta.path.name}: 源文件已变更，重置状态")
            state = {"key": key, "stage": "discovered", "attempts": {}}
        self.state = state

    def idempotency_key(self) -> dict[str, Any]:
        st = self.meta.path.stat()
        return {
            "video": str(self.meta.path.resolve()),
            "size": st.st_size,
            # mtime 取整到秒；不同文件系统的精度差异不该导致无谓重跑
            "mtime": int(st.st_mtime),
        }

    def save(self) -> None:
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def bump_attempt(self, step: str) -> int:
        attempts = self.state.setdefault("attempts", {})
        attempts[step] = attempts.get(step, 0) + 1
        self.save()
        return attempts[step]

    def advance(self, stage: str) -> None:
        self.state["stage"] = stage
        self.state.pop("failed_step", None)
        self.state.pop("last_error", None)
        self.save()

    def fail(self, step: str, error: str) -> None:
        """记录失败但**不回退**进度，续跑时从同一阶段继续。"""
        self.state["failed_step"] = step
        self.state["last_error"] = error[:800]
        self.save()

    # --- 阶段判断 ---------------------------------------------------
    @property
    def stage(self) -> str:
        return self.state.get("stage", "discovered")

    def stage_index(self) -> int:
        return _STAGE_INDEX.get(self.stage, 0)

    def is_done(self) -> bool:
        return self.stage == "delivered" and self.note_docx.exists()

    def extra_materials(self) -> list[Path]:
        """同目录下的附加材料（讲师盘后笔记、新闻、自选股清单…）。

        只取源视频所在的目录，且排除本流水线自己的产物——否则重跑一次
        就会把自己的笔记当成新材料喂回模型，越滚越偏。
        """
        src_dir = self.meta.path.parent
        own = {self.transcript.resolve(), self.note_md.resolve(),
               self.note_docx.resolve(), self.runlog.resolve()}
        out: list[Path] = []
        try:
            entries = sorted(src_dir.iterdir())
        except OSError:
            return []
        for p in entries:
            if not p.is_file() or p.suffix.lower() not in READABLE_EXT:
                continue
            if p.resolve() in own or p.name.startswith(("state", "run.log")):
                continue
            out.append(p)
        return out

    def log_run(self, message: str) -> None:
        with self.runlog.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def discover(video_dir: Path, lookback_days: int | None = None) -> list[VideoMeta]:
    """扫描目录里的视频，按录制时间升序返回。"""
    if not video_dir.is_dir():
        raise FileNotFoundError(f"视频目录不存在：{video_dir}")

    metas: list[VideoMeta] = []
    for p in sorted(video_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
            continue
        if p.name.startswith("."):
            continue
        metas.append(VideoMeta.parse(p))

    metas.sort(key=lambda m: (m.recorded_on, m.path.name))
    if lookback_days is not None:
        cutoff = date.today()
        metas = [m for m in metas if (cutoff - m.recorded_on).days <= lookback_days]
    return metas


def workdir_for(meta: VideoMeta, output_root: Path) -> Path:
    """按日期分目录：output/26.10.12/。多场同一天也共用目录。"""
    d = meta.recorded_on
    return output_root / f"{d.year % 100}.{d.month}.{d.day}"


def short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]
