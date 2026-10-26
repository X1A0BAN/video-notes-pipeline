#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asr.py — 音频抽取 + SenseVoice 转写。

分两层：
    extract_audio()   纯 ffmpeg，无模型依赖，可单独测
    transcribe()      模型推理，依赖 sherpa-onnx 与 onnx 模型文件

转写稿格式（下游按行解析，改动需同步 note_writer）：
    [MM.SS] 文本
时间戳是该说话片段的起点（分钟.秒），VAD 切段保证不会跨越长静音。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16000

# SenseVoice int8 模型约 228 MB；低于这个数基本可以断定下载不完整。
# 校验大小比等 sherpa 抛「Protobuf parsing failed」友好得多。
SENSEVOICE_MIN_BYTES = 150 * 1024 * 1024
VAD_MIN_BYTES = 100 * 1024


class ASRError(Exception):
    """音频抽取或识别失败。"""


@dataclass
class TranscriptResult:
    text_path: Path
    duration_sec: float
    speech_sec: float
    segments: int
    lines: int
    elapsed_sec: float

    @property
    def speedup(self) -> float:
        return self.duration_sec / self.elapsed_sec if self.elapsed_sec else 0.0


def log(msg: str) -> None:
    print(f"[asr] {msg}", flush=True)


def _ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG", "ffmpeg")


