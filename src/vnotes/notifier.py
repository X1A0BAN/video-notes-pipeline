#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notifier.py — 企业微信群机器人推送：markdown 摘要 + docx 文件。

摘要字段**程序化抽取**，不让模型二次生成——二次生成是幻觉的主要入口，
且会与正文不一致（正文写 62 分，摘要写高风险，读的人该信哪个？）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

MAX_MARKDOWN_BYTES = 3800  # 官方上限 4096，留余量
_RETRY_WAITS = (1, 5, 25)


class NotifyError(Exception):
    """推送失败（重试耗尽）。"""


def log(msg: str) -> None:
    print(f"[push] {msg}", flush=True)


class WeComNotifier:
    def __init__(self, webhook: str):
        self.webhook = webhook
        m = re.search(r"key=([^&]+)", webhook)
        if not m:
            raise NotifyError("webhook 地址里没有 key 参数")
        self.key = m.group(1)

    # --- HTTP -------------------------------------------------------
    def _post(self, url: str, payload: dict, timeout: int = 30) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("errcode") != 0:
            raise NotifyError(f"企业微信返回错误：{body}")
        return body

    def _with_retry(self, fn, what: str, retries: int = 3):
        for i in range(retries):
            try:
                return fn()
            except (urllib.error.URLError, NotifyError, TimeoutError) as e:
                if i == retries - 1:
                    raise NotifyError(f"{what} 失败（已重试 {retries} 次）：{e}") from e
                wait = _RETRY_WAITS[min(i, len(_RETRY_WAITS) - 1)]
                log(f"{what} 失败（{e}），{wait}s 后重试（{i + 1}/{retries - 1}）")
                time.sleep(wait)
        return None

    def send_markdown(self, content: str) -> None:
        payload = {"msgtype": "markdown", "markdown": {"content": content}}
        self._with_retry(lambda: self._post(self.webhook, payload), "摘要推送")
        log("markdown 摘要已推送")

    def upload_and_send_file(self, path: Path) -> None:
        media_id = self._with_retry(lambda: self._upload(path), "素材上传")
        payload = {"msgtype": "file", "file": {"media_id": media_id}}
        self._with_retry(lambda: self._post(self.webhook, payload), "文件推送")
        log(f"docx 已推送：{path.name}")

    def _upload(self, path: Path) -> str:
        url = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
               f"?key={self.key}&type=file")
        boundary = "----vnotes" + str(int(time.time() * 1000))
        content = path.read_bytes()
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; '
            f'filename="{path.name}"; filelength={len(content)}\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body = head + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("errcode") != 0:
            raise NotifyError(f"上传素材失败：{data}")
        return data["media_id"]


# --- 摘要抽取（纯函数，可测） ----------------------------------------

def _truncate_utf8(s: str, limit: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    while len(b) > limit and s:
        s = s[: int(len(s) * 0.85)]
        b = s.encode("utf-8")
    return s.rstrip() + "…"


def _section_items(md: str, title_pat: str, limit: int = 3) -> list[str]:
    m = re.search(rf"^##+\s+.*{title_pat}.*$", md, re.M)
    if not m:
        return []
    out: list[str] = []
    for line in md[m.end():].splitlines():
        s = line.strip()
        if s.startswith("#"):
            break
        im = re.match(r"^(?:[-*+]|\d{1,3}[.)、])\s*(.+)", s)
        if im:
            out.append(re.sub(r"\*\*", "", im.group(1)).strip())
        if len(out) >= limit:
            break
    return out


def _extract_band(md: str) -> str:
    """抽取风险等级及其说明，括号必须配对完整。

    输入：风险指数 45/100（中风险，谨慎做多，控仓低吸）
    返回：（中风险，谨慎做多，控仓低吸）

    注意开括号在「中风险」**之前**，所以整段一起匹配，
    否则会从标签之后开始找括号而永远找不到。
    """
    full = re.search(
        r"[（(]\s*(低风险|中风险|高风险)\s*(?:[，,、]\s*([^）)]*))?[）)]", md)
    if full:
        label = full.group(1)
        detail = (full.group(2) or "").strip()
        return f"（{label}，{detail}）" if detail else f"（{label}）"

    bare = re.search(r"(低风险|中风险|高风险)", md)
    return f"（{bare.group(1)}）" if bare else ""


def build_summary(md: str) -> str:
    header = re.search(r"^>\s*讲师：(.+)$", md, re.M)
    head_txt = header.group(1).strip() if header else ""

    risk_m = re.search(r"风险指数[^\d]{0,10}(\d{1,3})", md)
    risk = risk_m.group(1) if risk_m else "?"
    band = _extract_band(md)

    # 一句话主旨 = 抬头之后第一行非结构化的加粗文本
    gist = ""
    body_after_header = md[header.end():] if header else md
    for line in body_after_header.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ">", "|", "---", "*备注")):
            continue
        bm = re.match(r"\*\*(.+?)\*\*", s)
        gist = (bm.group(1) if bm else s).strip()
        break

    plans = _section_items(md, "次日预案|明日")
    risks = _section_items(md, "风险提示", limit=2)

    # 抬头里的「讲师：」在群消息里是冗余的，去掉后直接当标题用
    if head_txt:
        title = re.sub(r"^讲师：", "", head_txt).strip()
        head = [f"## 📒 {title}", f"> 风险指数：{risk}/100 {band}".rstrip()]
    else:
        head = ["## 📒 课堂笔记", f"> 风险指数：{risk}/100".rstrip()]

    body: list[str] = []
    if gist:
        body.append(f"\n**一句话主旨**：{gist}")
    if plans:
        body.append("\n**下次要点**：")
        body += [f"{i + 1}. {p}" for i, p in enumerate(plans)]
    if risks:
        body.append("\n⚠️ " + risks[0])

    footer = "\n完整 docx 见下方文件消息。"
    # 先拼正文再截断，最后补 footer——否则 footer 会被截到中间甚至丢失，
    # 读者就不知道还有一份全文了。
    content = _truncate_utf8("\n".join(head + body), MAX_MARKDOWN_BYTES - len(footer.encode("utf-8")))
    return content + footer


def push(note_md: Path, note_docx: Path | None, webhook: str) -> None:
    notifier = WeComNotifier(webhook)
    notifier.send_markdown(build_summary(Path(note_md).read_text(encoding="utf-8")))
    if note_docx and Path(note_docx).is_file():
        notifier.upload_and_send_file(Path(note_docx))
