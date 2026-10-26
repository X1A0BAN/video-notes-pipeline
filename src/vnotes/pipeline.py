#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py — 编排：扫描 → 转写 → 笔记 → docx → 推送。

每个阶段只做「还没做完的那件事」，产物存在即跳过（幂等）。
失败保留已完成进度，下次从断点继续；单场失败不影响同批其他场次。

退出码：0 全部成功或无事可做；1 有场次失败（便于 cron/systemd 告警）。
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import asr as asr_mod
from . import md2docx
from . import models as M
from . import note_writer
from . import notifier
from .config import Config
from .schedule import Schedule, ScheduleError


@dataclass
class RunStats:
    total: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def note_failure(self, name: str, err: str) -> None:
        self.failed += 1
        self.failures.append((name, err))


class Pipeline:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.schedule = Schedule.load(cfg._p("notes", "schedule_file"))
        self.rules = cfg._p("notes", "rules_file").read_text(encoding="utf-8")
        # init 阶段的配置错误（缺排班表/规则）应立即上抛，而不是等跑到一半
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # --- 单场 -------------------------------------------------------
    def process(self, sess: M.Session) -> str:
        """推进一场到终态，返回最终阶段。失败抛异常。"""
        meta = sess.meta
        if sess.is_done():
            return "skipped"

        stage = sess.stage_index()

        if stage < 1:
            # 转写稿已存在就跳过：单独跑过 `vnotes transcribe` 之后，
            # 直接 run 不该把几十分钟的视频再转一遍。
            if sess.transcript.is_file() and sess.transcript.stat().st_size > 0:
                M.log(f"[{meta.path.name}] 已有转写稿，跳过 ASR")
            else:
                sess.bump_attempt("asr")
                asr_cfg = self.cfg.section("asr")
                asr_mod.transcribe(
                    meta.path, sess.transcript,
                    model_dir=self.cfg._p("asr", "model_dir"),
                    vad_model=self.cfg._p("asr", "vad_model"),
                    threads=asr_cfg["threads"],
                    max_speech_seconds=asr_cfg["max_speech_seconds"],
                    keep_wav=asr_cfg.get("keep_wav", False),
                )
            sess.advance("transcribed")
            stage = 1
            M.log(f"[{meta.path.name}] 转写完成")

        if stage < 2:
            sess.bump_attempt("notes")
            transcript = sess.transcript.read_text(encoding="utf-8")
            extras = self._load_extras(sess)
            note_writer.generate(
                transcript, meta, sess.note_md, self.rules, self.schedule,
                llm_cfg=self.cfg.section("llm"),
                notes_cfg=self.cfg.section("notes"),
                extras=extras,
            )
            sess.advance("noted")
            stage = 2
            M.log(f"[{meta.path.name}] 笔记完成")

        if stage < 3:
            if self.cfg.section("render")["enabled"]:
                sess.bump_attempt("render")
                md2docx.convert(sess.note_md, sess.note_docx)
                M.log(f"[{meta.path.name}] docx 完成")
            sess.advance("rendered")
            stage = 3

        if stage < 4:
            if self.cfg.section("notify")["enabled"]:
                webhook = self.cfg.webhook()
                if not webhook:
                    raise notifier.NotifyError(
                        f"缺少环境变量 {self.cfg.section('notify')['webhook_env']}")
                sess.bump_attempt("notify")
                notifier.push(sess.note_md, sess.note_docx, webhook)
            sess.advance("delivered")

        return sess.stage

    def _load_extras(self, sess: M.Session) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        for p in sess.extra_materials():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError as e:
                M.log(f"附加材料读取失败，跳过：{p.name}（{e}）")
                continue
            out.append((p, text))
        if out:
            M.log(f"[{sess.meta.path.name}] 附加材料 {len(out)} 份：" +
                  "、".join(p.name for p, _ in out))
        return out

    # --- 批量 -------------------------------------------------------
    def run(self, lookback_days: int | None = None, *, dry_run: bool = False) -> RunStats:
        stats = RunStats()
        try:
            metas = M.discover(self.cfg.video_dir, lookback_days=lookback_days)
        except FileNotFoundError as e:
            M.log(str(e))
            stats.note_failure("<scan>", str(e))
            return stats

        stats.total = len(metas)
        M.log(f"发现 {len(metas)} 个视频（{self.cfg.video_dir}）")
        if not metas:
            return stats

        for meta in metas:
            label = meta.path.name
            sess: M.Session | None = None
            try:
                # 全程只用一个 Session 对象：state 是内存态 + 磁盘态两份，
                # 若 process 内部另建一个新对象，失败路径上外层会用旧状态覆盖掉新进度。
                sess = M.Session(meta, M.workdir_for(meta, self.cfg.output_dir))
                stage = self.process(sess)
                if stage == "skipped":
                    stats.skipped += 1
                else:
                    stats.completed += 1
                    M.log(f"[{label}] ✅ 完成（{stage}）")
            except Exception as e:  # 单场失败不该拖垮整批
                msg = f"{type(e).__name__}: {e}"
                stats.note_failure(label, msg)
                M.log(f"[{label}] ❌ 失败：{msg}")
                if sess is not None:
                    step = self._current_step(sess)
                    sess.fail(step, msg)
                    sess.log_run(f"FAILED {step}: {msg}")
                    sess.log_run(traceback.format_exc()[-2000:])

        return stats

    @staticmethod
    def _current_step(sess: M.Session) -> str:
        return {0: "asr", 1: "notes", 2: "render", 3: "notify"}.get(sess.stage_index(), "unknown")


def run(cfg: Config, lookback_days: int | None = None) -> int:
    """配置已加载的前提下跑一遍，返回退出码。"""
    try:
        cfg.require_files()
    except Exception as e:
        M.log(f"启动检查失败：{e}")
        return 2

    try:
        pipe = Pipeline(cfg)
    except ScheduleError as e:
        M.log(f"排班表有问题：{e}")
        return 2

    stats = pipe.run(lookback_days=lookback_days)
    M.log(f"本轮结束：完成 {stats.completed} / 跳过 {stats.skipped} / "
          f"失败 {stats.failed}（共 {stats.total}）")
    for name, err in stats.failures:
        M.log(f"  失败：{name} — {err}")
    return 1 if stats.failed else 0
