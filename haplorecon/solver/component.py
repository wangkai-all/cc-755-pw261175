"""Per-component optimisation.

The only decision inside a component is the binary orientation of each
fragment (which latent sequence it came from).  Given an orientation vector,
each position's two alleles are chosen independently from the pairs that locks
allow, so all observations of a fragment are scored together -- never column
by column.

Engines, in order of preference
-------------------------------
``sweep-dp``
    Exact dynamic programme over position order.  The state is the orientation
    of every fragment still "alive" at the current cut.  For fragments with
    local coverage this state stays tiny, so the DP is exact on large inputs.
    It also yields the exact number of optimal orientation vectors and, for
    every position, every allele pair occurring in some optimal solution.

``enumeration``
    Gray-code walk over all 2^n orientation vectors, used for very small
    components when the DP's state cap is exceeded (dense overlap).

``branch-and-bound`` (fallback, may time out)
    Local-search incumbent plus depth-first search with an admissible bound.
    A chunked relaxation gives a proven additive lower bound, so a result can
    still be certified optimal when the bound meets the incumbent.  If the
    budget runs out the status is ``timeout`` and bounds are reported.
"""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .base import Component, PositionModel
from .dp import solve_dp

Pair = Tuple[int, int]
INF = 10**9
MAX_VARIANTS = 200
ENUM_MAX_N = 18
DP_MAX_STATES = 1 << 15
DP_MAX_TRANSITIONS = 6_000_000
CHUNK_WIDTH = 12


@dataclass
class CompSolution:
    index: int
    positions: List[int]
    status: str  # "optimal" | "timeout"
    proven: bool
    cost: int
    lower_bound: int
    upper_bound: int
    orientation: Dict[str, int]
    hap0: Dict[int, int]
    hap1: Dict[int, int]
    errors: Dict[str, List[int]]
    n_optimal_vectors: Optional[int]
    n_orientation_classes: Optional[int]
    n_palindromic: int
    classes_truncated: bool
    ambiguity_known: bool
    ambiguous_positions: List[int]
    pair_options: Dict[int, List[List[int]]]
    variants: List[Dict[str, object]]
    variants_truncated: bool
    phase: Dict[str, object]
    elapsed_ms: int
    nodes: int
    engine: str
    engine_note: str
    order_pinned: bool
    flip_free: bool


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------

def _flatten(comp: Component):
    pindex = {p: i for i, p in enumerate(comp.positions)}
    return [[(pindex[p], a) for p, a in sorted(f.alleles.items())] for f in comp.fragments]


def _counts(comp: Component, frag_entries, bits: Sequence[int]):
    npos = len(comp.positions)
    s = [0] * npos
    k = [0] * npos
    for i, entries in enumerate(frag_entries):
        if bits[i] == 1:
            continue
        for pidx, a in entries:
            s[pidx] += 1
            if a == 1:
                k[pidx] += 1
    return s, k


def _position_cost(pm: PositionModel, s: int, k: int, a: int, b: int) -> int:
    m0 = k if a == 0 else s - k
    k1 = pm.ones - k
    m1 = k1 if b == 0 else (pm.t - s) - k1
    return m0 + m1


def _eval_bits(comp: Component, bits: Sequence[int]):
    """Cost, canonical allele pair per position, and all optimal pairs."""
    frag_entries = _flatten(comp)
    s, k = _counts(comp, frag_entries, bits)
    total = 0
    pairs: List[Pair] = []
    options: List[List[Pair]] = []
    for j, pm in enumerate(comp.pos_models):
        best = INF
        opts: List[Pair] = []
        for a, b in comp.allowed[pm.position]:
            c = _position_cost(pm, s[j], k[j], a, b)
            if c < best:
                best = c
                opts = [(a, b)]
            elif c == best:
                opts.append((a, b))
        total += best
        pairs.append(min(opts))
        options.append(opts)
    return total, pairs, options


def _errors_for(comp: Component, bits, hap0, hap1) -> Dict[str, List[int]]:
    errors: Dict[str, List[int]] = {}
    for i, f in enumerate(comp.fragments):
        side = hap0 if bits[i] == 0 else hap1
        bad = [p for p, a in sorted(f.alleles.items()) if side.get(p) != a]
        if bad:
            errors[f.fid] = bad
    return errors


