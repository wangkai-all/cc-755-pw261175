"""Shared machinery: lock compilation, component discovery, scoring tables."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

from ..model import Dataset, Fragment, Lock

Pair = Tuple[int, int]
ALL_PAIRS: Tuple[Pair, ...] = ((0, 0), (0, 1), (1, 0), (1, 1))


def allowed_pairs_for(lock: Lock | None) -> Tuple[Pair, ...]:
    if lock is None:
        return ALL_PAIRS
    a0, a1 = lock.resolved()
    pairs = []
    for a, b in ALL_PAIRS:
        if a0 is not None and a != a0:
            continue
        if a1 is not None and b != a1:
            continue
        if lock.distinct and a == b:
            continue
        pairs.append((a, b))
    if not pairs:
        # A well-formed lock always leaves at least one pair; guard anyway.
        return ALL_PAIRS
    return tuple(pairs)


def compile_allowed(ds: Dataset) -> Dict[int, Tuple[Pair, ...]]:
    return {p: allowed_pairs_for(ds.locks.get(p)) for p in ds.positions}


def swapped(pairs: Sequence[Pair]) -> Tuple[Pair, ...]:
    return tuple((b, a) for a, b in pairs)


def is_order_pinned(lock: Lock | None) -> bool:
    if lock is None:
        return False
    return lock.resolved()[0] is not None or lock.resolved()[1] is not None


def flip_symmetric(allowed: Sequence[Tuple[Pair, ...]]) -> bool:
    for pairs in allowed:
        if set(pairs) != set(swapped(pairs)):
            return False
    return True


@dataclass
class PositionModel:
    position: int
    cov: List[Tuple[int, int]]  # (local fragment bit, allele)
    t: int
    ones: int
    table: List[int]  # (t+1)^2, index s*(t+1)+k
    table_swapped: List[int]

    def cost_from_counts(self, s: int, k: int) -> int:
        return self.table[s * (self.t + 1) + k]

    def alt_pairs(self, s: int, k: int, pairs: Sequence[Pair]) -> List[Pair]:
        best = None
        out: List[Pair] = []
        for a, b in pairs:
            m0 = k if a == 0 else s - k
            m1 = (self.ones - k) if b == 0 else (self.t - s) - (self.ones - k)
            c = m0 + m1
            if best is None or c < best:
                best = c
                out = [(a, b)]
            elif c == best:
                out.append((a, b))
        return out


def _build_table(t: int, ones: int, pairs: Sequence[Pair]) -> List[int]:
    size = t + 1
    table = [10**9] * (size * size)
    for s in range(t + 1):
        lo = max(0, s - (t - ones))
        hi = min(s, ones)
        base = s * size
        for k in range(lo, hi + 1):
            k1 = ones - k
            best = 10**9
            for a, b in pairs:
                m0 = k if a == 0 else s - k
                m1 = k1 if b == 0 else (t - s) - k1
                c = m0 + m1
                if c < best:
                    best = c
            table[base + k] = best
    return table


@dataclass
class Component:
    index: int
    positions: List[int]
    fragments: List[Fragment]
    allowed: Dict[int, Tuple[Pair, ...]]
    pos_models: List[PositionModel] = field(default_factory=list)

    def build_models(self) -> None:
        local = {f.fid: i for i, f in enumerate(self.fragments)}
        cov: Dict[int, List[Tuple[int, int]]] = {p: [] for p in self.positions}
        for f in self.fragments:
            b = local[f.fid]
            for p, a in f.alleles.items():
                cov[p].append((b, a))
        models = []
        for p in self.positions:
            entries = cov[p]
            t = len(entries)
            ones = sum(a for _, a in entries)
            pairs = self.allowed[p]
            models.append(
                PositionModel(
                    position=p,
                    cov=entries,
                    t=t,
                    ones=ones,
                    table=_build_table(t, ones, pairs),
                    table_swapped=_build_table(t, ones, swapped(pairs)),
                )
            )
        self.pos_models = models

    @property
    def n_fragments(self) -> int:
        return len(self.fragments)

    @property
    def order_pinned(self) -> bool:
        return any(is_order_pinned(self.allowed_lock.get(p)) for p in self.positions)

    # set by build_components; declared for typing convenience
    allowed_lock: Dict[int, Lock | None] = field(default_factory=dict)


def build_components(ds: Dataset) -> List[Component]:
    """Union positions that share a fragment; return components in coordinate order."""
    frags = ds.all_fragments()
    allowed = compile_allowed(ds)
    parent: Dict[int, int] = {p: p for p in ds.positions}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra > rb:
                ra, rb = rb, ra
            parent[rb] = ra

    for f in frags:
        ps = f.positions
        for p in ps[1:]:
            union(ps[0], p)

    groups: Dict[int, Dict[str, object]] = {}
    for p in ds.positions:
        r = find(p)
        g = groups.setdefault(r, {"positions": [], "frags": {}})
        g["positions"].append(p)  # type: ignore[union-attr]
    frag_by_id = {f.fid: f for f in frags}
    for f in frags:
        r = find(f.positions[0])
        groups[r]["frags"][f.fid] = frag_by_id[f.fid]  # type: ignore[index]

    comps: List[Component] = []
    for idx, (r, g) in enumerate(sorted(groups.items(), key=lambda kv: min(kv[1]["positions"]))):  # type: ignore[type-var]
        positions = sorted(g["positions"])  # type: ignore[arg-type]
        frs = sorted(g["frags"].values(), key=lambda f: (f.start, f.fid))  # type: ignore[union-attr]
        comp = Component(
            index=idx,
            positions=positions,
            fragments=frs,
            allowed={p: allowed[p] for p in positions},
        )
        comp.allowed_lock = {p: ds.locks.get(p) for p in positions}
        comp.build_models()
        comps.append(comp)
    return comps


def lower_bound_component(comp: Component) -> int:
    """Position-wise relaxation: fragments may be oriented independently per position."""
    lb = 0
    for pm in comp.pos_models:
        best = None
        for a, b in comp.allowed[pm.position]:
            total = 0
            k1 = pm.ones
            for _, allele in pm.cov:
                m0 = 1 if allele != a else 0
                m1 = 1 if allele != b else 0
                total += m0 if m0 <= m1 else m1
            if best is None or total < best:
                best = total
        lb += best or 0
    return lb


def lock_error_lower_bound(ds: Dataset) -> int:
    """Per-fragment unavoidable mismatches on locked positions.

    A fragment's orientation is shared across all positions, so a fragment that
    matches hap0 at one locked position and hap1 at another must pay at least
    one error.  Summing the per-fragment minimum is a valid lower bound.
    """
    l0, l1 = ds.ordered_lock_values()
    total = 0
    for f in ds.all_fragments():
        best = None
        for orient in (0, 1):
            pins = l0 if orient == 0 else l1
            other = l1 if orient == 0 else l0
            errs = 0
            for p, a in f.alleles.items():
                if p in pins:
                    if pins[p] != a:
                        errs += 1
                elif p in other:
                    # Reading the other haplotype gives no constraint; the
                    # position is still observed but unconstrained on this side.
                    pass
            if best is None or errs < best:
                best = errs
        total += best or 0
    return total


def component_signature(comp: Component) -> Dict[str, object]:
    return {
        "index": comp.index,
        "start": comp.positions[0],
        "end": comp.positions[-1],
        "n_positions": len(comp.positions),
        "fragment_ids": [f.fid for f in comp.fragments],
        "order_pinned": comp.order_pinned,
        "flip_symmetric": flip_symmetric([comp.allowed[p] for p in comp.positions]),
    }


def describe_components(comps: Sequence[Component]) -> List[Dict[str, object]]:
    return [component_signature(c) for c in comps]
