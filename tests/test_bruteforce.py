"""暴力枚举校验求解器：随机小规模问题，比较 DP 与全枚举最优解。"""
import itertools
import random
import sys

sys.path.insert(0, "/workspace")
from app.corrector import CueInput, Window, solve_run


def brute(cues, windows_list, gap):
    m = len(cues)
    domains = [sorted({v for w in wins for v in range(w.lo, w.hi + 1)})
               for wins in windows_list]
    ids_sorted = sorted(range(m), key=lambda i: cues[i].cue_id)

    best_combo, best_key = None, None
    for combo in itertools.product(*domains):
        if any(combo[i] + cues[i].duration + gap > combo[i + 1]
               for i in range(m - 1)):
            continue
        total = sum(abs(combo[i] - cues[i].start_ms) for i in range(m))
        moved = sum(combo[i] != cues[i].start_ms for i in range(m))
        vec = tuple(abs(combo[ids_sorted[pos]] - cues[ids_sorted[pos]].start_ms)
                    for pos in range(m))
        key = (total, moved, vec)
        if best_key is None or key < best_key:
            best_key, best_combo = key, combo
    return best_combo, best_key


def dp_solve(cues, windows_list, gap, bound):
    res = solve_run(cues, windows_list, gap, bound)
    m = len(cues)
    if len(res.proposals) < m:
        return None, None
    combo = tuple(res.proposals[i] for i in range(m))
    total = sum(abs(combo[i] - cues[i].start_ms) for i in range(m))
    moved = sum(combo[i] != cues[i].start_ms for i in range(m))
    ids_sorted = sorted(range(m), key=lambda i: cues[i].cue_id)
    vec = tuple(abs(combo[ids_sorted[pos]] - cues[ids_sorted[pos]].start_ms)
                for pos in range(m))
    return combo, (total, moved, vec)


def random_case(rng):
    n = rng.randint(1, 3)
    # 小坐标宇宙 0..40
    cues = []
    for i in range(n):
        start = rng.randint(2, 30)
        dur = rng.randint(1, 12)
        cid = rng.choice([f"a{i}", f"b{i}", f"z{i}", f"m{i}"]) + f"#{i}"
        cues.append(CueInput(cid, i + 1, start, start + dur, rng.randint(1, 20)))
    windows_list = []
    for c in cues:
        nwin = rng.randint(1, 2)
        wins = []
        for _ in range(nwin):
            lo = rng.randint(0, 25)
            hi = lo + rng.randint(0, 8)
            wins.append(Window(lo, hi))
        # 合并重叠
        wins.sort(key=lambda w: (w.lo, w.hi))
        merged = []
        for w in wins:
            if merged and w.lo <= merged[-1].hi:
                merged[-1] = Window(merged[-1].lo, max(merged[-1].hi, w.hi))
            else:
                merged.append(w)
        windows_list.append(merged)
    gap = rng.choice([0, 1, 2, 4])
    return cues, windows_list, gap, 100


def main():
    rng = random.Random(20260922)
    trials = 5000
    fails = 0
    for trial in range(trials):
        cues, wins, gap, bound = random_case(rng)
        exp_combo, exp_key = brute(cues, wins, gap)
        got_combo, got_key = dp_solve(cues, wins, gap, bound)
        if exp_combo is None:
            if got_combo is not None:
                fails += 1
                print(f"[FALSE-FEASIBLE] trial {trial}")
                print("  cues:", [(c.cue_id, c.start_ms, c.duration) for c in cues])
                print("  wins:", [[(w.lo, w.hi) for w in ww] for ww in wins],
                      "gap", gap, "got", got_combo)
                if fails >= 10:
                    break
            continue
        if got_combo != exp_combo:
            fails += 1
            print(f"[FAIL] trial {trial}")
            print("  cues:", [(c.cue_id, c.start_ms, c.duration) for c in cues])
            print("  wins:", [[(w.lo, w.hi) for w in ww] for ww in ww] if False else [[(w.lo, w.hi) for w in ww] for ww in wins])
            print("  expected", exp_combo, exp_key)
            print("  got     ", got_combo, got_key)
            if fails >= 10:
                break
    print(f"done: {trials} trials, {fails} failures")


if __name__ == "__main__":
    main()