def _flip_symmetric(comp: Component) -> bool:
    for p in comp.positions:
        pairs = set(comp.allowed[p])
        if pairs != {(b, a) for a, b in pairs}:
            return False
    return True


def _canonical(comp: Component, bits, pairs, order_pinned: bool):
    bits = list(bits)
    hap0 = {p: pairs[j][0] for j, p in enumerate(comp.positions)}
    hap1 = {p: pairs[j][1] for j, p in enumerate(comp.positions)}
    if order_pinned:
        return bits, hap0, hap1
    seq0 = tuple(hap0[p] for p in comp.positions)
    seq1 = tuple(hap1[p] for p in comp.positions)
    if seq1 < seq0:
        bits = [1 - b for b in bits]
        hap0, hap1 = hap1, hap0
    return bits, hap0, hap1


# ---------------------------------------------------------------------------
# Exact enumeration (tiny components)
# ---------------------------------------------------------------------------

def enumerate_exact(comp: Component, deadline: float):
    n = len(comp.fragments)
    if n > ENUM_MAX_N:
        return None
    npos = len(comp.positions)
    frag_entries = _flatten(comp)
    tables = [pm.table for pm in comp.pos_models]
    t = [pm.t for pm in comp.pos_models]
    bits = [0] * n
    s, k = _counts(comp, frag_entries, bits)
    cost = [tables[j][s[j] * (t[j] + 1) + k[j]] for j in range(npos)]
    total = sum(cost)
    best = total
    best_bits = list(bits)
    n_opt = 1
    opt_bits: List[Tuple[int, ...]] = [tuple(bits)]
    for g in range(1, 1 << n):
        i = (g & -g).bit_length() - 1
        bits[i] ^= 1
        delta = -1 if bits[i] == 1 else 1
        for pidx, a in frag_entries[i]:
            s[pidx] += delta
            if a == 1:
                k[pidx] += delta
            newc = tables[pidx][s[pidx] * (t[pidx] + 1) + k[pidx]]
            total += newc - cost[pidx]
            cost[pidx] = newc
        if total < best:
            best = total
            best_bits = list(bits)
            n_opt = 1
            opt_bits = [tuple(bits)]
        elif total == best:
            n_opt += 1
            if len(opt_bits) < MAX_VARIANTS:
                opt_bits.append(tuple(bits))
        if (g & 0x1FFF) == 0 and time.perf_counter() > deadline:
            return None
    return {
        "cost": best,
        "bits": best_bits,
        "n_opt": n_opt,
        "opt_bits": opt_bits,
        "truncated": n_opt > len(opt_bits),
    }


# ---------------------------------------------------------------------------
# Heuristic + branch and bound fallback
# ---------------------------------------------------------------------------

def heuristic_orientations(comp: Component, deadline: float, restarts: int = 24):
    n = len(comp.fragments)
    npos = len(comp.positions)
    frag_entries = _flatten(comp)
    tables = [pm.table for pm in comp.pos_models]
    t = [pm.t for pm in comp.pos_models]
    rng = random.Random(0xC0FFEE + comp.index)
    starts = [[0] * n, [1] * n]
    for _ in range(max(0, restarts - 2)):
        starts.append([rng.randint(0, 1) for _ in range(n)])
    best_cost = INF
    best_bits = [0] * n
    for start in starts:
        bits = list(start)
        s, k = _counts(comp, frag_entries, bits)
        cost = [tables[j][s[j] * (t[j] + 1) + k[j]] for j in range(npos)]
        total = sum(cost)
        improved = True
        while improved and time.perf_counter() <= deadline:
            improved = False
            order = list(range(n))
            rng.shuffle(order)
            for i in order:
                delta = -1 if bits[i] == 0 else 1
                changes = []
                d = 0
                for pidx, a in frag_entries[i]:
                    v = s[pidx] + delta
                    kk = k[pidx] + (delta if a == 1 else 0)
                    c = tables[pidx][v * (t[pidx] + 1) + kk]
                    d += c - cost[pidx]
                    changes.append((pidx, v, kk, c))
                if d < 0:
                    for pidx, v, kk, c in changes:
                        s[pidx] = v
                        k[pidx] = kk
                        cost[pidx] = c
                    total += d
                    bits[i] ^= 1
                    improved = True
        if total < best_cost:
            best_cost = total
            best_bits = bits
    return best_cost, best_bits


