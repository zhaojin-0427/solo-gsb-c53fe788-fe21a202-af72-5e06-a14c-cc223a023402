"""相接镜头切点回归测试。

场景：镜头 [0,1200) 与 [1200,2400) 相接于 1200ms 切点，字幕 900–1300ms。
- 每个镜头独立参与判断，相接镜头不合并；
- 原字幕跨越切点即标记跨镜头，不受平移容差影响；
- 恰在切点结束（800–1200）或开始（1200–1500）视为合法；
- 容差 200ms：提案 800–1200ms（恰在切点结束，允许）；
- 容差 50ms：无可采纳提案，列出冲突链与内在冲突。
"""
import sys

sys.path.insert(0, "/workspace")

from app.analysis import build_analysis
from app.corrector import CueInput, shot_windows
from app.shots import Shot
from app.subtitles import ParsedCue

SHOTS_RAW = [(0, 1200), (1200, 2400)]


def _cue(cid, ord_, s, e):
    return ParsedCue(cue_id=cid, ord=ord_, start_ms=s, end_ms=e,
                     raw_text="x", graphemes=1, raw_hash="h")


def _shots():
    return [Shot(ord=1, start_ms=0, end_ms=1200),
            Shot(ord=2, start_ms=1200, end_ms=2400)]


def test_windows_respect_touching_cut():
    """相接镜头不合并：900–1300（时长 400，容差 200）只有 [700,800]。"""
    c = CueInput("c1", 1, 900, 1300, 1)
    wins = shot_windows(c, SHOTS_RAW, 200, 10000)
    assert [(w.lo, w.hi) for w in wins] == [(700, 800)], wins


def test_cross_marked_and_proposal_800_1200():
    """容差 200ms：标记跨镜头，提案 800–1200ms，无冲突链。"""
    res = build_analysis([_cue("c1", 1, 900, 1300)], _shots(), 21.0, 40, 200)
    rep = res.cues[0]
    assert rep.cross_shot, "900–1300 跨越 1200ms 切点，必须标记跨镜头"
    assert rep.proposal_start_ms == 800, rep.proposal_start_ms
    assert rep.proposal_end_ms == 1200, rep.proposal_end_ms
    assert not res.chains and not res.intrinsic
    assert res.stats["cross_shot_count"] == 1


def test_tolerance_50_no_proposal_but_chain():
    """容差 50ms：无可采纳提案，列出冲突链与内在冲突。"""
    res = build_analysis([_cue("c1", 1, 900, 1300)], _shots(), 21.0, 40, 50)
    rep = res.cues[0]
    assert rep.cross_shot
    assert rep.proposal_start_ms is None
    assert rep.proposal_end_ms is None
    assert len(res.chains) == 1 and res.chains[0]["indexes"] == [0]
    assert len(res.intrinsic) == 1 and res.intrinsic[0]["cue_id"] == "c1"


def test_exact_cut_boundaries_allowed():
    """恰在切点结束（800–1200）或开始（1200–1500）不标记跨镜头。"""
    res = build_analysis(
        [_cue("a", 1, 800, 1200), _cue("b", 2, 1200, 1500)],
        _shots(), 21.0, 0, 200)
    assert not res.cues[0].cross_shot
    assert not res.cues[1].cross_shot


def test_cross_mark_independent_of_tolerance():
    """跨镜头标记不受平移容差影响：容差 0 时依然标记。"""
    res = build_analysis([_cue("c1", 1, 900, 1300)], _shots(), 21.0, 40, 0)
    assert res.cues[0].cross_shot
    assert res.cues[0].proposal_start_ms is None


def main():
    test_windows_respect_touching_cut()
    test_cross_marked_and_proposal_800_1200()
    test_tolerance_50_no_proposal_but_chain()
    test_exact_cut_boundaries_allowed()
    test_cross_mark_independent_of_tolerance()
    print("shot boundary regression ok")


if __name__ == "__main__":
    main()
