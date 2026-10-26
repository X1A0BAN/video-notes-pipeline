#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli.py — 命令行入口。

子命令：
    scan      只列出会发现哪些视频、各自归到哪个目录（不处理）
    run       跑流水线（默认）
    transcribe 只转写，不生成笔记（想先看识别质量时用）
    check     自检：配置、模型文件、ffmpeg、密钥、排班表
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config as cfg_mod
from . import models as M
from . import schedule as sched_mod


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--config", help="配置文件路径（默认 ./config/settings.json）")
    p.add_argument("--video-dir", help="覆盖配置里的视频目录")
    p.add_argument("--output-dir", help="覆盖配置里的输出目录")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vnotes",
        description="把直播/课程视频转成结构化课堂笔记（md + docx）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  vnotes check                     检查环境\n"
            "  vnotes scan                      看看会处理哪些视频\n"
            "  vnotes run                       跑全部\n"
            "  vnotes run --days 3              只处理最近 3 天的视频\n"
            "  vnotes run --only 行情           只处理文件名含「行情」的\n"
            "  vnotes transcribe --only 10-12   只转写，不出笔记\n"
        ),
    )
    sub = ap.add_subparsers(dest="cmd")

    scan = sub.add_parser("scan", help="列出待处理视频")
    _add_common(scan)
    scan.add_argument("--days", type=int, help="只看最近 N 天")

    run = sub.add_parser("run", help="跑完整流水线")
    _add_common(run)
    run.add_argument("--days", type=int, help="只处理最近 N 天的视频")
    run.add_argument("--only", help="只处理文件名包含该关键词的视频")
    run.add_argument("--force", action="store_true", help="已完成的重做")
    run.add_argument("--dry-run", action="store_true",
                     help="不调模型，用占位笔记打通流程（开发/演示用）")

    tr = sub.add_parser("transcribe", help="只转写")
    _add_common(tr)
    tr.add_argument("--only", help="只处理文件名包含该关键词的视频")
    tr.add_argument("--days", type=int)

    chk = sub.add_parser("check", help="环境自检")
    _add_common(chk)
    return ap


def _load(args) -> cfg_mod.Config:
    cfg = cfg_mod.load(args.config)
    if getattr(args, "video_dir", None):
        cfg.raw["paths"]["video_dir"] = args.video_dir
    if getattr(args, "output_dir", None):
        cfg.raw["paths"]["output_dir"] = args.output_dir
    return cfg


def _filter(metas, only: str | None):
    if not only:
        return metas
    key = only.lower()
    kept = [m for m in metas if key in m.path.name.lower()]
    if not kept:
        M.log(f"没有文件名包含「{only}」的视频")
    return kept


def cmd_scan(args) -> int:
    cfg = _load(args)
    try:
        metas = M.discover(cfg.video_dir, lookback_days=args.days)
    except FileNotFoundError as e:
        M.log(str(e))
        return 2
    sched = sched_mod.Schedule.load(cfg._p("notes", "schedule_file"))
    if not metas:
        M.log(f"{cfg.video_dir} 里没有视频文件")
        return 0

    print(f"\n找到 {len(metas)} 个视频：\n")
    for m in metas:
        lec = sched.for_date(m.recorded_on)
        who = lec.name if lec else "未排班"
        wd = sched_mod.weekday_cn(m.recorded_on)
        out = M.workdir_for(m, cfg.output_dir)
        print(f"  {m.recorded_on.isoformat()} 周{wd}  {who:<6} "
              f"{m.path.name}")
        print(f"      → {out}")
    print()
    return 0


def cmd_run(args) -> int:
    from . import pipeline

    cfg = _load(args)
    if args.dry_run:
        cfg.raw["llm"]["dry_run"] = True
        cfg.raw["notify"]["enabled"] = False
    if args.force:
        _clear_states(cfg)

    if args.only:
        kept = _filter(M.discover(cfg.video_dir), args.only)
        return _run_selected(cfg, kept, args.days)

    return pipeline.run(cfg, lookback_days=args.days)


def _run_selected(cfg, metas, days: int | None) -> int:
    """--only 时只跑选中项：复用 Pipeline 的逐场逻辑，跳过目录扫描。"""
    from .pipeline import Pipeline, RunStats

    if not metas:
        return 0
    try:
        cfg.require_files()
    except Exception as e:
        M.log(f"启动检查失败：{e}")
        return 2
    pipe = Pipeline(cfg)
    stats = RunStats()
    stats.total = len(metas)
    for meta in metas:
        sess = M.Session(meta, M.workdir_for(meta, cfg.output_dir))
        try:
            stage = pipe.process(sess)
            if stage == "skipped":
                stats.skipped += 1
            else:
                stats.completed += 1
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            stats.note_failure(meta.path.name, msg)
            sess.fail(pipe._current_step(sess), msg)
            M.log(f"[{meta.path.name}] ❌ {msg}")
    M.log(f"完成 {stats.completed} / 跳过 {stats.skipped} / 失败 {stats.failed}")
    return 1 if stats.failed else 0


