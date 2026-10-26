#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config.py — 配置加载与校验。

单一事实来源是 JSON 配置文件；环境变量只用于注入**密钥**，不用于改行为，
这样「跑出什么结果」和「跑在谁的机器上」是解耦的，便于复现。

优先级：命令行 --config > $VNOTES_CONFIG > ./config/settings.json
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """配置缺失或不合法。"""


# 默认值集中在这里，缺省的键由这里补齐，避免每个调用点都写 .get(..., default)
DEFAULTS: dict[str, Any] = {
    "paths": {"video_dir": "./input", "output_dir": "./output"},
    "asr": {
        "backend": "sensevoice",
        "model_dir": "./models/sensevoice",
        "vad_model": "./models/vad/silero_vad.onnx",
        "threads": 2,
        "max_speech_seconds": 20.0,
        "keep_wav": False,
    },
    "llm": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k3",
        "api_key_env": "MOONSHOT_API_KEY",
        "temperature": 0.3,
        "max_tokens": 30000,
        "max_retries": 1,
        "timeout": 600,
    },
    "notes": {
        "rules_file": "./config/note_rules.md",
        "schedule_file": "./config/schedule.json",
        "min_length": 1500,
        "max_length": 20000,
    },
    "render": {"enabled": True},
    "notify": {"enabled": False, "webhook_env": "WECOM_WEBHOOK"},
    "retention": {"days": 1},
}

# 允许的键，用于抓出拼写错误（拼错的键会被静默忽略，是很难查的一类 bug）
_ALLOWED: dict[str, set[str]] = {k: set(v) for k, v in DEFAULTS.items()}


def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
    for k, v in override.items():
        key = f"{path}.{k}" if path else k
        if path:
            section = path.split(".")[0]
            if k not in _ALLOWED[section]:
                raise ConfigError(f"未知配置项：{key}（可用项：{sorted(_ALLOWED[section])}）")
        elif k not in _ALLOWED:
            raise ConfigError(f"未知配置段：{key}（可用段：{sorted(_ALLOWED)}）")
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v, key)
        else:
            base[k] = v
    return base


@dataclass
class Config:
    """已解析、已校验的配置。路径均已展开为绝对路径。"""

    raw: dict[str, Any]
    root: Path
    source: Path | None = None

    # --- 便捷访问器 -------------------------------------------------
    @property
    def video_dir(self) -> Path:
        return self._p("paths", "video_dir")

    @property
    def output_dir(self) -> Path:
        return self._p("paths", "output_dir")

    def _p(self, *keys: str) -> Path:
        cur: Any = self.raw
        for k in keys:
            cur = cur[k]
        p = Path(os.path.expandvars(os.path.expanduser(str(cur))))
        return p if p.is_absolute() else (self.root / p).resolve()

    def section(self, name: str) -> dict[str, Any]:
        return self.raw[name]

    def api_key(self) -> str | None:
        env = self.raw["llm"]["api_key_env"]
        return os.environ.get(env) or None

    def webhook(self) -> str | None:
        env = self.raw["notify"]["webhook_env"]
        return os.environ.get(env) or None

    def require_files(self) -> None:
        """开工前把该在的文件一次查完，别跑到一半才发现少模型。"""
        missing: list[str] = []

        rules = self._p("notes", "rules_file")
        if not rules.is_file():
            missing.append(f"笔记规则文件：{rules}")

        sched = self._p("notes", "schedule_file")
        if not sched.is_file():
            missing.append(f"排班表：{sched}")

        asr = self.raw["asr"]
        model_dir = self._p("asr", "model_dir")
        for name in ("model.int8.onnx", "tokens.txt"):
            if not (model_dir / name).is_file():
                missing.append(f"ASR 模型：{model_dir / name}")
        if not self._p("asr", "vad_model").is_file():
            missing.append(f"VAD 模型：{self._p('asr', 'vad_model')}")

        if missing:
            raise ConfigError(
                "以下文件缺失，请先补齐（见 docs/SETUP.md）：\n  - "
                + "\n  - ".join(missing)
            )

        if self.raw["llm"].get("dry_run"):
            return
        if not self.api_key():
            raise ConfigError(
                f"缺少环境变量 {self.raw['llm']['api_key_env']}"
                f"（生成笔记需要调用 {self.raw['llm']['base_url']}）"
            )


def load(path: str | Path | None = None, root: str | Path | None = None) -> Config:
    """加载配置。

    查找顺序：显式 path > $VNOTES_CONFIG > <root>/config/settings.json > ./config/settings.json

    注意 root 参与查找，而不只影响相对路径的基准——否则在项目仓库里跑测试时，
    会意外读到仓库自己的 config/settings.json，把测试的相对路径解析到真实项目目录。
    """
    if path is None:
        path = os.environ.get("VNOTES_CONFIG")

    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path).expanduser().resolve()
        if not cfg_path.is_file():
            raise ConfigError(f"配置文件不存在：{cfg_path}")
    else:
        candidates = []
        if root:
            # 显式给了 root 就只看 root：cwd 是「用户在哪运行」，不该影响
            # 「用哪份配置」，否则测试/脚本会意外读到仓库自己的配置。
            candidates.append(Path(root).expanduser().resolve() / "config" / "settings.json")
        else:
            candidates.append(Path.cwd() / "config" / "settings.json")
        for cand in candidates:
            if cand.is_file():
                cfg_path = cand.resolve()
                break

    data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
    if cfg_path:
        try:
            user = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{cfg_path} 不是合法 JSON：{e}") from e
        if not isinstance(user, dict):
            raise ConfigError(f"{cfg_path} 顶层必须是对象")
        _deep_merge(data, user)
        base = cfg_path.parent.parent  # config/ 的上一级即项目根
    else:
        base = Path(root).expanduser().resolve() if root else Path.cwd()

    return Config(raw=data, root=base, source=cfg_path)