def _bound(comp: Component, s0, k0, s1, k1, u, u1) -> int:
    total = 0
    for j, pm in enumerate(comp.pos_models):
        best = INF
        for a, b in comp.allowed[pm.position]:
            m0 = k0[j] if a == 0 else s0[j] - k0[j]
            m1 = k1[j] if b == 0 else s1[j] - k1[j]
            cu = 0
            if a == 0 and b == 0:
                cu = u1[j]
            elif a == 1 and b == 1:
                cu = u[j] - u1[j]
            c = m0 + m1 + cu
            if c < best:
                best = c
        total += best
    return total


def branch_and_bound(comp: Component, deadline: float, init_ub: int, init_bits,
                     max_nodes: int):
    n = len(comp.fragments)
    npos = len(comp.positions)
    frag_entries = _flatten(comp)
    order = sorted(range(n), key=lambda i: (-len(frag_entries[i]), i))
    s0 = [0] * npos
    k0 = [0] * npos
    s1 = [0] * npos
    k1 = [0] * npos
    u = [pm.t for pm in comp.pos_models]
    u1 = [pm.ones for pm in comp.pos_models]
    assigned = [0] * n
    best_cost = init_ub
    best_bits = list(init_bits)
    nodes = 0
    timed_out = False
    root_lb = _bound(comp, s0, k0, s1, k1, u, u1)

    def dfs(depth: int) -> None:
        nonlocal best_cost, best_bits, nodes, timed_out
        nodes += 1
        if nodes > max_nodes or (not (nodes & 0x3FF) and time.perf_counter() > deadline):
            timed_out = True
            return
        # The bound already charges the committed fragments and the best case
        # for the undecided ones, so it is the node's lower bound on the whole
        # solution -- not a partial cost to be added to.
        lb = _bound(comp, s0, k0, s1, k1, u, u1)
        if lb >= best_cost:
            return
        if depth == n:
            best_cost = lb
            best_bits = list(assigned)
            return
        i = order[depth]
        entries = frag_entries[i]
        for side in (0, 1):
            if side == 0:
                for pidx, a in entries:
                    s0[pidx] += 1
                    if a == 1:
                        k0[pidx] += 1
                    u[pidx] -= 1
                    if a == 1:
                        u1[pidx] -= 1
            else:
                for pidx, a in entries:
                    s1[pidx] += 1
                    if a == 1:
                        k1[pidx] += 1
                    u[pidx] -= 1
                    if a == 1:
                        u1[pidx] -= 1
            assigned[i] = side
            dfs(depth + 1)
            if side == 0:
                for pidx, a in entries:
                    s0[pidx] -= 1
                    if a == 1:
                        k0[pidx] -= 1
                    u[pidx] += 1
                    if a == 1:
                        u1[pidx] += 1
            else:
                for pidx, a in entries:
                    s1[pidx] -= 1
                    if a == 1:
                        k1[pidx] -= 1
                    u[pidx] += 1
                    if a == 1:
                        u1[pidx] += 1
            if timed_out:
                return

    dfs(0)
    return {
        "best": best_cost,
        "bits": best_bits,
        "root_lb": root_lb,
        "nodes": nodes,
        "timed_out": timed_out,
    }


def _sub_component(comp: Component, positions: List[int]) -> Optional[Component]:
    """Restrict a component to a contiguous window of positions.

    Fragments keep one shared orientation inside the window, which makes the
    window optimum an optimistic estimate for the global optimum (a relaxation
    in the sense that a fragment may pick a different side in each window).
    """
    pos_set = set(positions)
    frags = []
    for f in comp.fragments:
        alleles = {p: a for p, a in f.alleles.items() if p in pos_set}
        if alleles:
            from ..model import Fragment

            frags.append(Fragment(f.fid, alleles))
    if not frags:
        return None
    sub = Component(
        index=comp.index,
        positions=list(positions),
        fragments=frags,
        allowed={p: comp.allowed[p] for p in positions},
    )
    sub.allowed_lock = {p: comp.allowed_lock.get(p) for p in positions}
    sub.build_models()
    return sub


