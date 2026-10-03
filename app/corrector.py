"""确定性平移校正求解器。

约束（仅平移、保持原时长）：
  1. 相邻 cue 间隔：t[i-1] + dur[i-1] + min_gap <= t[i]（消除重叠与过小空隙）
  2. 每条 cue 必须整体落在某一个镜头内（边界容差 tolerance_ms；
     相接/重叠的镜头各自独立参与判断，不做合并）
  3. 无镜头窗口可容纳等属于“内在冲突”，仅平移不可解

择优（字典序，确定性）：
  ① 总移动毫秒 Σ|t - orig| 最少
  ② 发生移动的 cue 条数最少
  ③ cue_id 字典序：|偏移| 向量按 cue_id 升序字典序最小（靠前的 cue 优先零偏移）

方法：整数区间上的分段线性「后缀极小」DP。
F(x)=min{G(y):y≥x}，用整数断点表示，断点间为整数斜率直线；
RESTART 边表示 +∞ 可行域空隙。边标签供确定性回溯：
  follow = 包络跟随 G 的正斜率边（内部整数点 y=x 即最早最优）
  tie    = 包络恒等于 G 的水平边（内部整数点 y=x）
  carry  = 包络取右侧常值（内部整数点的取小者在右侧）
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

LINEAR = "linear"
RESTART = "restart"
FOLLOW = "follow"
TIE = "tie"
CARRY = "carry"


@dataclass
class CueInput:
    cue_id: str
    ord: int
    start_ms: int
    end_ms: int
    graphemes: int
    duration: int = field(init=False)

    def __post_init__(self) -> None:
        self.duration = self.end_ms - self.start_ms


@dataclass
class Window:
    lo: int
    hi: int


@dataclass
class Chain:
    indexes: list[int]
    reason: str


@dataclass
class SolveResult:
    proposals: dict[int, int]          # 下标 -> 新起点
    chains: list[Chain]
    intrinsic: dict[int, str]          # 下标 -> 内在冲突说明


@dataclass
class _Node:
    x: int
    v: int
    arg: int                 # F(x) 的最早取小 y


@dataclass
class _Edge:
    kind: str                # RESTART / FOLLOW / TIE / CARRY


@dataclass
class _Func:
    nodes: list[_Node]
    edges: list[_Edge]       # len = len(nodes)-1


def shot_windows(
    cue: CueInput, shots: list[tuple[int, int]], tol: int, bound: int
) -> list[Window]:
    """cue 可选取的起点窗口（整数闭区间）。无镜头数据时全域可行。

    每个镜头独立参与判断：相接（前镜终点等于后镜起点）或重叠的镜头
    一律不合并，cue 必须整体落在某一个镜头之内；仅把同一 cue 在不同
    镜头下得到的相交/相接起点窗口并成连续可行域（不改变可行集合，
    保证窗口 x 坐标单调，供 DP 的 RESTART 语义使用）。
    """
    if not shots:
        return [Window(0, max(0, bound - cue.duration))]
    windows: list[Window] = []
    for s, e in sorted(shots):
        if e - s < cue.duration:
            continue
        lo = max(s, cue.start_ms - tol)
        hi = min(e - cue.duration, cue.start_ms + tol)
        if lo <= hi:
            windows.append(Window(lo, hi))
    merged: list[Window] = []
    for w in windows:
        if merged and w.lo <= merged[-1].hi:
            merged[-1] = Window(merged[-1].lo, max(merged[-1].hi, w.hi))
        else:
            merged.append(w)
    return merged


# ---------- 分段线性函数运算 ----------

def _eval(f: _Func | None, x: int) -> int | None:
    """F(x)=min{G(y):y≥x}。

    - x 早于可行域：取最左分量最优
    - x 晚于可行域：None（后继无解）
    - 位于 RESTART 空隙内部：取右侧分量最优
    """
    if f is None:
        return 0
    if x > f.nodes[-1].x:
        return None
    if x < f.nodes[0].x:
        return f.nodes[0].v
    xs = [n.x for n in f.nodes]
    j = bisect_right(xs, x) - 1
    if x == f.nodes[j].x:
        return f.nodes[j].v
    x0, x1 = f.nodes[j].x, f.nodes[j + 1].x
    if f.edges[j].kind == RESTART:
        return f.nodes[j + 1].v
    num = (f.nodes[j + 1].v - f.nodes[j].v) * (x - x0)
    span = x1 - x0
    q, r = divmod(num, span)
    assert r == 0, f"整数点上的价值必须为整数（x={x}, {x0}->{x1}）"
    return f.nodes[j].v + q


def _suffix_min(g: _Func) -> _Func:
    """F(x)=min{G(y):y≥x}，同值取最小 y。

    输入边：LINEAR（整数斜率）或 RESTART（可行域空隙）。
    分两步：①每个连通分量内部自右向左做后缀极小；
    ②跨 RESTART 级联：左分量每点的 F 再与右域整体最优比较。
    """
    n = len(g.nodes)
    # 切分连通分量（按 RESTART 边）
    comps: list[tuple[int, int]] = []   # (start node idx, end node idx)
    start = 0
    for j, e in enumerate(g.edges):
        if e.kind == RESTART:
            comps.append((start, j))
            start = j + 1
    comps.append((start, n - 1))

    out_nodes: list[_Node] = []
    out_edges: list[_Edge] = []
    right_best: int | None = None
    right_arg: int | None = None

    for ci in range(len(comps) - 1, -1, -1):
        s, e = comps[ci]
        comp_nodes, comp_edges = _suffix_min_component(g, s, e)
        # comp_nodes/comp_edges 为逆序（右端点在前；边 k 连接 nodes[k] 与 nodes[k+1]）
        if right_best is None:
            chosen_nodes = list(comp_nodes)
            chosen_edges = list(comp_edges)
            best_node = min(comp_nodes, key=lambda nd: (nd.v, nd.arg))
            ba, barg = best_node.v, best_node.arg
        else:
            # 找到首个（逆序中最靠右的）F_local <= 右域最优的位置 p
            p = 0
            while p < len(comp_nodes) and (comp_nodes[p].v, comp_nodes[p].arg) > (
                right_best, right_arg
            ):
                p += 1
            chosen_nodes = list(comp_nodes)
            chosen_edges = list(comp_edges)
            if p < len(comp_nodes):
                # p 及其左侧采用 F_local；p 右侧采用右域常数
                for k in range(p):
                    chosen_nodes[k] = _Node(comp_nodes[k].x, right_best, right_arg)
                    chosen_edges[k] = _Edge(CARRY)
                # p 处若 F_local 等于右域，arg 取较早者；边 p-1 为常数 TIE/CARRY
                if p > 0:
                    nd = comp_nodes[p]
                    if nd.v == right_best:
                        chosen_nodes[p] = _Node(nd.x, nd.v, min(nd.arg, right_arg))
                    chosen_edges[p - 1] = _Edge(CARRY)
                ba, barg = chosen_nodes[-1].v, chosen_nodes[-1].arg
            else:
                # 整段被右域覆盖
                for k in range(len(comp_nodes)):
                    chosen_nodes[k] = _Node(comp_nodes[k].x, right_best, right_arg)
                for k in range(len(comp_edges)):
                    chosen_edges[k] = _Edge(CARRY)
                ba, barg = right_best, right_arg
        if out_nodes:
            out_edges.append(_Edge(RESTART))
        out_edges.extend(chosen_edges)
        out_nodes.extend(chosen_nodes)
        right_best, right_arg = ba, barg

    out_nodes.reverse()
    out_edges.reverse()
    return _Func(out_nodes, out_edges)


def _suffix_min_component(
    g: _Func, s: int, e: int
) -> tuple[list[_Node], list[_Edge]]:
    """对单个连通分量（节点 s..e，全 LINEAR 边）做后缀极小，返回逆序结果。"""
    out_nodes: list[_Node] = []
    out_edges: list[_Edge] = []
    best = g.nodes[e].v
    arg = g.nodes[e].x
    out_nodes.append(_Node(g.nodes[e].x, best, arg))

    for j in range(e - 1, s - 1, -1):
        x0, x1 = g.nodes[j].x, g.nodes[j + 1].x
        v0, v1 = g.nodes[j].v, g.nodes[j + 1].v
        span = x1 - x0
        d = v1 - v0
        if v0 >= best:
            if v0 == best and d == 0:
                edge_kind = TIE
                node_arg = x0
            else:
                edge_kind = CARRY
                node_arg = x0 if v0 == best else arg
            out_edges.append(_Edge(edge_kind))
            out_nodes.append(_Node(x0, best, node_arg))
            if v0 <= best:
                best, arg = v0, x0
            continue

        # v0 < best：G 上升穿 best
        assert d > 0, f"异常片段：v0={v0} v1={v1} best={best}"
        num = (best - v0) * span
        k = (num + d - 1) // d
        if k >= span:
            assert k == span
            out_nodes[-1].arg = x1
            out_edges.append(_Edge(FOLLOW))
            out_nodes.append(_Node(x0, v0, x0))
        else:
            cross = v0 + (d * k) // span
            assert cross >= best
            xk = x0 + k
            out_edges.append(_Edge(CARRY))
            out_nodes.append(_Node(xk, best, xk if cross == best else arg))
            out_edges.append(_Edge(FOLLOW))
            out_nodes.append(_Node(x0, v0, x0))
        best, arg = v0, x0
    return out_nodes, out_edges


def _argmin_suffix(f: _Func, x: int) -> int:
    """求使 F 最小（同值取最小 y）且 y≥x 的 y。"""
    xs = [n.x for n in f.nodes]
    if x <= xs[0]:
        return f.nodes[0].arg
    j = bisect_right(xs, x) - 1
    if x == xs[j]:
        return f.nodes[j].arg
    if j + 1 >= len(f.nodes):
        return f.nodes[j].arg
    edge = f.edges[j]
    if edge.kind == RESTART:
        # 查询点落在空隙中：取右侧分量的最优
        return f.nodes[j + 1].arg
    if edge.kind in (FOLLOW, TIE):
        return x
    return f.nodes[j + 1].arg

def _global_argmin(f: _Func) -> int:
    vmin = min(n.v for n in f.nodes)
    for n in f.nodes:
        if n.v == vmin:
            return n.arg
    raise AssertionError



# ---------- 主求解 ----------

def _edge_blocked(
    wins_a: list[Window], wins_b: list[Window], dur_a: int, gap: int
) -> bool:
    return wins_a[0].lo + dur_a + gap > wins_b[-1].hi





def solve_run(
    cues: list[CueInput],
    shots_list: list[list[Window]],
    gap: int,
    bound: int,
) -> SolveResult:
    n = len(cues)
    durations = [c.duration for c in cues]

    # ---- 可行性 DP（自右向左构建后缀价值函数）----
    # 每一层需要的权重仅用于价值比较，可行性只看层是否为空；
    # 用全长 m 的统一权重即可。
    w1, w2, coeffs0 = _weights(n, bound)
    order = sorted((c.cue_id, k) for k, c in enumerate(cues))
    coeff = {local_k: coeffs0[pos] for pos, (_cid, local_k) in enumerate(order)}
    feasible = [False] * n
    fnext: _Func | None = None
    for i in range(n - 1, -1, -1):
        if not shots_list[i]:
            fnext = None
            continue
        h = _h_layer(cues[i], shots_list[i], fnext, coeff[i], w1, w2, gap)
        if not h.nodes:
            fnext = None
            continue
        fnext = _suffix_min(h)
        feasible[i] = True

    # ---- 冲突链：不可行 cue 的连续段 ----
    intrinsic: dict[int, str] = {}
    for i, c in enumerate(cues):
        if not shots_list[i]:
            intrinsic[i] = (
                f"cue {c.cue_id}：时长 {c.duration}ms，在边界容差范围内没有任何镜头窗口"
                "可整体容纳，仅平移无法消除跨镜头切换"
            )

    blocked: set[tuple[int, int]] = set()
    for i in range(n - 1):
        if shots_list[i] and shots_list[i + 1] and _edge_blocked(
            shots_list[i], shots_list[i + 1], durations[i], gap
        ):
            blocked.add((i, i + 1))

    chains: list[Chain] = []
    i = 0
    while i < n:
        if feasible[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not feasible[j + 1]:
            j += 1
        members = list(range(i, j + 1))
        edges = [(a, b) for a, b in blocked if i <= a and b <= j]
        detail = "；".join(
            f"cue {cues[a].cue_id}→cue {cues[b].cue_id}：容差窗口内不存在满足最小间隔的取值组合"
            for a, b in edges
        )
        if not detail:
            bad = next((k for k in members if k in intrinsic), None)
            if bad is not None:
                detail = intrinsic[bad]
            else:
                detail = ("最小间隔与镜头窗口约束沿相邻 cue 传播后，"
                          "该连续段内找不到可同时满足的平移位置")
        chains.append(Chain(
            indexes=members,
            reason=(
                f"cue {cues[i].cue_id} … cue {cues[j].cue_id}"
                f"（共 {len(members)} 条）：{detail}"
            ),
        ))
        i = j + 1
    chains.sort(key=lambda ch: ch.indexes[0])

    # ---- 可解后缀段独立最优求解 ----
    proposals: dict[int, int] = {}
    i = 0
    while i < n:
        if not feasible[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and feasible[j + 1]:
            j += 1
        proposals.update(_dp_segment(cues, shots_list, gap, bound, i, j))
        i = j + 1

    return SolveResult(proposals=proposals, chains=chains, intrinsic=intrinsic)


def _weights(m: int, bound: int) -> tuple[int, int, dict[int, int]]:
    """①②权重与③超递增系数（段内按 cue_id 排位，位 0=字典序最前）。

    单条 |偏移| ∈ [0, bound]。超递增：c[pos]·bound > bound·Σ_{q>pos} c[q]。
    """
    base = bound + 1
    coeffs: dict[int, int] = {}
    tail_sum = 0
    for pos in range(m - 1, -1, -1):
        c = base * tail_sum + 1
        coeffs[pos] = c
        tail_sum += c
    tert_max = tail_sum * bound          # 三级项最大可能值
    w2 = tert_max + 1                     # 移动 1 条 > 任意三级差
    w1 = m * w2 + tert_max + 1           # 移动 1ms > 任意②③差
    return w1, w2, coeffs


def _h_layer(
    cue: CueInput, wins: list[Window], fnext: _Func | None,
    coeff: int, w1: int, w2: int, gap: int,
) -> _Func:
    """G(x)=局部代价 + F_{i+1}(x+dur+gap)。

    后缀函数对「落在空隙或早于可行域」的查询返回右侧分量最优；
    查询晚于可行域时返回 None。故单窗口内 x 连续可行（除非整体超出后继域），
    窗口之间为 RESTART。
    """
    delta = cue.duration + gap
    nodes: list[_Node] = []
    edges: list[_Edge] = []
    max_x = fnext.nodes[-1].x if fnext is not None else None
    for w in wins:
        hi = w.hi if max_x is None else min(w.hi, max_x - delta)
        if hi < w.lo:
            continue
        kinks = set(range(w.lo, hi + 1))
        if fnext is not None:
            for nd in fnext.nodes:
                xx = nd.x - delta
                if w.lo <= xx <= hi:
                    kinks.add(xx)
        for li, xx in enumerate(sorted(kinks)):
            gv = _eval(fnext, xx + delta) if fnext is not None else 0
            shift = abs(xx - cue.start_ms)
            val = gv + w1 * shift + coeff * shift
            if xx != cue.start_ms:
                val += w2
            if nodes:
                edges.append(_Edge(RESTART if li == 0 else LINEAR))
            nodes.append(_Node(xx, val, xx))
    return _compress(nodes, edges)


def _compress(nodes: list[_Node], edges: list[_Edge]) -> _Func:
    """合并斜率相同的相邻 LINEAR 单位边，保持精确；RESTART 原样保留。"""
    if len(nodes) <= 1:
        return _Func(nodes, edges)
    cn: list[_Node] = [nodes[0]]
    ce: list[_Edge] = []
    j = 0
    while j < len(edges):
        if edges[j].kind == RESTART:
            ce.append(edges[j])
            cn.append(nodes[j + 1])
            j += 1
            continue
        # 吞掉单位斜率相同的连续 LINEAR 边
        slope = nodes[j + 1].v - nodes[j].v
        k = j
        while (k < len(edges) and edges[k].kind == LINEAR
               and nodes[k + 1].v - nodes[k].v == slope):
            k += 1
        ce.append(_Edge(LINEAR))
        cn.append(nodes[k])
        j = k
    return _Func(cn, ce)


def _dp_segment(
    cues: list[CueInput],
    shots_list: list[list[Window]],
    gap: int,
    bound: int,
    lo_i: int, hi_i: int,
) -> dict[int, int]:
    m = hi_i - lo_i + 1
    w1, w2, coeffs0 = _weights(m, bound)
    order = sorted((c.cue_id, k) for k, c in enumerate(cues[lo_i:hi_i + 1]))
    coeff = {lo_i + local_k: coeffs0[pos]
             for pos, (_cid, local_k) in enumerate(order)}

    f_store: list[_Func] = []
    for idx in range(hi_i, lo_i - 1, -1):
        fnext = f_store[-1] if f_store else None
        h = _h_layer(cues[idx], shots_list[idx], fnext,
                     coeff[idx], w1, w2, gap)
        f_store.append(_suffix_min(h))
    f_store.reverse()

    result: dict[int, int] = {}
    x = _global_argmin(f_store[0])
    result[lo_i] = x
    for k in range(m - 1):
        idx = lo_i + k
        t_min = x + cues[idx].duration + gap
        x = _argmin_suffix(f_store[k + 1], t_min)
        result[lo_i + k + 1] = x
    return result
