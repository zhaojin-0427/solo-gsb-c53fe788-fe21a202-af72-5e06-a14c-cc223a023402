"""导入解析 → 冲突检测 → 校正提案 的编排层。"""
from __future__ import annotations

from dataclasses import dataclass

from .corrector import CueInput, shot_windows, solve_run
from .shots import Shot
from .subtitles import visible_text


@dataclass
class CueReport:
    uid: int
    cue_id: str
    ord: int
    start_ms: int
    end_ms: int
    duration_ms: int
    text: str
    graphemes: int
    raw_hash: str
    cps: float
    cps_ok: bool
    overlap_with: str | None = None      # 与前一条重叠/间隔不足
    cross_shot: bool = False             # 当前（原）位置跨镜头
    proposal_start_ms: int | None = None
    proposal_end_ms: int | None = None
    adopted: bool = False
    adopted_start_ms: int | None = None
    adopted_end_ms: int | None = None


@dataclass
class AnalysisResult:
    cues: list[CueReport]
    shots: list[tuple[int, int]]
    chains: list[dict]
    intrinsic: list[dict]
    stats: dict


def _cps(graphemes: int, duration_ms: int) -> float:
    if duration_ms <= 0:
        return float("inf")
    return graphemes * 1000.0 / duration_ms


def build_analysis(
    parsed: list[ParsedCue],
    shots_raw: list[Shot],
    cps_limit: float,
    min_gap_ms: int,
    tolerance_ms: int,
) -> AnalysisResult:
    shots = [(s.start_ms, s.end_ms) for s in shots_raw]
    bound = max(
        [c.end_ms for c in parsed] + [e for _, e in shots] + [1000]
    ) + tolerance_ms + 1000

    cue_inputs = [
        CueInput(c.cue_id, c.ord, c.start_ms, c.end_ms, c.graphemes)
        for c in parsed
    ]
    windows = [shot_windows(c, shots, tolerance_ms, bound) for c in cue_inputs]
    res = solve_run(cue_inputs, windows, min_gap_ms, bound)

    reports: list[CueReport] = []
    for i, pc in enumerate(parsed):
        ci = cue_inputs[i]
        dur = pc.end_ms - pc.start_ms
        cps = _cps(pc.graphemes, dur)
        overlap_with = None
        if i > 0:
            prev = parsed[i - 1]
            actual_gap = pc.start_ms - prev.end_ms
            if actual_gap < min_gap_ms:
                kind = "重叠" if actual_gap < 0 else "间隔不足"
                overlap_with = (
                    f"{kind}：与 {prev.cue_id} 的间隔为 {actual_gap}ms，"
                    f"小于最小间隔 {min_gap_ms}ms"
                )
        # 原位置是否整段属于某一镜头：跨越切点即标记跨镜头，与平移容差
        # 无关；恰好始于/止于切点（端点闭合包含）视为合法。
        cross = bool(shots) and not any(
            s <= pc.start_ms and pc.end_ms <= e for s, e in shots
        )
        prop = res.proposals.get(i)
        reports.append(CueReport(
            uid=0,  # 由调用方（DB id）填充
            cue_id=pc.cue_id,
            ord=pc.ord,
            start_ms=pc.start_ms,
            end_ms=pc.end_ms,
            duration_ms=dur,
            text=visible_text(pc.raw_text),
            graphemes=pc.graphemes,
            raw_hash=pc.raw_hash,
            cps=round(cps, 2),
            cps_ok=cps <= cps_limit and dur > 0,
            overlap_with=overlap_with,
            cross_shot=cross,
            proposal_start_ms=prop,
            proposal_end_ms=(prop + dur) if prop is not None else None,
        ))

    chains = [{
        "cue_ids": [parsed[i].cue_id for i in ch.indexes],
        "indexes": ch.indexes,
        "reason": ch.reason,
    } for ch in res.chains]
    intrinsic = [{
        "cue_id": parsed[i].cue_id,
        "reason": reason,
    } for i, reason in sorted(res.intrinsic.items())]

    bad_gap = sum(1 for r in reports if r.overlap_with)
    bad_cps = sum(1 for r in reports if not r.cps_ok)
    moved = sum(1 for r in reports if r.proposal_start_ms is not None
                and r.proposal_start_ms != r.start_ms)
    stats = {
        "cue_count": len(reports),
        "shot_count": len(shots),
        "gap_violations": bad_gap,
        "cps_violations": bad_cps,
        "cross_shot_count": sum(1 for r in reports if r.cross_shot),
        "proposal_count": len(res.proposals),
        "proposal_moved_count": moved,
        "conflict_chain_count": len(chains),
        "conflict_cue_count": sum(len(c["indexes"]) for c in chains),
        "total_shift_ms": sum(
            abs(reports[i].proposal_start_ms - cue_inputs[i].start_ms)
            for i in res.proposals
        ),
    }
    return AnalysisResult(
        cues=reports, shots=shots, chains=chains,
        intrinsic=intrinsic, stats=stats,
    )


def revalidate_adoptions(
    cue_rows: list,
    shots: list[tuple[int, int]],
    tolerance_ms: int,
    min_gap_ms: int,
    proposals: dict[str, tuple[int, int] | None] | None = None,
) -> list[int]:
    """用当前参数重新核验每条采纳记录，返回应撤销的 cue_uid 列表。

    每条采纳 (new_start_ms, new_end_ms) 必须同时满足：
      1. 仅平移：新时长与原时长完全一致；
      2. 容差：|新起点 − 原起点| ≤ tolerance_ms；
      3. 镜头边界：整体落在某一个镜头之内（无镜头数据时起点非负即可）；
      4. 最小间隔：与相邻条目的「生效位置」之间保持 min_gap_ms。生效位置
         与导出 SRT 的有效时间轴一致：已采纳取采纳值，未采纳先取当前参数
         下 build_analysis 的新提案，无提案再取原值（按 cue_id 对应，
         proposals 缺省/缺失时回退到原值）。
    第 4 条相互依赖：撤销一条会改变其邻居的生效位置，因此按轮次核验，
    每轮撤销全部当前失效的采纳，直至剩余采纳均满足约束（不动点）。
    """
    proposals = proposals or {}
    adopted = {
        r["id"]: (r["new_start_ms"], r["new_end_ms"])
        for r in cue_rows if r["new_start_ms"] is not None
    }

    def effective(r) -> tuple[int, int]:
        pos = adopted.get(r["id"])
        if pos is not None:
            return pos
        prop = proposals.get(r["cue_id"])
        if prop is not None:
            return prop
        return (r["start_ms"], r["end_ms"])

    revoked: list[int] = []
    while True:
        eff = [effective(r) for r in cue_rows]
        bad: list[int] = []
        for i, r in enumerate(cue_rows):
            pos = adopted.get(r["id"])
            if pos is None:
                continue
            ns, ne = pos
            dur = r["end_ms"] - r["start_ms"]
            ok = (
                ne - ns == dur
                and abs(ns - r["start_ms"]) <= tolerance_ms
                and (any(s <= ns and ne <= e for s, e in shots)
                     if shots else ns >= 0)
                and (i == 0 or ns - eff[i - 1][1] >= min_gap_ms)
                and (i + 1 == len(cue_rows)
                     or eff[i + 1][0] - ne >= min_gap_ms)
            )
            if not ok:
                bad.append(r["id"])
        if not bad:
            return revoked
        for uid in bad:
            del adopted[uid]
            revoked.append(uid)
