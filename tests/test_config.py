# -*- coding: utf-8 -*-
"""配置加载、合并、校验。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vnotes import config as C  # noqa: E402


def _write(tmp_path: Path, data: dict) -> Path:
    cfgdir = tmp_path / "config"
    cfgdir.mkdir(exist_ok=True)
    p = cfgdir / "settings.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_defaults_loaded_when_no_file(tmp_path):
    cfg = C.load(root=tmp_path)
    assert cfg.source is None
    assert cfg.video_dir == (tmp_path / "input").resolve()
    assert cfg.section("llm")["model"] == "kimi-k3"


def test_merge_keeps_unspecified_defaults(tmp_path):
    p = _write(tmp_path, {"llm": {"model": "other-model"}})
    cfg = C.load(p)
    assert cfg.section("llm")["model"] == "other-model"
    assert cfg.section("llm")["base_url"] == "https://api.moonshot.cn/v1"
    assert cfg.section("asr")["threads"] == 2


def test_unknown_key_rejected(tmp_path):
    p = _write(tmp_path, {"llm": {"modle": "typo"}})
    with pytest.raises(C.ConfigError, match="未知配置项"):
        C.load(p)


def test_unknown_section_rejected(tmp_path):
    p = _write(tmp_path, {"gpu": {"enabled": True}})
    with pytest.raises(C.ConfigError, match="未知配置段.*gpu"):
        C.load(p)


def test_invalid_json_reported(tmp_path):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    bad = cfgdir / "settings.json"
    bad.write_text("{ oops", encoding="utf-8")
    with pytest.raises(C.ConfigError, match="不是合法 JSON"):
        C.load(bad)


def test_missing_file_reported(tmp_path):
    with pytest.raises(C.ConfigError, match="不存在"):
        C.load(tmp_path / "nope.json")


def test_paths_resolve_relative_to_project_root(tmp_path):
    p = _write(tmp_path, {"paths": {"video_dir": "media"}})
    cfg = C.load(p)
    assert cfg.video_dir == (tmp_path / "media").resolve()


def test_absolute_paths_preserved(tmp_path):
    target = tmp_path / "elsewhere"
    p = _write(tmp_path, {"paths": {"video_dir": str(target)}})
    assert C.load(p).video_dir == target.resolve()


def test_env_var_expanded_in_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VNOTES_TEST_DIR", str(tmp_path / "fromenv"))
    p = _write(tmp_path, {"paths": {"video_dir": "$VNOTES_TEST_DIR"}})
    assert C.load(p).video_dir == (tmp_path / "fromenv").resolve()


def test_env_overrides_config_path(tmp_path, monkeypatch):
    p = _write(tmp_path, {"llm": {"model": "from-env-var"}})
    monkeypatch.setenv("VNOTES_CONFIG", str(p))
    assert C.load().section("llm")["model"] == "from-env-var"


def test_api_key_read_from_named_env(tmp_path, monkeypatch):
    cfg = C.load(root=tmp_path)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    assert cfg.api_key() is None
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    assert cfg.api_key() == "sk-test"


def test_require_files_lists_everything_missing(tmp_path, monkeypatch):
    p = _write(tmp_path, {})
    cfg = C.load(p)
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")
    with pytest.raises(C.ConfigError) as ei:
        cfg.require_files()
    msg = str(ei.value)
    # 一次把该报的都报出来，而不是跑到一半才说少个模型
    assert "笔记规则文件" in msg
    assert "排班表" in msg
    assert "ASR 模型" in msg
    assert "VAD 模型" in msg


def test_require_files_skips_key_in_dry_run(tmp_path, monkeypatch):
    cfg = C.load(root=tmp_path)
    cfg.raw["llm"]["dry_run"] = True
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    # 文件仍缺失，所以先造好
    _fake_assets(cfg)
    cfg.require_files()  # 不应因缺密钥而失败


def test_require_files_reports_missing_key(tmp_path, monkeypatch):
    cfg = C.load(root=tmp_path)
    _fake_assets(cfg)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    with pytest.raises(C.ConfigError, match="MOONSHOT_API_KEY"):
        cfg.require_files()


def test_root_participates_in_lookup(tmp_path, monkeypatch):
    """回归：root 指定的目录里若有 settings.json，必须用它，
    而不是去读 cwd（项目仓库）里那份——那会让测试写脏真实配置。"""
    _write(tmp_path, {"llm": {"model": "from-root"}})
    monkeypatch.chdir(tmp_path.parent)
    cfg = C.load(root=tmp_path)
    assert cfg.section("llm")["model"] == "from-root"
    assert cfg.root == tmp_path.resolve()


def _fake_assets(cfg: C.Config) -> None:
    cfg._p("notes", "rules_file").parent.mkdir(parents=True, exist_ok=True)
    cfg._p("notes", "rules_file").write_text("规则", encoding="utf-8")
    cfg._p("notes", "schedule_file").write_text('{"lecturers":[{"name":"甲","weekdays":[0]}]}',
                                               encoding="utf-8")
    md = cfg._p("asr", "model_dir")
    md.mkdir(parents=True, exist_ok=True)
    (md / "model.int8.onnx").write_bytes(b"x")
    (md / "tokens.txt").write_text("a 0", encoding="utf-8")
    cfg._p("asr", "vad_model").parent.mkdir(parents=True, exist_ok=True)
    cfg._p("asr", "vad_model").write_bytes(b"x")


def test_webhook_from_env(tmp_path, monkeypatch):
    cfg = C.load(root=tmp_path)
    monkeypatch.delenv("WECOM_WEBHOOK", raising=False)
    assert cfg.webhook() is None
    monkeypatch.setenv("WECOM_WEBHOOK", "https://qyapi.weixin.qq.com/x?key=k")
    assert cfg.webhook().endswith("key=k")