def probe_duration(src: Path) -> float | None:
    """用 ffprobe 取时长，失败返回 None（不致命，仅用于日志与进度）。"""
    ffprobe = os.environ.get("FFPROBE", "ffprobe")
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, timeout=60,
        )
        return float(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def extract_audio(src: Path, dst: Path) -> None:
    """把任意音视频转成 16kHz 单声道 16bit wav。"""
    cmd = [_ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(src),
           "-vn", "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "wav", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as e:
        raise ASRError(f"找不到 ffmpeg（可用环境变量 FFMPEG 指定路径）：{e}") from e
    except subprocess.TimeoutExpired as e:
        raise ASRError(f"ffmpeg 抽音超过 1 小时：{src}") from e
    if r.returncode != 0:
        raise ASRError(f"ffmpeg 抽音失败（{r.returncode}）：{r.stderr.strip()[-600:]}")
    if not dst.is_file() or dst.stat().st_size == 0:
        raise ASRError(f"ffmpeg 未产出音频：{dst}")


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if (w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1
                or w.getsampwidth() != 2):
            raise ASRError(
                f"需要 16kHz/单声道/16bit wav，实际 "
                f"{w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth() * 8}bit"
            )
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0


def transcribe(
    src: Path,
    out_txt: Path,
    *,
    model_dir: Path,
    vad_model: Path,
    threads: int = 2,
    max_speech_seconds: float = 20.0,
    keep_wav: bool = False,
) -> TranscriptResult:
    """转写 src，结果写入 out_txt。中间 wav 默认清理。"""
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError as e:
        raise ASRError(f"缺少依赖 sherpa-onnx / numpy：{e}（pip install sherpa-onnx）") from e

    model_file = model_dir / "model.int8.onnx"
    tokens = model_dir / "tokens.txt"
    for p in (model_file, tokens, vad_model):
        if not p.is_file():
            raise ASRError(f"模型文件缺失：{p}")

    _check_model_size(model_file, SENSEVOICE_MIN_BYTES, "SenseVoice")
    _check_model_size(vad_model, VAD_MIN_BYTES, "Silero VAD")

    t0 = time.time()
    tmpdir: Path | None = None
    wav_path = src
    if not (src.suffix.lower() == ".wav" and _is_16k_mono(src)):
        tmpdir = Path(tempfile.mkdtemp(prefix="vnotes_asr_"))
        wav_path = tmpdir / "audio.wav"
        log(f"抽音频 → {wav_path.name}")
        extract_audio(src, wav_path)

    try:
        samples = read_wav(wav_path)
        duration = len(samples) / SAMPLE_RATE

        try:
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_file), tokens=str(tokens),
                num_threads=threads, use_itn=True, language="zh", debug=False,
            )
        except RuntimeError as e:
            # 最常见的原因是模型文件下载不完整（tar 解压中断、断点续传截断），
            # sherpa 只会说「Protobuf parsing failed」，对使用者没有任何指向性。
            size_mb = model_file.stat().st_size / 1024 / 1024
            raise ASRError(
                f"加载 SenseVoice 模型失败：{e}\n"
                f"  模型文件：{model_file}（{size_mb:.0f} MB）\n"
                f"  若体积明显偏小，说明下载不完整，请按 docs/SETUP.md 重新下载。"
            ) from e
        except Exception as e:
            raise ASRError(f"加载 SenseVoice 模型失败：{e}") from e
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(vad_model)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.5
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = max_speech_seconds
        cfg.sample_rate = SAMPLE_RATE
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)

        window = cfg.silero_vad.window_size
        segments: list[tuple[float, Any]] = []
        for i in range(0, len(samples), window):
            vad.accept_waveform(samples[i:i + window])
            while not vad.empty():
                segments.append((vad.front.start / SAMPLE_RATE, np.array(vad.front.samples)))
                vad.pop()
        vad.flush()
        while not vad.empty():
            segments.append((vad.front.start / SAMPLE_RATE, np.array(vad.front.samples)))
            vad.pop()

        speech_sec = sum(len(s) for _, s in segments) / SAMPLE_RATE
        log(f"VAD {len(segments)} 段 / 说话 {speech_sec/60:.1f} 分钟"
            f"（{speech_sec/duration*100:.0f}%），静音跳过")

        t1 = time.time()
        lines = 0
        out_txt.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out_txt.with_suffix(".txt.tmp")
        with tmp_out.open("w", encoding="utf-8") as f:
            for idx, (start, seg) in enumerate(segments, 1):
                stream = rec.create_stream()
                stream.accept_waveform(SAMPLE_RATE, seg)
                rec.decode_stream(stream)
                text = stream.result.text.strip()
                if not text:
                    continue
                f.write(f"[{start/60:06.2f}] {text}\n")
                lines += 1
                if idx % 100 == 0:
                    log(f"  {idx}/{len(segments)} 段，{lines} 行，"
                        f"{time.time()-t1:.0f}s（{start/60:.0f}/{duration/60:.0f} 分）")
        os.replace(tmp_out, out_txt)
        elapsed = time.time() - t1

        result = TranscriptResult(
            text_path=out_txt, duration_sec=duration, speech_sec=speech_sec,
            segments=len(segments), lines=lines, elapsed_sec=elapsed,
        )
        log(f"完成 {lines} 行 / {elapsed:.1f}s = {result.speedup:.1f}× 实时"
            f"（全程 {time.time()-t0:.1f}s）")
        return result
    finally:
        if tmpdir is not None:
            if keep_wav:
                keep = out_txt.parent / "audio.wav"
                try:
                    os.replace(wav_path, keep)
                    log(f"保留音频：{keep}")
                except OSError:
                    pass
            _rmtree(tmpdir)


def _check_model_size(path: Path, min_bytes: int, label: str) -> None:
    size = path.stat().st_size
    if size < min_bytes:
        raise ASRError(
            f"{label} 模型文件疑似不完整：{path} 只有 {size / 1024 / 1024:.1f} MB，"
            f"至少应有 {min_bytes / 1024 / 1024:.0f} MB。请重新下载（见 docs/SETUP.md）。"
        )


def _is_16k_mono(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as w:
            return (w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
                    and w.getsampwidth() == 2)
    except (wave.Error, OSError):
        return False


def _rmtree(p: Path) -> None:
    try:
        for f in p.iterdir():
            f.unlink(missing_ok=True)
        p.rmdir()
    except OSError:
        pass