def _clear_states(cfg) -> None:
    root = cfg.output_dir
    if not root.is_dir():
        return
    n = 0
    for st in root.rglob("state.json"):
        st.unlink(missing_ok=True)
        n += 1
    if n:
        M.log(f"--force：已清除 {n} 个状态文件（产物不会删，缺的才会重做）")


def cmd_transcribe(args) -> int:
    from . import asr

    cfg = _load(args)
    try:
        metas = _filter(M.discover(cfg.video_dir, lookback_days=args.days), args.only)
    except FileNotFoundError as e:
        M.log(str(e))
        return 2
    if not metas:
        return 0

    asr_cfg = cfg.section("asr")
    failed = 0
    for meta in metas:
        wd = M.workdir_for(meta, cfg.output_dir)
        sess = M.Session(meta, wd)
        if sess.transcript.exists() and sess.transcript.stat().st_size > 0:
            M.log(f"[{meta.path.name}] 已有转写稿，跳过")
            continue
        try:
            asr.transcribe(
                meta.path, sess.transcript,
                model_dir=cfg._p("asr", "model_dir"),
                vad_model=cfg._p("asr", "vad_model"),
                threads=asr_cfg["threads"],
                max_speech_seconds=asr_cfg["max_speech_seconds"],
                keep_wav=asr_cfg.get("keep_wav", False),
            )
        except Exception as e:
            failed += 1
            M.log(f"[{meta.path.name}] ❌ {type(e).__name__}: {e}")
    return 1 if failed else 0


def cmd_check(args) -> int:
    import os

    print("\n环境自检\n" + "=" * 46)
    problems: list[str] = []

    cfg = _load(args)
    print(f"配置来源      : {cfg.source or '（默认值，未找到 settings.json）'}")
    print(f"项目根        : {cfg.root}")

    print(f"视频目录      : {cfg.video_dir} "
          f"{'✅' if cfg.video_dir.is_dir() else '❌ 不存在'}")
    if not cfg.video_dir.is_dir():
        problems.append(f"视频目录不存在：{cfg.video_dir}")

    print(f"输出目录      : {cfg.output_dir} "
          f"{'✅' if cfg.output_dir.is_dir() else '（将自动创建）'}")

    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(os.environ.get(name.upper(), name))
        print(f"{name:<13} : {path or '❌ 未找到'}")
        if not path:
            problems.append(f"未找到 {name}")

    try:
        sched = sched_mod.Schedule.load(cfg._p("notes", "schedule_file"))
        covered = sorted({wd for l in sched.lecturers for wd in l.weekdays})
        print(f"排班表        : ✅ {len(sched.lecturers)} 位讲师，"
              f"覆盖周{''.join(sched_mod.WEEKDAY_CN[i] for i in covered)}")
    except Exception as e:
        print(f"排班表        : ❌ {e}")
        problems.append(str(e))

    rules = cfg._p("notes", "rules_file")
    print(f"笔记规则      : {'✅ ' + str(rules) if rules.is_file() else '❌ 缺失 ' + str(rules)}")
    if not rules.is_file():
        problems.append(f"笔记规则缺失：{rules}")

    model_dir = cfg._p("asr", "model_dir")
    for f in ("model.int8.onnx", "tokens.txt"):
        ok = (model_dir / f).is_file()
        print(f"ASR 模型      : {f} {'✅' if ok else '❌'}")
        if not ok:
            problems.append(f"ASR 模型缺失：{model_dir / f}")
    vad = cfg._p("asr", "vad_model")
    print(f"VAD 模型      : {'✅' if vad.is_file() else '❌ ' + str(vad)}")
    if not vad.is_file():
        problems.append(f"VAD 模型缺失：{vad}")

    try:
        import sherpa_onnx  # noqa: F401
        print("sherpa-onnx   : ✅")
    except ImportError:
        print("sherpa-onnx   : ❌ 未安装（pip install sherpa-onnx）")
        problems.append("sherpa-onnx 未安装")

    try:
        import docx  # noqa: F401
        print("python-docx   : ✅")
    except ImportError:
        print("python-docx   : ❌ 未安装（pip install python-docx）")
        problems.append("python-docx 未安装")

    key_env = cfg.section("llm")["api_key_env"]
    print(f"{key_env:<13} : {'✅ 已设置' if os.environ.get(key_env) else '❌ 未设置'}")
    if not os.environ.get(key_env):
        problems.append(f"环境变量 {key_env} 未设置（或加 --dry-run 跳过）")

    n_videos = 0
    if cfg.video_dir.is_dir():
        n_videos = len(M.discover(cfg.video_dir))
    print(f"待处理视频    : {n_videos} 个")

    print("=" * 46)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("全部就绪 ✅")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    handler = {
        "scan": cmd_scan,
        "run": cmd_run,
        "transcribe": cmd_transcribe,
        "check": cmd_check,
    }[args.cmd]
    try:
        return handler(args)
    except cfg_mod.ConfigError as e:
        M.log(f"配置错误：{e}")
        return 2
    except KeyboardInterrupt:
        M.log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
