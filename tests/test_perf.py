"""更大规模与真实尺度的性能/正确性冒烟测试。"""
import random
import sys
import time

sys.path.insert(0, "/workspace")
from app.corrector import CueInput, Window, solve_run


def realistic(rng, n):
    cues = []
    t = rng.randint(0, 500)
    for i in range(n):
        dur = rng.randint(800, 4000)
        start = max(0, t + rng.randint(-400, 200))
        cues.append(CueInput(f"c{i:04d}", i + 1, start, start + dur,
                             rng.randint(5, 40)))
        t = start + dur
    shots = []
    s = 0
    while s < t + 2000:
        e = s + rng.randint(1000, 6000)
        shots.append((s, e))
        s = e + rng.randint(0, 500)
    return cues, shots, t + 2000


def main():
    rng = random.Random(42)
    for n in (50, 200, 500):
        for tol, gap in ((150, 40), (500, 80)):
            cues, shots, bound = realistic(rng, n)
            from app.corrector import shot_windows
            wins = [shot_windows(c, shots, tol, bound) for c in cues]
            t0 = time.time()
            res = solve_run(cues, wins, gap, bound)
            dt = time.time() - t0
            n_prop = len(res.proposals)
            n_chain = sum(len(ch.indexes) for ch in res.chains)
            # 校验提案满足约束
            starts = {i: res.proposals[i] for i in res.proposals}
            for i, ns in starts.items():
                assert any(w.lo <= ns <= w.hi for w in wins[i]), f"窗口违例 {i}"
                assert ns + cues[i].duration == (
                    ns + (cues[i].end_ms - cues[i].start_ms))
            for i in range(len(cues) - 1):
                if i in starts and i + 1 in starts:
                    assert starts[i] + cues[i].duration + gap <= starts[i + 1], \
                        f"间隔违例 {i}"
            chain = set()
            for ch in res.chains:
                chain.update(ch.indexes)
            assert n_prop == len(set(res.proposals))
            assert set(res.proposals).isdisjoint(chain)
            assert len(set(res.proposals) | chain) == n
            print(f"n={n:4d} tol={tol:4d} gap={gap:3d}  "
                  f"提案={n_prop:4d} 冲突链cue={n_chain:4d} 用时={dt:.2f}s")
            assert dt < 30, f"性能超限：{dt}s"
    print("performance smoke ok")


if __name__ == "__main__":
    main()