def chunked_lower_bound(comp: Component, width: int = CHUNK_WIDTH, deadline: Optional[float] = None):
    """Additive lower bound: sum of exact optima over position windows.

    Every position belongs to exactly one window, so the global optimum is at
    least the sum.  Windows wider than a fragment's span preserve those
    fragments' internal coupling and make the bound tighter.
    """
    if deadline is None:
        deadline = time.perf_counter() + 2.0
    total = 0
    proven = True
    positions = comp.positions
    for start in range(0, len(positions), width):
        window = positions[start:start + width]
        sub = _sub_component(comp, window)
        if sub is None:
            continue
        out = solve_dp(sub, deadline, max_states=1 << 13)
        if out is not None:
            total += out.cost
            continue
        enum = enumerate_exact(sub, deadline)
        if enum is not None:
            total += enum["cost"]
            continue
        proven = False
        total += _position_relaxation(sub)
    return total, proven


def _position_relaxation(comp: Component) -> int:
    total = 0
    for j, pm in enumerate(comp.pos_models):
        best = INF
        for a, b in comp.allowed[pm.position]:
            c = _position_cost(pm, pm.t, pm.ones, a, b)
            if c < best:
                best = c
        total += best
    return total


# ---------------------------------------------------------------------------
# Phase linkage
# ---------------------------------------------------------------------------

def _phase_analysis(comp: Component, optimal_bits, complete: bool):
    n = len(comp.fragments)
    if not complete:
        return {"status": "unknown", "reason": "optimal set not fully enumerated"}
    if n > 48:
        return {"status": "skipped", "reason": f"{n} fragments exceeds phase-analysis cap"}
    if not optimal_bits:
        return {"status": "unknown"}
    same: List[List[str]] = []
    opposite: List[List[str]] = []
    ambiguous = set()
    for i, j in itertools.combinations(range(n), 2):
        rel = {b[i] == b[j] for b in optimal_bits}
        if len(rel) == 1:
            (val,) = rel
            (same if val else opposite).append([comp.fragments[i].fid, comp.fragments[j].fid])
        else:
            ambiguous.add(comp.fragments[i].fid)
            ambiguous.add(comp.fragments[j].fid)
    return {
        "status": "complete",
        "same_phase_pairs": same[:400],
        "n_same_phase_pairs": len(same),
        "opposite_phase_pairs": opposite[:400],
        "n_opposite_phase_pairs": len(opposite),
        "ambiguous_fragments": sorted(ambiguous),
        "note": (
            "same/opposite refers to the fragment pair inside this component; a free "
            "global flip swaps the two labels"
        ),
    }


# ---------------------------------------------------------------------------
# Assemble a component solution
# ---------------------------------------------------------------------------

CLASS_BUDGET = 400_000


def _sequence_classes(comp, optimal_bits, order_pinned, cost,
                      limit: int) -> Tuple[Optional[int], int, List[Dict[str, object]]]:
    """Count distinct optimal reconstructions of one component.

    A reconstruction is the full ordered pair of haplotype strings: for a fixed
    orientation vector there may be several allele pairs attaining the same
    minimum at a position, and those are different reconstructions.  No label
    canonicalisation is applied here -- swapping hap0/hap1 is a different
    ordered pair, exactly as the user sees it.  ``palindromic`` counts those
    reconstructions with hap0 == hap1 at every position, which are the only
    ones a global label swap leaves fixed.

    Returns ``(count, palindromic, variants)``; ``count`` is ``None`` when the
    exact enumeration would exceed :data:`CLASS_BUDGET` combinations, and
    ``variants`` is then a partial, bounded list of representatives.
    """
    seen = set()
    out: List[Dict[str, object]] = []
    budget = CLASS_BUDGET
    palindromic = 0
    for ob in optimal_bits:
        c, _pairs, options = _eval_bits(comp, ob)
        if c != cost:
            continue
        # Every combination of optimal pairs at each position is a distinct
        # reconstruction; enumerating them is bounded by CLASS_BUDGET.
        combos = 1
        for opts in options:
            combos *= max(1, len(opts))
        if combos > budget:
            return None, palindromic, out
        budget -= combos
        for combo in itertools.product(*[o or [(0, 0)] for o in options]):
            hap0 = {p: combo[j][0] for j, p in enumerate(comp.positions)}
            hap1 = {p: combo[j][1] for j, p in enumerate(comp.positions)}
            bits = list(ob)
            key = (tuple(hap0[p] for p in comp.positions), tuple(hap1[p] for p in comp.positions))
            if key in seen:
                continue
            seen.add(key)
            if key[0] == key[1]:
                palindromic += 1
            if len(out) < limit:
                out.append(
                    {
                        "cost": c,
                        "hap0": "".join(str(x) for x in key[0]),
                        "hap1": "".join(str(x) for x in key[1]),
                        "orientation": {
                            comp.fragments[i].fid: bits[i] for i in range(len(bits))
                        },
                        "errors": _errors_for(comp, bits, hap0, hap1),
                    }
                )
    return len(seen), palindromic, out


