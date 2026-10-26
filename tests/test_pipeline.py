# -*- coding: utf-8 -*-
"""编排器的端到端测试：全部依赖都被替换，不碰 ffmpeg / 模型 / 网络。

重点验证：幂等跳过、失败不丢进度、单场失败不拖垮整批。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vnotes import asr as asr_mod  # noqa: E402
from vnotes import md2docx, notifier  # noqa: E402
from vnotes import models as M  # noqa: E402
from vnotes import note_writer  # noqa: E402
from vnotes.config import Config, _deep_merge, DEFAULTS  # noqa: E402
from vnotes.pipeline import Pipeline, run  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """造一个最小可用项目：配置 + 规则 + 排班 + 一个视频。"""
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "note_rules.md").write_text("规则正文", encoding="utf-8")
    (root / "config" / "schedule.json").write_text(
        (REPO / "config" / "schedule.json").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "input").mkdir()
    (root / "input" / "2026-09-01_课.mp4").write_bytes(b"\0" * 64)

    raw = json.loads(json.dumps(DEFAULTS))
    _deep_merge(raw, {"paths": {"video_dir": "./input", "output_dir": "./output"}})
    cfg = Config(raw=raw, root=root)

    # 模型与转写全部打桩
    monkeypatch.setattr(asr_mod, "transcribe", _fake_transcribe)
    monkeypatch.setattr(note_writer, "generate", _fake_generate)
    monkeypatch.setattr(md2docx, "convert", _fake_convert)
    return cfg


def _fake_transcribe(src, out_txt, **kw):
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text("[00.00] 今天讲量能\n[01.30] 半导体设备是主线\n", encoding="utf-8")
    return asr_mod.TranscriptResult(out_txt, 60.0, 50.0, 2, 2, 2.0)


def _fake_generate(transcript, meta, out_md, rules, schedule, llm_cfg, notes_cfg, extras=None):
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(f"# 📒 课堂笔记 · {meta.title}\n", encoding="utf-8")
    return note_writer.ValidationReport()


def _fake_convert(src, dst):
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_bytes(b"PK\x03\x04fake")
    return Path(dst)


def test_full_run_creates_all_artifacts(env):
    stats = Pipeline(env).run()
    assert stats.total == 1
    assert stats.completed == 1
    assert stats.failed == 0

    wd = env.output_dir / "26.9.1"
    assert (wd / "transcript.txt").is_file()
    assert (wd / "课.课堂笔记.md").is_file()
    assert (wd / "课.课堂笔记.docx").is_file()
    state = json.loads((wd / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "delivered"


def test_second_run_is_noop(env):
    Pipeline(env).run()
    stats = Pipeline(env).run()
    assert stats.skipped == 1
    assert stats.completed == 0


def test_existing_transcript_skips_asr(env, monkeypatch):
    """回归：先 transcribe 再 run，不该把音频重转一遍。"""
    wd = env.output_dir / "26.9.1"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "transcript.txt").write_text("[00.00] 已有的稿子\n", encoding="utf-8")

    def explode(*a, **kw):
        raise AssertionError("已有转写稿时不该再调 ASR")

    monkeypatch.setattr(asr_mod, "transcribe", explode)
    stats = Pipeline(env).run()
    assert stats.completed == 1
    assert (wd / "课.课堂笔记.docx").is_file()


def test_resume_from_transcribed(env, monkeypatch):
    """已有转写稿时不该再调 ASR。"""
    called = {"asr": 0}
    orig = asr_mod.transcribe

    def counting(*a, **kw):
        called["asr"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(asr_mod, "transcribe", counting)
    pipe = Pipeline(env)
    pipe.run()
    assert called["asr"] == 1

    # 抹掉状态但保留转写稿 → 续跑时凭产物跳过 ASR，只补后续阶段
    wd = env.output_dir / "26.9.1"
    (wd / "state.json").unlink()
    (wd / "课.课堂笔记.md").unlink()
    (wd / "课.课堂笔记.docx").unlink()
    Pipeline(env).run()
    assert called["asr"] == 1, "转写稿已存在，不该重跑 ASR"
    assert (wd / "课.课堂笔记.docx").is_file()


def test_failure_isolated_and_recorded(env, monkeypatch):
    (env.video_dir / "2026-09-02_bad.mp4").write_bytes(b"\0" * 64)

    def boom(*a, **kw):
        raise asr_mod.ASRError("模拟音频损坏")

    real = asr_mod.transcribe

    def selective(src, *a, **kw):
        if "bad" in Path(src).name:
            boom()
        return real(src, *a, **kw)

    monkeypatch.setattr(asr_mod, "transcribe", selective)
    stats = Pipeline(env).run()

    assert stats.total == 2
    assert stats.completed == 1
    assert stats.failed == 1
    assert any("bad" in n for n, _ in stats.failures)

    # 好的那场仍然完成
    assert (env.output_dir / "26.9.1" / "课.课堂笔记.docx").is_file()
    # 坏的那场留下了失败记录，且阶段没有倒退
    bad_wd = env.output_dir / "26.9.2"
    st = json.loads((bad_wd / "state.json").read_text(encoding="utf-8"))
    assert st["stage"] == "discovered"
    assert st["failed_step"] == "asr"
    assert "模拟音频损坏" in st["last_error"]


def test_partial_failure_then_recovery(env, monkeypatch):
    """失败后修好，重跑应该从失败点继续，不重做已完成的事。"""
    state = {"broken": True}
    real_note = _fake_generate
    calls = {"notes": 0}

    def flaky_note(*a, **kw):
        calls["notes"] += 1
        if state["broken"]:
            raise note_writer.NoteGenError("模型 502")
        return real_note(*a, **kw)

    monkeypatch.setattr(note_writer, "generate", flaky_note)
    stats = Pipeline(env).run()
    assert stats.failed == 1
    wd = env.output_dir / "26.9.1"
    st = json.loads((wd / "state.json").read_text(encoding="utf-8"))
    assert st["stage"] == "transcribed", "转写进度不该被笔记失败抹掉"
    assert (wd / "transcript.txt").is_file()

    state["broken"] = False
    stats2 = Pipeline(env).run()
    assert stats2.completed == 1
    assert (wd / "课.课堂笔记.docx").is_file()


def test_render_disabled(env):
    env.raw["render"]["enabled"] = False
    Pipeline(env).run()
    wd = env.output_dir / "26.9.1"
    assert (wd / "课.课堂笔记.md").is_file()
    assert not (wd / "课.课堂笔记.docx").exists()


def test_notify_disabled_by_default(env, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("notify 关闭时不该被调用")

    monkeypatch.setattr(notifier, "push", explode)
    Pipeline(env).run()  # 不应触发


def test_notify_missing_webhook_fails_cleanly(env, monkeypatch):
    env.raw["notify"]["enabled"] = True
    monkeypatch.delenv("WECOM_WEBHOOK", raising=False)
    stats = Pipeline(env).run()
    assert stats.failed == 1
    assert any("WECOM_WEBHOOK" in e for _, e in stats.failures)


def test_extra_materials_fed_to_model(env, monkeypatch):
    seen = {}

    def capture(transcript, meta, out_md, rules, schedule, llm_cfg, notes_cfg, extras=None):
        seen["extras"] = [p.name for p, _ in (extras or [])]
        return _fake_generate(transcript, meta, out_md, rules, schedule,
                              llm_cfg, notes_cfg, extras)

    monkeypatch.setattr(note_writer, "generate", capture)
    (env.video_dir / "盘后笔记.txt").write_text("讲师原文", encoding="utf-8")
    Pipeline(env).run()
    assert seen["extras"] == ["盘后笔记.txt"]


def test_dry_run_writes_placeholder(env, monkeypatch):
    """dry_run 走真实的 generate，只把模型调用跳过。"""
    env.raw["llm"]["dry_run"] = True
    monkeypatch.undo()  # 先还原 fixture 里对 generate 的打桩
    monkeypatch.setattr(asr_mod, "transcribe", _fake_transcribe)
    monkeypatch.setattr(md2docx, "convert", _fake_convert)

    Pipeline(env).run()
    md = (env.output_dir / "26.9.1" / "课.课堂笔记.md").read_text(encoding="utf-8")
    assert "dry_run" in md
    assert "非投资建议" in md


def test_missing_video_dir_returns_failure(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "config" / "note_rules.md").write_text("规则", encoding="utf-8")
    (root / "config" / "schedule.json").write_text(
        (REPO / "config" / "schedule.json").read_text(encoding="utf-8"), encoding="utf-8")

    raw = json.loads(json.dumps(DEFAULTS))
    _deep_merge(raw, {"paths": {"video_dir": "./nope", "output_dir": "./out"}})
    cfg = Config(raw=raw, root=root)
    stats = Pipeline(cfg).run()
    assert stats.failed == 1
    assert stats.total == 0


def test_run_wrapper_reports_config_problems(tmp_path, monkeypatch):
    cfg = Config(raw=json.loads(json.dumps(DEFAULTS)), root=tmp_path)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    assert run(cfg) == 2
