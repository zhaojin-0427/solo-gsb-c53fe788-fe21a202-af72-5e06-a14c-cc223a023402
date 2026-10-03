"""SRT / WebVTT 解析，Unicode 字素簇计数与原文哈希。

计数规则：去除 VTT/SRT 标记标签，将多行文本以空格拼接，空白折叠为单个空格后，
按 Unicode 扩展字素簇（UAX #29 grapheme cluster）计数，空格计入字符。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import regex

_TS = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
TIMESTAMP_RE = re.compile(rf"\s*{_TS}\s*-->\s*{_TS}\s*.*")
_TAG_RE = re.compile(r"<[^>]+>")
_VOICE_RE = re.compile(r"^<v[^>]*>|</v>$", re.IGNORECASE)
# UAX #29 扩展字素簇
_GRAPHEME_RE = regex.compile(r"\X")
_WS_RE = regex.compile(r"\s+")


@dataclass
class ParsedCue:
    cue_id: str
    ord: int
    start_ms: int
    end_ms: int
    raw_text: str          # 去除标签后的纯文本（保留换行）
    graphemes: int
    raw_hash: str


def _ts_to_ms(h: str, m: str, s: str, ms: str) -> int:
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(ms.ljust(3, "0"))


def count_graphemes(raw_text: str) -> int:
    """去除标签、折叠空白后按扩展字素簇计数。"""
    text = _TAG_RE.sub("", raw_text)
    text = _WS_RE.sub(" ", text.strip())
    return len(_GRAPHEME_RE.findall(text))


def visible_text(raw_text: str) -> str:
    """展示/导出用的可见文本：去标签、空白折叠为空格。"""
    text = _TAG_RE.sub("", raw_text)
    return _WS_RE.sub(" ", text.strip())


def _parse_block(block: list[str], ordinal: int, used_ids: set[str]) -> ParsedCue | None:
    idx = 0
    cue_id: str | None = None
    if not TIMESTAMP_RE.match(block[0]):
        cue_id = block[0].strip()
        idx = 1
    if idx >= len(block) or not TIMESTAMP_RE.match(block[idx]):
        return None
    m = TIMESTAMP_RE.match(block[idx])
    start_ms = _ts_to_ms(*m.groups()[:4])
    end_ms = _ts_to_ms(*m.groups()[4:])
    lines = block[idx + 1:]
    raw = "\n".join(l for l in lines if l.strip() != "").strip()
    if cue_id is None:
        cue_id = str(ordinal)
    base = cue_id
    k = 1
    while cue_id in used_ids:
        cue_id = f"{base}~{k}"
        k += 1
    used_ids.add(cue_id)
    return ParsedCue(
        cue_id=cue_id, ord=ordinal, start_ms=start_ms, end_ms=end_ms,
        raw_text=raw, graphemes=count_graphemes(raw),
        raw_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def parse_subtitle(content: bytes | str, filename: str = "") -> list[ParsedCue]:
    """自动识别 SRT / WebVTT。返回保持文件顺序的条目列表。"""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    is_vtt = text.upper().startswith("WEBVTT") or filename.lower().endswith(".vtt")
    if is_vtt:
        # 去掉 WEBVTT 头（到第一个空行）
        nl = text.find("\n\n")
        if nl != -1 and text[:nl].lstrip().upper().startswith("WEBVTT"):
            text = text[nl + 2:]
    blocks = [b for b in re.split(r"\n{2,}", text) if b.strip()]
    cues: list[ParsedCue] = []
    used: set[str] = set()
    ordinal = 0
    for block in blocks:
        lines = block.split("\n")
        # VTT 中 NOTE/REGION/STYLE 块跳过
        if lines and lines[0].strip().upper().startswith(("NOTE", "REGION", "STYLE")):
            continue
        ordinal += 1
        cue = _parse_block(lines, ordinal, used)
        if cue is not None:
            cues.append(cue)
    return cues
