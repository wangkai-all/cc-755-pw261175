"""Top-level reconstruction: components, ambiguity, gaps, conflicts, reuse.

Modelling rules encoded here
----------------------------
* A fragment's orientation (which latent sequence it came from) is one shared
  hidden variable.  Its whole set of observations is scored together.
* Components are coverage-connected position groups.  They share no positions
  and no fragments, so the optimum is the sum of component optima and each
  component is solved on its own.
* A component with no order-pinning lock has a free global flip.  Both label
  assignments fit the data identically; we report that ambiguity rather than
  choosing one silently.
* Disconnected components are never concatenated into a "certain" sequence.
  Coordinate gaps with no observations are reported as undetermined.
* Every number carries provenance: status, proven flag, lower and upper bound,
  engine and elapsed time.  A result that did not prove optimality is never
  presented as optimal.
"""
from __future__ import annotations

import hashlib
import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import Dataset, Fragment, Lock
from .base import (
    Component,
    build_components,
    compile_allowed,
    describe_components,
    lock_error_lower_bound,
)
from .component import CompSolution, ENUM_MAX_N, solve_component

RESULT_SCHEMA = "haplorecon/result/1"
SOLVER_VERSION = "1.1.0"
MAX_GLOBAL_SOLUTIONS = 24


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

@dataclass
class SolveOptions:
    time_limit_s: float = 20.0
    engine: str = "auto"  # auto | enum | bandb
    max_variants: int = 200
    include_phase: bool = True
    max_nodes: int = 300_000

    def normalised(self) -> "SolveOptions":
        engine = (self.engine or "auto").strip().lower()
        if engine not in ("auto", "enum", "bandb"):
            raise ValueError(f"unknown engine {self.engine!r}")
        tl = float(self.time_limit_s)
        if not (tl > 0) or tl != tl:
            raise ValueError("time_limit_s must be a positive number")
        return SolveOptions(
            time_limit_s=min(tl, 600.0),
            engine=engine,
            max_variants=max(1, min(int(self.max_variants), 500)),
            include_phase=bool(self.include_phase),
            max_nodes=max(1, min(int(self.max_nodes), 5_000_000)),
        )

    def to_json(self) -> Dict[str, object]:
        return {
            "time_limit_s": self.time_limit_s,
            "engine": self.engine,
            "max_variants": self.max_variants,
            "include_phase": self.include_phase,
            "max_nodes": self.max_nodes,
        }


