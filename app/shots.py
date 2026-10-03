"""镜头 JSON 解析。

支持的输入形态：
- [{"start_ms": 0, "end_ms": 1200}, ...]
- {"shots": [...]}
条目内同时兼容 start/end、in/out、start_ms/end_ms 等命名（单位统一为毫秒整数）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class Shot:
    ord: int
    start_ms: int
    end_ms: int


_ALIASES = (
    ("start_ms", "end_ms"),
    ("start", "end"),
    ("in_ms", "out_ms"),
    ("in", "out"),
    ("begin_ms", "end_ms"),
)


def _pick(d: dict, names: tuple[str, ...]) -> int | None:
    for n in names:
        if n in d:
            return int(d[n])
    return None


def parse_shots(content: bytes | str) -> list[Shot]:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    data = json.loads(content)
    if isinstance(data, dict) and "shots" in data:
        data = data["shots"]
    if not isinstance(data, list):
        raise ValueError("镜头 JSON 必须是数组或含 shots 数组的对象")
    shots: list[Shot] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 个镜头不是对象")
        start = _pick(item, tuple(a[0] for a in _ALIASES))
        end = _pick(item, tuple(a[1] for a in _ALIASES))
        if start is None or end is None:
            raise ValueError(f"第 {i} 个镜头缺少起止毫秒字段")
        if end <= start:
            raise ValueError(f"第 {i} 个镜头时长必须为正（{start}→{end}）")
        shots.append(Shot(ord=i, start_ms=start, end_ms=end))
    shots.sort(key=lambda s: s.start_ms)
    return shots