def _make_variants(comp, optimal_bits, order_pinned, cost, limit=MAX_VARIANTS):
    seen = set()
    out: List[Dict[str, object]] = []
    for ob in optimal_bits:
        c, pairs, _ = _eval_bits(comp, ob)
        if c != cost:
            continue
        bits, hap0, hap1 = _canonical(comp, ob, pairs, order_pinned)
        key = (tuple(hap0[p] for p in comp.positions), tuple(hap1[p] for p in comp.positions))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "cost": c,
                "hap0": "".join(str(hap0[p]) for p in comp.positions),
                "hap1": "".join(str(hap1[p]) for p in comp.positions),
                "orientation": {comp.fragments[i].fid: bits[i] for i in range(len(bits))},
                "errors": _errors_for(comp, bits, hap0, hap1),
            }
        )
        if len(out) >= limit:
            break
    return out


def _finish(comp: Component, *, cost, bits, status, lower_bound, upper_bound,
            n_opt_vectors, opt_bits, ambiguity_known, pair_options,
            variants_truncated, engine, engine_note, nodes, elapsed_ms) -> CompSolution:
    order_pinned = comp.order_pinned
    flip_free = (not order_pinned) and _flip_symmetric(comp)
    c_cost, pairs, options = _eval_bits(comp, bits)
    cbits, hap0, hap1 = _canonical(comp, bits, pairs, order_pinned)
    errors = _errors_for(comp, cbits, hap0, hap1)

    if not pair_options:
        pair_options = {}
        for j, opts in enumerate(options):
            if len(opts) > 1:
                pair_options[comp.positions[j]] = [list(x) for x in sorted(opts)]

    # Canonical representative first, then the enumerated optimal vectors.
    ordered_bits = [tuple(cbits)] + [tuple(b) for b in opt_bits if tuple(b) != tuple(cbits)]
    n_classes: Optional[int]
    n_palindromic = 0
    if bool(ambiguity_known) and not variants_truncated:
        n_classes, n_palindromic, variants = _sequence_classes(
            comp, ordered_bits, order_pinned, c_cost, MAX_VARIANTS
        )
    else:
        n_classes = None
        variants = _make_variants(comp, ordered_bits, order_pinned, c_cost)

    phase = (
        _phase_analysis(comp, opt_bits, bool(ambiguity_known) and not variants_truncated)
        if ambiguity_known
        else {"status": "unknown", "reason": "optimal set not fully enumerated"}
    )
    # ``classes_exact`` turns false when the pair enumeration itself hit its
    # budget; the partial list still supplies a usable representative set.
    variants_truncated = bool(variants_truncated) or n_classes is None
    if not variants:
        # Always keep the canonical reconstruction so callers can index [0].
        variants = [
            {
                "cost": c_cost,
                "hap0": "".join(str(hap0[p]) for p in comp.positions),
                "hap1": "".join(str(hap1[p]) for p in comp.positions),
                "orientation": {comp.fragments[i].fid: cbits[i] for i in range(len(cbits))},
                "errors": errors,
            }
        ]
        variants_truncated = True

    return CompSolution(
        index=comp.index,
        positions=list(comp.positions),
        status=status,
        proven=status == "optimal",
        cost=c_cost,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        orientation={comp.fragments[i].fid: cbits[i] for i in range(len(cbits))},
        hap0=hap0,
        hap1=hap1,
        errors=errors,
        n_optimal_vectors=n_opt_vectors,
        n_orientation_classes=n_classes,
        n_palindromic=n_palindromic,
        classes_truncated=bool(variants_truncated),
        ambiguity_known=bool(ambiguity_known),
        ambiguous_positions=sorted(pair_options),
        pair_options=pair_options,
        variants=variants,
        variants_truncated=bool(variants_truncated),
        phase=phase,
        elapsed_ms=elapsed_ms,
        nodes=nodes,
        engine=engine,
        engine_note=engine_note,
        order_pinned=order_pinned,
        flip_free=flip_free,
    )