def dataset_signature(ds: Dataset) -> str:
    parts = [f"v={SOLVER_VERSION}"]
    for f in sorted(ds.all_fragments(), key=lambda f: f.fid):
        parts.append(f.fid + ":" + ",".join(f"{p}{a}" for p, a in sorted(f.alleles.items())))
    for p in sorted(ds.locks):
        lk = ds.locks[p]
        parts.append(f"L{p}:{lk.hap0}:{lk.hap1}:{int(lk.distinct)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def constraint_signature(ds: Dataset) -> str:
    parts = [f"v={SOLVER_VERSION}"]
    for f in sorted(ds.all_fragments(), key=lambda f: f.fid):
        parts.append(f.fid + ":" + ",".join(f"{p}{a}" for p, a in sorted(f.alleles.items())))
    for p in sorted(ds.locks):
        lk = ds.locks[p]
        parts.append(f"L{p}:{lk.hap0}:{lk.hap1}:{int(lk.distinct)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Component cache keys (for incremental re-solves)
# ---------------------------------------------------------------------------

def component_cache_key(comp: Component) -> str:
    frag_key = ";".join(sorted(f.fid + "=" + ",".join(f"{p}{a}" for p, a in sorted(f.alleles.items()))
                               for f in comp.fragments))
    lock_key = ";".join(
        f"{p}:{comp.allowed_lock[p].hap0}:{comp.allowed_lock[p].hap1}:{int(comp.allowed_lock[p].distinct)}"
        for p in comp.positions
        if comp.allowed_lock.get(p) is not None
    )
    return hashlib.sha256(
        f"{','.join(map(str, comp.positions))}|{frag_key}|{lock_key}".encode()
    ).hexdigest()[:20]


def _component_payload_to_cache_entry(payload: Dict[str, object]) -> Dict[str, object]:
    """Convert a serialised result component into a loader-compatible entry."""
    return {
        "status": payload["status"],
        "proven": payload["proven"],
        "cost": payload["cost"],
        "lower_bound": payload["lower_bound"],
        "upper_bound": payload["upper_bound"],
        "orientation": payload["orientation"],
        "hap0": payload["alleles"],
        "hap1": payload["alleles"],
        "errors": payload["errors"],
        "n_optimal_vectors": payload["n_optimal_vectors"],
        "n_orientation_classes": payload["n_orientation_classes"],
        "classes_truncated": payload["variants_truncated"],
        "ambiguity_known": payload["classes_known"],
        "ambiguous_positions": payload["ambiguous_positions"],
        "pair_options": payload["pair_options_detail"],
        "variants": payload["variants"],
        "variants_truncated": payload["variants_truncated"],
        "phase": payload["phase"],
        "engine": payload["engine"],
        "engine_note": payload["engine_note"],
        "order_pinned": payload["order_pinned"],
        "flip_free": payload["flip_free"],
    }


def _comp_solution_to_cache(comp: Component, sol: CompSolution) -> Dict[str, object]:
    return {
        "cache_key": component_cache_key(comp),
        "status": sol.status,
        "proven": sol.proven,
        "cost": sol.cost,
        "lower_bound": sol.lower_bound,
        "upper_bound": sol.upper_bound,
        "orientation": dict(sol.orientation),
        "hap0": {str(k): v for k, v in sol.hap0.items()},
        "hap1": {str(k): v for k, v in sol.hap1.items()},
        "errors": {k: list(v) for k, v in sol.errors.items()},
        "n_optimal_vectors": sol.n_optimal_vectors,
        "n_orientation_classes": sol.n_orientation_classes,
        "classes_truncated": sol.classes_truncated,
        "ambiguity_known": sol.ambiguity_known,
        "ambiguous_positions": list(sol.ambiguous_positions),
        "pair_options": {str(k): v for k, v in sol.pair_options.items()},
        "variants": list(sol.variants),
        "variants_truncated": sol.variants_truncated,
        "phase": dict(sol.phase),
        "engine": sol.engine,
        "engine_note": sol.engine_note,
        "order_pinned": sol.order_pinned,
        "flip_free": sol.flip_free,
    }


def _pair(value):
    """Accept either ``{pos: allele}`` (cache form) or ``{pos: [h0, h1]}`` (result form)."""
    if isinstance(value, (list, tuple)):
        return int(value[0]), int(value[1])
    v = int(value)
    return v, v


def _comp_solution_from_cache(comp: Component, cached: Dict[str, object]) -> CompSolution:
    return CompSolution(
        index=comp.index,
        positions=list(comp.positions),
        status=str(cached["status"]),
        proven=bool(cached["proven"]),
        cost=int(cached["cost"]),  # type: ignore[arg-type]
        lower_bound=int(cached["lower_bound"]),  # type: ignore[arg-type]
        upper_bound=int(cached["upper_bound"]),  # type: ignore[arg-type]
        orientation={str(k): int(v) for k, v in dict(cached["orientation"]).items()},  # type: ignore[arg-type]
        hap0={int(k): _pair(v)[0] for k, v in dict(cached["hap0"]).items()},
        hap1={int(k): _pair(v)[1] for k, v in dict(cached["hap1"]).items()},
        errors={str(k): [int(p) for p in v] for k, v in dict(cached["errors"]).items()},  # type: ignore[arg-type]
        n_optimal_vectors=cached.get("n_optimal_vectors"),  # type: ignore[arg-type]
        n_orientation_classes=cached.get("n_orientation_classes"),  # type: ignore[arg-type]
        classes_truncated=bool(cached.get("classes_truncated", True)),
        ambiguity_known=bool(cached.get("ambiguity_known", False)),
        ambiguous_positions=[int(p) for p in cached.get("ambiguous_positions", [])],  # type: ignore[arg-type]
        pair_options={int(k): v for k, v in dict(cached.get("pair_options", {})).items()},  # type: ignore[arg-type]
        variants=list(cached.get("variants") or []),  # type: ignore[arg-type]
        variants_truncated=bool(cached.get("variants_truncated", True)),
        phase=dict(cached.get("phase") or {"status": "reused"}),  # type: ignore[arg-type]
        elapsed_ms=0,
        nodes=0,
        engine="reused:" + str(cached.get("engine", "?")),
        engine_note="reused from a previous solve; constraints unchanged",
        order_pinned=bool(cached.get("order_pinned", False)),
        flip_free=bool(cached.get("flip_free", False)),
    )


# ---------------------------------------------------------------------------
# Conflict reporting
# ---------------------------------------------------------------------------

def detect_conflicts(ds: Dataset, comps: Sequence[Component], sols: Sequence[CompSolution]):
    """Report fragment/lock contradictions at two levels.

    ``unavoidable``
        A fragment's assignments disagree with *both* haplotype sides implied by
        the locks it touches (two locks pinning opposite labels at different
        positions it covers).  No orientation can satisfy all of them, so the
        fragment set is a proven contradiction regardless of search outcome.

    ``conflicting``
        In the reported optimum the fragment pays an error on a locked position.
        That is the actionable set: either the lock or the fragment is wrong, and
        the list is exactly what to inspect.
    """
    l0, l1 = ds.ordered_lock_values()  # position -> allele for hap0 / hap1
    comp_of = {f.fid: c.index for c in comps for f in c.fragments}
    orient: Dict[str, int] = {}
    for s in sols:
        orient.update(s.orientation)

    unavoidable: List[Dict[str, object]] = []
    conflicting: Dict[int, List[str]] = {}
    for f in ds.all_fragments():
        side_errs: Dict[int, List[int]] = {0: [], 1: []}
        for p, a in f.alleles.items():
            if p in l0 and l0[p] != a:
                side_errs[0].append(p)
            if p in l1 and l1[p] != a:
                side_errs[1].append(p)
        if side_errs[0] and side_errs[1]:
            best_side = 0 if len(side_errs[0]) <= len(side_errs[1]) else 1
            other = 1 - best_side
            unavoidable.append(
                {
                    "fragment": f.fid,
                    "component": comp_of.get(f.fid),
                    "conflicting_positions": sorted(side_errs[other]),
                    "explanation": (
                        "the fragment matches the hap0-pinned alleles at some positions and the "
                        "hap1-pinned alleles at others; no single orientation satisfies both sets"
                    ),
                }
            )
        side = orient.get(f.fid)
        if side is None:
            continue
        pins = l0 if side == 0 else l1
        for p, a in f.alleles.items():
            if p in pins and pins[p] != a:
                conflicting.setdefault(p, []).append(f.fid)

    lock_rows = []
    for p in sorted(ds.locks):
        frags = sorted(set(conflicting.get(p, [])))
        lock_rows.append(
            {
                "position": p,
                "lock": ds.locks[p].describe(),
                "conflict_fragments": frags,
                "n_conflicts": len(frags),
                "status": "contradicted" if frags else "consistent",
            }
        )
    return {
        "unavoidable": unavoidable,
        "n_unavoidable": len(unavoidable),
        "locks": lock_rows,
        "n_contradicted_locks": sum(1 for r in lock_rows if r["status"] == "contradicted"),
        "conflict_fragments": sorted(
            {fid for v in conflicting.values() for fid in v}
            | {row["fragment"] for row in unavoidable}
        ),
    }


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------

def coverage_blocks(ds: Dataset) -> List[Dict[str, object]]:
    if not ds.positions:
        return []
    positions = sorted(ds.positions)
    blocks: List[Dict[str, object]] = []
    start = prev = positions[0]
    for p in positions[1:]:
        if p != prev + 1:
            blocks.append({"start": start, "end": prev, "n_positions": prev - start + 1})
            start = p
        prev = p
    blocks.append({"start": start, "end": prev, "n_positions": prev - start + 1})
    return blocks


def gaps(ds: Dataset) -> List[Dict[str, object]]:
    blocks = coverage_blocks(ds)
    return [
        {
            "after": a["end"],
            "before": b["start"],
            "length": int(b["start"]) - int(a["end"]) - 1,
            "evidence": "none",
            "note": "no fragment covers these coordinates; their relation is not observed",
        }
        for a, b in zip(blocks, blocks[1:])
    ]


# ---------------------------------------------------------------------------
# Global solution assembly
# ---------------------------------------------------------------------------

def _blank_sequence(positions: Sequence[int]) -> List[str]:
    return ["-"] * len(positions)


def _fill(seq: List[str], index: Dict[int, int], comp_positions, hap_string: str) -> None:
    for off, p in enumerate(comp_positions):
        seq[index[p]] = hap_string[off]


def _per_component_flips(comps, sols, hap0: str, hap1: str, positions) -> List[Dict[str, object]]:
    """Components whose two labels may be exchanged independently of the rest."""
    index = {p: i for i, p in enumerate(positions)}
    out = []
    for comp, sol in zip(comps, sols):
        if not (sol.flip_free and not sol.order_pinned):
            continue
        h0, h1 = list(hap0), list(hap1)
        for p in comp.positions:
            k = index[p]
            h0[k], h1[k] = h1[k], h0[k]
        out.append({"component": comp.index, "hap0": "".join(h0), "hap1": "".join(h1)})
    return out


def _sequence_of(comps, sols, variant_idx: Sequence[int], which: str, positions):
    index = {p: i for i, p in enumerate(positions)}
    out = _blank_sequence(positions)
    for comp, sol, vi in zip(comps, sols, variant_idx):
        v = sol.variants[vi]
        _fill(out, index, comp.positions, v[which])
    return "".join(out)


def solve_dataset(
    ds: Dataset,
    options: SolveOptions,
    cache: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Solve a dataset, reusing unchanged components from ``cache``.

    ``cache`` is a mapping from :func:`component_cache_key` to a serialised
    component solution (see :func:`build_cache_from_result`).  A component is
    reused only when its positions, fragment observations and locks are all
    unchanged; a new connector fragment therefore recomputes exactly the
    components it touches (possibly merging several).
    """
    t0 = time.perf_counter()
    opts = options.normalised()
    ds.recompute_positions()
    ds.validate()
    comps = build_components(ds)
    cache = dict(cache or {})

    sols: List[CompSolution] = []
    reused = 0
    for comp in comps:
        key = component_cache_key(comp)
        cached = cache.get(key)
        if cached is not None:
            try:
                sols.append(_comp_solution_from_cache(comp, cached))  # type: ignore[arg-type]
                reused += 1
                continue
            except (KeyError, TypeError, ValueError):
                pass
        deadline = time.perf_counter() + opts.time_limit_s
        sols.append(solve_component(comp, deadline, engine=opts.engine, max_nodes=opts.max_nodes))

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    cost = sum(s.cost for s in sols)
    lock_lb = lock_error_lower_bound(ds)
    proven_lb = sum(s.lower_bound for s in sols)
    lower_bound = max(proven_lb, lock_lb)
    upper_bound = sum(s.upper_bound for s in sols)
    timed_comps = [s.index for s in sols if s.status != "optimal"]
    status = "timeout" if timed_comps else "optimal"
    proven = not timed_comps and lower_bound == cost

    # --- equivalence classes across components ---------------------------
    # ``s.n_orientation_classes`` counts *ordered* reconstructions of the
    # component (every optimal allele pair at every position, hap0/hap1 not
    # identified).  The whole solution's reconstruction is the product over
    # components, and the only remaining symmetry is the global swap of the
    # two labels, which acts freely on every non-palindromic reconstruction.
    per_comp = []
    raw_classes = 1
    n_palindromic = 1
    for s in sols:
        count = s.n_orientation_classes
        known = count is not None
        per_comp.append(
            {
                "component": s.index,
                "classes": count,
                "classes_known": known,
                "flip_free": bool(s.flip_free and not s.order_pinned),
                "order_pinned": s.order_pinned,
                "variants_listed": len(s.variants),
                "variants_truncated": s.variants_truncated,
            }
        )
        if known:
            raw_classes *= max(1, count)  # type: ignore[arg-type]
            n_palindromic *= s.n_palindromic
    # A global swap of the two labels is only a symmetry when *every* component
    # admits it; one order-pinned or non-flip-symmetric block breaks it.
    global_flip_free = all(s.flip_free for s in sols)
    any_order_pin = any(s.order_pinned for s in sols)
    label_quotient = 2 if global_flip_free else 1
    classes_known = all(p["classes_known"] for p in per_comp)
    if classes_known and label_quotient == 2:
        # Burnside for a free involution: orbits = (fixed + rest/2).
        n_orbits = (raw_classes + n_palindromic) // 2
    else:
        n_orbits = raw_classes

    # --- representative solutions -----------------------------------------
    # Each component's ``variants`` are its distinct *ordered* optimal
    # reconstructions, so the product over components gives the whole ordered
    # optimum.  When the global label swap is a symmetry we list one ordering per
    # unordered pair; the reported count already uses the same quotient.
    choices = [list(range(max(1, len(s.variants)))) for s in sols]

    reps: List[Dict[str, object]] = []
    seen_reps = set()
    truncated_reps = False
    for combo in itertools.product(*choices):
        hap0 = _sequence_of(comps, sols, combo, "hap0", ds.positions)
        hap1 = _sequence_of(comps, sols, combo, "hap1", ds.positions)
        key = (hap1, hap0) if (label_quotient == 2 and hap1 < hap0) else (hap0, hap1)
        if key in seen_reps:
            continue
        seen_reps.add(key)
        if len(reps) >= MAX_GLOBAL_SOLUTIONS:
            truncated_reps = True
            break
        orientation: Dict[str, int] = {}
        errors: Dict[str, List[int]] = {}
        for sol, k in zip(sols, combo):
            v = sol.variants[k] if sol.variants else {}
            orientation.update(v.get("orientation", {}))  # type: ignore[arg-type]
            errors.update(v.get("errors", {}))  # type: ignore[arg-type]
        reps.append(
            {
                "id": len(reps),
                "cost": sum(
                    (sol.variants[k]["cost"] if sol.variants else sol.cost)
                    for sol, k in zip(sols, combo)
                ),
                "hap0": hap0,
                "hap1": hap1,
                "label_swapped": hap1 < hap0,
                "component_variant": {str(c.index): k for c, k in zip(comps, combo)},
                "orientation": orientation,
                "errors": errors,
                "per_component_flip": _per_component_flips(comps, sols, hap0, hap1, ds.positions),
            }
        )
    reps_capped = truncated_reps or any(s.variants_truncated for s in sols)

    # --- per-position detail ---------------------------------------------
    cov = ds.coverage()
    comp_of_pos: Dict[int, int] = {}
    for c in comps:
        for p in c.positions:
            comp_of_pos[p] = c.index
    pos_rows = []
    for p in ds.positions:
        ci = comp_of_pos[p]
        sol = sols[ci]
        n_err = 0
        for fid, ps in sol.errors.items():
            if p in ps:
                errbad = True
                n_err += 1
        pos_rows.append(
            {
                "position": p,
                "coverage": cov.get(p, 0),
                "fragments": sorted(f.fid for f in ds.all_fragments() if p in f.alleles),
                "component": ci,
                "hap0": sol.hap0.get(p),
                "hap1": sol.hap1.get(p),
                "lock": ds.locks[p].describe() if p in ds.locks else None,
                "orientation_ambiguous": p in sol.ambiguous_positions,
                "error_count": n_err,
                "pair_options": sol.pair_options.get(p, []),
            }
        )

    comp_payload = []
    for comp, sol in zip(comps, sols):
        comp_payload.append(
            {
                "cache_key": component_cache_key(comp),
                "index": sol.index,
                "start": comp.positions[0],
                "end": comp.positions[-1],
                "positions": list(comp.positions),
                "fragment_ids": [f.fid for f in comp.fragments],
                "n_positions": len(comp.positions),
                "n_fragments": len(comp.fragments),
                "n_observations": sum(len(f) for f in comp.fragments),
                "status": sol.status,
                "proven": sol.proven,
                "cost": sol.cost,
                "lower_bound": sol.lower_bound,
                "upper_bound": sol.upper_bound,
                "hap0": "".join(str(sol.hap0[p]) for p in comp.positions),
                "hap1": "".join(str(sol.hap1[p]) for p in comp.positions),
                "alleles": {str(p): [sol.hap0[p], sol.hap1[p]] for p in comp.positions},
                "orientation": sol.orientation,
                "errors": sol.errors,
                "n_errors": sum(len(v) for v in sol.errors.values()),
                "n_optimal_vectors": sol.n_optimal_vectors,
                "n_orientation_classes": sol.n_orientation_classes,
                "classes_known": sol.n_orientation_classes is not None,
                "variants_truncated": sol.variants_truncated,
                "variants": sol.variants[:32],
                "n_variants_listed": len(sol.variants),
                "phase": sol.phase if opts.include_phase else {"status": "disabled"},
                "ambiguous_positions": sol.ambiguous_positions,
                "pair_options_detail": {
                    str(p): sol.pair_options[p]
                    for p in sol.ambiguous_positions
                    if p in sol.pair_options and len(sol.pair_options[p]) < 4
                },
                "engine": sol.engine,
                "engine_note": sol.engine_note,
                "nodes": sol.nodes,
                "elapsed_ms": sol.elapsed_ms,
                "order_pinned": sol.order_pinned,
                "flip_free": bool(sol.flip_free and not sol.order_pinned),
                "enumeration_limit": ENUM_MAX_N,
                "exact_engine": sol.engine.startswith("enumeration"),
            }
        )

    conflicts = detect_conflicts(ds, comps, sols)
    gap_list = gaps(ds)
    undetermined: List[Dict[str, object]] = []
    for c in comp_payload:
        if c["flip_free"]:
            undetermined.append(
                {
                    "kind": "flip",
                    "component": c["index"],
                    "start": c["start"],
                    "end": c["end"],
                    "note": (
                        "no order-pinning lock here: swapping hap0 and hap1 fits the data "
                        "equally well"
                    ),
                }
            )
        if c["status"] != "optimal":
            undetermined.append(
                {
                    "kind": "unproven",
                    "component": c["index"],
                    "start": c["start"],
                    "end": c["end"],
                    "note": "solver budget exhausted; the reported cost is an upper bound",
                }
            )
        amb = [p for p in c["ambiguous_positions"] if p in c["pair_options_detail"]]
        if amb:
            undetermined.append(
                {
                    "kind": "position_pairs",
                    "component": c["index"],
                    "start": c["start"],
                    "end": c["end"],
                    "positions": amb[:200],
                    "note": "more than one haplotype pair attains the optimum at these positions",
                }
            )
    for g in gap_list:
        undetermined.append(
            {
                "kind": "gap",
                "component": None,
                "start": g["after"],
                "end": g["before"],
                "note": "coordinate gap with no observations; sequence here is undetermined",
            }
        )

    warnings: List[str] = []
    if timed_comps:
        warnings.append(
            "at least one component hit the solver budget: its cost is an upper bound and "
            "the global lower bound may not be tight"
        )
    if len(comps) > 1:
        warnings.append(
            f"{len(comps)} disconnected coverage components: nothing links them, so the "
            "sequence is determined block by block, not as one certain string"
        )
    if conflicts["n_unavoidable"]:
        warnings.append(
            f"{conflicts['n_unavoidable']} fragment(s) cannot satisfy all locks simultaneously"
        )
    if global_flip_free and not any_order_pin:
        warnings.append(
            "no lock pins which haplotype is hap0: the whole solution is only defined up to "
            "a global label swap"
        )
    if not proven:
        warnings.append(
            f"not proven optimal: cost {cost}, proven lower bound {lower_bound}"
        )

    return {
        "schema": RESULT_SCHEMA,
        "solver_version": SOLVER_VERSION,
        "signature": dataset_signature(ds),
        "options": opts.to_json(),
        "status": status,
        "proven": proven,
        "cost": cost,
        "lower_bound": lower_bound,
        "upper_bound": upper_bound,
        "component_lower_bound": proven_lb,
        "lock_lower_bound": lock_lb,
        "gap_ms": elapsed_ms,
        "positions": list(ds.positions),
        "n_positions": len(ds.positions),
        "n_components": len(comps),
        "reused_components": reused,
        "timed_out_components": timed_comps,
        "components": comp_payload,
        "positions_detail": pos_rows,
        "gaps": gap_list,
        "coverage_blocks": coverage_blocks(ds),
        "undetermined": undetermined,
        "equivalence": {
            "classes_known": classes_known,
            "raw_classes_product": raw_classes if classes_known else None,
            "palindromic_reconstructions": n_palindromic if classes_known else None,
            "label_quotient": label_quotient,
            "n_solutions_modulo_labels": n_orbits if classes_known else None,
            "formula": (
                "product of ordered optima per block; divided by the global hap0/hap1 "
                "swap when no lock fixes the labels (Burnside: (n + palindromic) / 2)"
            ),
            "global_flip_free": global_flip_free,
            "labels_fixed_by_lock": any_order_pin,
            "per_component": per_comp,
            "representatives_listed": len(reps),
            "representatives_capped": reps_capped,
        },
        "solutions": reps,
        "conflicts": conflicts,
        "warnings": warnings,
        "dataset_summary": _summary(ds),
    }


def _summary(ds: Dataset) -> Dict[str, object]:
    from ..model import dataset_summary

    return dataset_summary(ds)


# ---------------------------------------------------------------------------
# Incremental helpers
# ---------------------------------------------------------------------------

def build_cache_from_result(result: Dict[str, object]) -> Dict[str, object]:
    cache: Dict[str, object] = {}
    for c in result.get("components", []):  # type: ignore[union-attr]
        key = c.get("cache_key")  # type: ignore[union-attr]
        if key:
            cache[key] = c
    return cache


def affected_components(result: Dict[str, object], frag: Fragment) -> List[int]:
    touched = []
    for c in result.get("components", []):  # type: ignore[union-attr]
        if set(c["positions"]) & set(frag.positions):  # type: ignore[index]
            touched.append(int(c["index"]))  # type: ignore[arg-type]
    return touched


def merge_hint(result: Dict[str, object], before: int, after: int) -> Dict[str, object]:
    return {
        "components_before": before,
        "components_after": after,
        "merged": after < before,
        "recomputed": [],
    }
