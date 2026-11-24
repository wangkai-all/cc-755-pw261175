"""Exact dynamic programme over the component's position sweep.

State at a cut is the orientation (side) of every fragment that is *alive*
at the cut: it has already started and will be seen again later.  Fragments
with contiguous or nearly-contiguous coverage therefore give a very small
state space, while still enforcing that each fragment's orientation is one
shared decision across all its positions.

The DP is exact.  It also yields:

* the number of optimal orientation vectors (big integer, exact),
* for every position, every haplotype allele pair that occurs in *some*
  optimal solution (forward/backward pass),
* a bounded list of distinct optimal reconstructions for display.

If the state space at any cut exceeds the cap, the function returns ``None``
so the caller can fall back to bounds plus a heuristic incumbent.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .base import Component

Pair = Tuple[int, int]
INF = 10**9
MAX_VARIANTS = 200
MAX_PATHS_EXAMINED = 4000


@dataclass
class DPOutcome:
    cost: int
    bits: List[int]
    n_opt_vectors: int
    opt_bits: List[Tuple[int, ...]]
    pair_options: Dict[int, List[Pair]]
    max_states: int
    transitions: int
    variants_hit_cap: bool


def _pos_pair_costs(pm, allowed, s0, k0, s1, k1) -> List[Tuple[Pair, int]]:
    out = []
    for a, b in allowed:
        m0 = k0 if a == 0 else s0 - k0
        m1 = k1 if b == 0 else s1 - k1
        out.append(((a, b), m0 + m1))
    return out


def solve_dp(
    comp: Component,
    deadline: float,
    max_states: int = 1 << 17,
    max_transitions: int = 8_000_000,
) -> Optional[DPOutcome]:
    t0 = time.perf_counter()
    positions = comp.positions
    m = len(positions)
    frags = comp.fragments
    n = len(frags)
    if n == 0 or m == 0:
        return None
    pindex = {p: i for i, p in enumerate(positions)}

    first = [0] * n
    last = [0] * n
    for fi, f in enumerate(frags):
        idxs = sorted(pindex[p] for p in f.alleles)
        first[fi] = idxs[0]
        last[fi] = idxs[-1]

    new_at: List[List[int]] = [[] for _ in range(m)]
    for fi in range(n):
        new_at[first[fi]].append(fi)

    # alive at cut i = fragments with first <= i < last ; alive_cut[-1] = []
    alive_cut: List[List[int]] = []
    prev_alive: List[int] = []
    for i in range(m):
        cur = sorted(set(prev_alive) | set(new_at[i]))
        cur = [f for f in cur if last[f] > i]
        alive_cut.append(cur)
        prev_alive = cur

    # observers per position, split into "already alive before i" and "new at i"
    obs_prev: List[List[Tuple[int, int]]] = []
    obs_new: List[List[Tuple[int, int]]] = []
    for i in range(m):
        alive_before = alive_cut[i - 1] if i > 0 else []
        pos_of_prev = {f: j for j, f in enumerate(alive_before)}
        pos_of_new = {f: j for j, f in enumerate(new_at[i])}
        ep: List[Tuple[int, int]] = []
        en: List[Tuple[int, int]] = []
        for p, a in frags_obs_at(comp, positions[i]):
            if p in pos_of_prev:
                ep.append((pos_of_prev[p], a))
            else:
                en.append((pos_of_new[p], a))
        obs_prev.append(ep)
        obs_new.append(en)

    allowed = [comp.allowed[p] for p in positions]

    # ---------------- forward pass ----------------
    layers: List[Dict[Tuple[int, ...], Tuple[int, int, Optional[Tuple[int, ...]], Optional[Tuple[int, ...]]]]] = []
    layers.append({(): (0, 1, None, None)})
    max_seen = 1
    transitions = 0
    for i in range(m):
        alive_before = alive_cut[i - 1] if i > 0 else []
        alive_next = alive_cut[i]
        new = new_at[i]
        n_new = len(new)
        next_pos_of = {f: j for j, f in enumerate(alive_next)}
        prev_layer = layers[-1]
        cur: Dict[Tuple[int, ...], Tuple[int, int, Optional[Tuple[int, ...]], Optional[Tuple[int, ...]]]] = {}
        ep = obs_prev[i]
        en = obs_new[i]
        pairs = allowed[i]
        for s, (cost, cnt, _, _) in prev_layer.items():
            for bits_new in range(1 << n_new):
                s0 = k0 = s1 = k1 = 0
                for j, a in ep:
                    if s[j] == 0:
                        s0 += 1
                        k0 += a
                    else:
                        s1 += 1
                        k1 += a
                for j, a in en:
                    if (bits_new >> j) & 1:
                        s1 += 1
                        k1 += a
                    else:
                        s0 += 1
                        k0 += a
                best = INF
                for a, b in pairs:
                    m0 = k0 if a == 0 else s0 - k0
                    m1 = k1 if b == 0 else s1 - k1
                    c = m0 + m1
                    if c < best:
                        best = c
                transitions += 1
                newc = cost + best
                ns = tuple(
                    ((bits_new >> new.index(f)) & 1) if first[f] == i else s[alive_before.index(f)]
                    for f in alive_next
                )
                entry = cur.get(ns)
                if entry is None or newc < entry[0]:
                    cur[ns] = (newc, cnt, s, bits_new)
                elif newc == entry[0]:
                    cur[ns] = (entry[0], entry[1] + cnt, entry[2], entry[3])
        if len(cur) > max_states:
            return None
        max_seen = max(max_seen, len(cur))
        if transitions > max_transitions:
            return None
        if time.perf_counter() > deadline:
            return None
        layers.append(cur)

    final = layers[-1]
    if () not in final:
        return None
    opt, n_opt_vectors = final[()][0], final[()][1]

    # ---------------- backward pass ----------------
    # b_cut[i][s] = min cost of positions i+1..m-1 given state s at cut i.
    # b_cut[m-1] is the sentinel for "after the last position" and must stay
    # {(): 0}; the i == 0 iteration computes the whole-path suffix and is used
    # only for the consistency check below.
    b_cut: List[Dict[Tuple[int, ...], int]] = [{} for _ in range(m)]
    b_cut[m - 1] = {(): 0}
    full_suffix: Optional[int] = None
    for i in range(m - 1, -1, -1):
        alive_before = alive_cut[i - 1] if i > 0 else []
        alive_next = alive_cut[i]
        new = new_at[i]
        n_new = len(new)
        pairs = allowed[i]
        ep = obs_prev[i]
        en = obs_new[i]
        nb = b_cut[i]
        cur_b: Dict[Tuple[int, ...], int] = {}
        for s in layers[i].keys():
            best_b = INF
            for bits_new in range(1 << n_new):
                s0 = k0 = s1 = k1 = 0
                for j, a in ep:
                    if s[j] == 0:
                        s0 += 1
                        k0 += a
                    else:
                        s1 += 1
                        k1 += a
                for j, a in en:
                    if (bits_new >> j) & 1:
                        s1 += 1
                        k1 += a
                    else:
                        s0 += 1
                        k0 += a
                c = INF
                for a, b in pairs:
                    m0 = k0 if a == 0 else s0 - k0
                    m1 = k1 if b == 0 else s1 - k1
                    v = m0 + m1
                    if v < c:
                        c = v
                ns = tuple(
                    ((bits_new >> new.index(f)) & 1) if first[f] == i else s[alive_before.index(f)]
                    for f in alive_next
                )
                tail = nb.get(ns)
                if tail is None:
                    continue
                if c + tail < best_b:
                    best_b = c + tail
            if best_b < INF:
                cur_b[s] = best_b
        if i == 0:
            # Prefix cost is empty, so this is the cost of the whole path.
            full_suffix = cur_b.get((), INF)
        else:
            b_cut[i - 1] = cur_b
        if time.perf_counter() > deadline:
            return None
    if full_suffix != opt:
        # Forward/backward disagree: should not happen; refuse to report.
        return None

    # ---------------- per-position allele-pair ambiguity ----------------
    pair_options: Dict[int, List[Pair]] = {}
    for i in range(m):
        alive_before = alive_cut[i - 1] if i > 0 else []
        alive_next = alive_cut[i]
        new = new_at[i]
        n_new = len(new)
        ep = obs_prev[i]
        en = obs_new[i]
        nb = b_cut[i]
        fwd = layers[i]
        good: List[Pair] = []
        for (a, b) in allowed[i]:
            found = False
            for s, (f_cost, _cnt, _, _) in fwd.items():
                if found:
                    break
                for bits_new in range(1 << n_new):
                    s0 = k0 = s1 = k1 = 0
                    for j, al in ep:
                        if s[j] == 0:
                            s0 += 1
                            k0 += al
                        else:
                            s1 += 1
                            k1 += al
                    for j, al in en:
                        if (bits_new >> j) & 1:
                            s1 += 1
                            k1 += al
                        else:
                            s0 += 1
                            k0 += al
                    m0 = k0 if a == 0 else s0 - k0
                    m1 = k1 if b == 0 else s1 - k1
                    ns = tuple(
                        ((bits_new >> new.index(f)) & 1) if first[f] == i else s[alive_before.index(f)]
                        for f in alive_next
                    )
                    tail = nb.get(ns)
                    if tail is None:
                        continue
                    if f_cost + m0 + m1 + tail == opt:
                        found = True
                        break
            if found:
                good.append((a, b))
        if len(good) > 1:
            pair_options[positions[i]] = good

    # ---------------- enumerate a bounded set of optimal solutions --------
    # Depth-first over optimal edges.  The running prefix cost must come from the
    # path taken, not from layers[i][s] (the cheapest prefix reaching s): two
    # different prefixes can share a state, and only the actual one decides
    # whether the rest of the path can still add up to the optimum.
    opt_bits: List[Tuple[int, ...]] = []
    truncated = False
    examined = 0

    def dfs(i: int, s: Tuple[int, ...], prefix_cost: int, acc: List[Tuple[int, int]]):
        nonlocal truncated, examined
        if truncated:
            return
        if i == m:
            bits = [0] * n
            for fi, side in acc:
                bits[fi] = side
            if len(opt_bits) < MAX_VARIANTS:
                opt_bits.append(tuple(bits))
            else:
                truncated = True
            return
        examined += 1
        if examined > MAX_PATHS_EXAMINED:
            truncated = True
            return
        alive_before = alive_cut[i - 1] if i > 0 else []
        alive_next = alive_cut[i]
        new = new_at[i]
        ep = obs_prev[i]
        en = obs_new[i]
        nb = b_cut[i]
        for bits_new in range(1 << len(new)):
            s0 = k0 = s1 = k1 = 0
            for j, al in ep:
                if s[j] == 0:
                    s0 += 1
                    k0 += al
                else:
                    s1 += 1
                    k1 += al
            for j, al in en:
                if (bits_new >> j) & 1:
                    s1 += 1
                    k1 += al
                else:
                    s0 += 1
                    k0 += al
            c = INF
            for a, b in allowed[i]:
                m0 = k0 if a == 0 else s0 - k0
                m1 = k1 if b == 0 else s1 - k1
                v = m0 + m1
                if v < c:
                    c = v
            ns = tuple(
                ((bits_new >> new.index(f)) & 1) if first[f] == i else s[alive_before.index(f)]
                for f in alive_next
            )
            tail = nb.get(ns)
            if tail is None or prefix_cost + c + tail != opt:
                continue
            add = [(f, (bits_new >> j) & 1) for j, f in enumerate(new)]
            dfs(i + 1, ns, prefix_cost + c, acc + add)
            if truncated:
                return

    dfs(0, (), 0, [])
    if not opt_bits:
        # Reconstruct at least one path from the stored backpointers.
        bits = [0] * n
        s = ()
        for i in range(m, 0, -1):
            entry = layers[i][s]
            _cost, _cnt, prev_s, bits_new = entry
            if prev_s is None:
                continue
            new = new_at[i - 1]
            for j, f in enumerate(new):
                bits[f] = (bits_new >> j) & 1
            s = prev_s
        opt_bits.append(tuple(bits))

    return DPOutcome(
        cost=opt,
        bits=list(opt_bits[0]) if opt_bits else [0] * n,
        n_opt_vectors=n_opt_vectors,
        opt_bits=opt_bits,
        pair_options=pair_options,
        max_states=max_seen,
        transitions=transitions,
        variants_hit_cap=truncated,
    )


def frags_obs_at(comp: Component, p: int) -> List[Tuple[int, int]]:
    out = []
    for fi, f in enumerate(comp.fragments):
        if p in f.alleles:
            out.append((fi, f.alleles[p]))
    return out