def solve_component(comp: Component, deadline: float, engine: str = "auto",
                    max_nodes: int = 300_000) -> CompSolution:
    t0 = time.perf_counter()
    note_parts: List[str] = []
    n = len(comp.fragments)

    if engine in ("auto", "dp"):
        out = solve_dp(comp, deadline, max_states=DP_MAX_STATES,
                       max_transitions=DP_MAX_TRANSITIONS)
        if out is not None:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return _finish(
                comp,
                cost=out.cost,
                bits=out.bits,
                status="optimal",
                lower_bound=out.cost,
                upper_bound=out.cost,
                n_opt_vectors=out.n_opt_vectors,
                opt_bits=out.opt_bits,
                ambiguity_known=True,
                pair_options={p: [list(x) for x in v] for p, v in out.pair_options.items()},
                variants_truncated=out.variants_hit_cap or out.n_opt_vectors > len(out.opt_bits),
                engine="sweep-dp",
                engine_note=f"exact; max live-set {out.max_states}, {out.transitions} transitions",
                nodes=out.transitions,
                elapsed_ms=elapsed,
            )
        if engine == "dp":
            note_parts.append("DP state cap exceeded")

    if engine in ("auto", "enum", "dp") and n <= ENUM_MAX_N:
        enum = enumerate_exact(comp, deadline)
        if enum is not None:
            elapsed = int((time.perf_counter() - t0) * 1000)
            return _finish(
                comp,
                cost=enum["cost"],
                bits=enum["bits"],
                status="optimal",
                lower_bound=enum["cost"],
                upper_bound=enum["cost"],
                n_opt_vectors=enum["n_opt"],
                opt_bits=enum["opt_bits"],
                ambiguity_known=True,
                pair_options={},
                variants_truncated=enum["truncated"],
                engine="enumeration",
                engine_note=f"exhaustive over 2^{n} orientations",
                nodes=1 << n,
                elapsed_ms=elapsed,
            )

    # ---- fallback: incumbent + branch and bound + chunked bound ----
    ub, bits = heuristic_orientations(comp, deadline)
    bb = branch_and_bound(comp, deadline, ub, bits, max_nodes)
    chunk_lb, chunk_proven = chunked_lower_bound(comp, deadline=min(deadline, time.perf_counter() + 2.0))
    lb = max(bb["root_lb"], chunk_lb)
    elapsed = int((time.perf_counter() - t0) * 1000)
    if bb["best"] <= lb:
        status = "optimal"
        lower_bound = upper_bound = bb["best"]
        note_parts.append("certified by matching lower bound")
    else:
        status = "timeout"
        lower_bound, upper_bound = lb, bb["best"]
        note_parts.append(
            f"search budget exhausted (nodes={bb['nodes']}); cost is an upper bound"
        )
    if not chunk_proven:
        note_parts.append("chunked lower bound used a per-position relaxation in some windows")
    return _finish(
        comp,
        cost=bb["best"],
        bits=bb["bits"],
        status=status,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        n_opt_vectors=None,
        opt_bits=[],
        ambiguity_known=False,
        pair_options={},
        variants_truncated=True,
        engine="branch-and-bound",
        engine_note="; ".join(note_parts),
        nodes=bb["nodes"],
        elapsed_ms=elapsed,
    )
