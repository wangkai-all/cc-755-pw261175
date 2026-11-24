"""Shared test helpers: an independent brute-force reference and fixtures.

The reference deliberately does not import any solver code.  It enumerates every
fragment-orientation vector, then every allele pair allowed at each position,
and scores directly against the observations.  Everything the optimiser claims
-- cost, the number of distinct reconstructions, which reconstructions exist --
is checked against this.
"""
from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from haplorecon.model import Dataset, Fragment, Lock, parse_text  # noqa: E402

Pair = Tuple[int, int]
Solution = Tuple[Tuple[int, ...], Tuple[int, ...]]


def allowed_pairs(lock: Optional[Lock]) -> List[Pair]:
    out = []
    for a, b in itertools.product((0, 1), repeat=2):
        if lock is not None:
            if lock.hap0 is not None and a != lock.hap0:
                continue
            if lock.hap1 is not None and b != lock.hap1:
                continue
            if lock.distinct and a == b:
                continue
        out.append((a, b))
    return out or [(0, 0)]


def reference_solve(ds: Dataset):
    """Return ``(best_cost, canonical_solutions, swap_symmetric)``.

    ``canonical_solutions`` identifies hap0/hap1 when the locks leave the label
    swap free, matching what the solver reports as one reconstruction.
    """
    frags = ds.all_fragments()
    positions = list(ds.positions)
    allowed = {p: allowed_pairs(ds.locks.get(p)) for p in positions}
    swap_symmetric = all(
        {pair for pair in allowed[p]} == {(b, a) for a, b in allowed[p]}
        for p in positions
    )

    best: Optional[int] = None
    solutions: Set[Solution] = set()

    def absorb(orientation, per_position: Sequence[Sequence[Pair]], cost: int) -> None:
        nonlocal best, solutions
        if best is None or cost < best:
            best = cost
            solutions = set()
        elif cost > best:
            return
        for combo in itertools.product(*per_position):
            h0 = tuple(pair[0] for pair in combo)
            h1 = tuple(pair[1] for pair in combo)
            solutions.add(min((h0, h1), (h1, h0)) if swap_symmetric else (h0, h1))

    for bits in itertools.product((0, 1), repeat=len(frags)):
        total = 0
        per_position: List[List[Pair]] = []
        for p in positions:
            sides: List[List[int]] = [[], []]
            for fi, frag in enumerate(frags):
                allele = frag.alleles.get(p)
                if allele is not None:
                    sides[bits[fi]].append(allele)
            scored = [
                (sum(1 for x in sides[0] if x != a) + sum(1 for x in sides[1] if x != b), (a, b))
                for a, b in allowed[p]
            ]
            low = min(c for c, _ in scored)
            total += low
            per_position.append([pair for c, pair in scored if c == low])
        if best is not None and total > best:
            continue
        absorb(bits, per_position, total)
    return best, solutions, swap_symmetric


def random_dataset(rng: random.Random, *, max_positions: int = 10, max_fragments: int = 7,
                   lock_probability: float = 0.5) -> Dataset:
    npos = rng.randint(3, max_positions)
    frags = []
    for i in range(rng.randint(1, max_fragments)):
        start = rng.randint(0, npos - 2)
        span = rng.randint(2, max(2, min(4, npos - start)))
        alleles = {p: rng.randint(0, 1) for p in range(start, min(npos, start + span))}
        if rng.random() < 0.25:
            p = rng.choice(list(alleles))
            alleles[p] = 1 - alleles[p]
        frags.append(Fragment(fid=f"f{i}", alleles=alleles))
    positions = sorted({p for f in frags for p in f.alleles})
    ds = Dataset(fragments=frags, positions=positions)
    if positions and rng.random() < lock_probability:
        p = rng.choice(positions)
        mode = rng.random()
        if mode < 0.4:
            ds.set_lock(Lock(position=p, hap0=rng.randint(0, 1)))
        elif mode < 0.7:
            ds.set_lock(Lock(position=p, hap1=rng.randint(0, 1)))
        else:
            a = rng.randint(0, 1)
            ds.set_lock(Lock(position=p, hap0=a, hap1=1 - a))
    return ds


def canonicalise(hap0: str, hap1: str, swap_symmetric: bool) -> Solution:
    a = tuple(int(c) for c in hap0)
    b = tuple(int(c) for c in hap1)
    return min((a, b), (b, a)) if swap_symmetric else (a, b)


def load_example(name: str) -> Dataset:
    return parse_text((ROOT / "examples" / f"{name}.txt").read_text(), name=name)
