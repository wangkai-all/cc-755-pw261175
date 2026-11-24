"""Incremental solve jobs.

A *job* is one user action that changes the problem (import, add connector,
pin a position, clear a pin, change options) followed by a solve.  Each job
records which components it recomputed and why, so the UI can show that adding
a connector merged two formerly independent blocks and re-solved them as one.

The component cache is keyed by the exact contents of the component (positions,
fragment observations, locks), so any component that a new fragment does not
touch is reused verbatim.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..model import Dataset, Fragment
from .base import build_components
from .solve import (
    SolveOptions,
    build_cache_from_result,
    component_cache_key,
    solve_dataset,
)


@dataclass
class JobOutcome:
    result: Dict[str, object]
    cache: Dict[str, object]
    report: Dict[str, object]


def describe_change(
    before_result: Optional[Dict[str, object]],
    before_components: Optional[List[List[int]]],
    ds: Dataset,
) -> Dict[str, object]:
    comps = build_components(ds)
    after = [list(c.positions) for c in comps]
    if before_components is None:
        return {
            "kind": "initial",
            "components_before": 0,
            "components_after": len(after),
            "merged": False,
            "merged_groups": [],
            "new_components": list(range(len(after))),
        }
    # A merge happened when an "after" component contains positions of >= 2
    # "before" components.
    merged_groups = []
    for idx, positions in enumerate(after):
        pos = set(positions)
        origins = [
            i
            for i, old in enumerate(before_components)
            if pos & set(old)
        ]
        if len(origins) > 1:
            merged_groups.append({"components": origins, "into": idx})
    return {
        "kind": "merge" if merged_groups else "extend",
        "components_before": len(before_components),
        "components_after": len(after),
        "merged": bool(merged_groups),
        "merged_groups": merged_groups,
        "new_components": [
            i for i, positions in enumerate(after)
            if not any(set(positions) & set(old) for old in before_components)
        ],
    }


def run_job(
    ds: Dataset,
    options: SolveOptions,
    action: str,
    previous_result: Optional[Dict[str, object]] = None,
    payload: Optional[Dict[str, object]] = None,
) -> JobOutcome:
    t0 = time.perf_counter()
    # A previous result stores a solved component as JSON (haplotype strings,
    # for example).  Re-key it into the shape the solver's cache loader expects.
    cache: Dict[str, object] = {}
    if previous_result:
        from .solve import _component_payload_to_cache_entry

        for comp in previous_result.get("components", []):  # type: ignore[union-attr]
            key = comp.get("cache_key")  # type: ignore[union-attr]
            if key:
                cache[str(key)] = _component_payload_to_cache_entry(comp)  # type: ignore[arg-type]
    before_components = None
    if previous_result:
        before_components = [list(c["positions"]) for c in previous_result.get("components", [])]  # type: ignore[union-attr]

    result = solve_dataset(ds, options, cache=cache)

    change = describe_change(previous_result, before_components, ds)
    after_keys = {c["cache_key"] for c in result["components"]}  # type: ignore[index]
    recomputed = [
        c["index"] for c in result["components"]  # type: ignore[index]
        if c["engine"] != "reused"
    ]
    reused = [
        c["index"] for c in result["components"]  # type: ignore[index]
        if str(c["engine"]).startswith("reused")
    ]
    report = {
        "action": action,
        "payload": payload or {},
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "change": change,
        "recomputed_components": recomputed,
        "reused_components": reused,
        "summary": _change_sentence(action, change, recomputed, reused),
    }
    return JobOutcome(result=result, cache=cache, report=report)


def _change_sentence(action: str, change: Dict[str, object], recomputed, reused) -> str:
    if action == "import":
        return (
            f"imported data forms {change['components_after']} coverage component(s); "
            f"all solved from scratch"
        )
    if action == "add_connector":
        if change.get("merged"):
            groups = "; ".join(
                "components " + ", ".join(str(i) for i in g["components"]) + f" -> {g['into']}"
                for g in change["merged_groups"]  # type: ignore[index]
            )
            return (
                f"connector bridges blocks ({groups}); the merged component was re-solved "
                f"as a whole, {len(reused)} untouched component(s) reused"
            )
        return (
            f"connector extends component(s) {recomputed}; re-solved those only, "
            f"{len(reused)} untouched component(s) reused"
        )
    if action in ("lock", "unlock"):
        return (
            f"{action} touches component(s) {recomputed}; those were re-solved with the "
            f"constraint as a hard requirement, {len(reused)} component(s) reused"
        )
    return f"{action}: recomputed {len(recomputed)} component(s), reused {len(reused)}"


# ---------------------------------------------------------------------------
# Conflict-focused connector suggestion
# ---------------------------------------------------------------------------

def suggest_connector_positions(
    result: Dict[str, object], ds: Dataset, max_gaps: int = 5
) -> List[Dict[str, object]]:
    """Cheap hints for where a connector would link two blocks.

    Purely informational: it lists coordinate gaps, not a synthesised fragment.
    The user decides what observations to add.
    """
    hints = []
    for g in result.get("gaps", [])[:max_gaps]:  # type: ignore[union-attr]
        hints.append(
            {
                "kind": "gap",
                "after": g["after"],
                "before": g["before"],
                "suggestion": (
                    f"a fragment covering positions {g['after'] + 1}..{g['before'] - 1} "
                    "would join the neighbouring blocks"
                ),
            }
        )
    for u in result.get("undetermined", []):  # type: ignore[union-attr]
        if u.get("kind") == "flip" and len(hints) < max_gaps:
            hints.append(
                {
                    "kind": "flip_resolution",
                    "component": u.get("component"),
                    "suggestion": (
                        "pin one position in this component (a lock) to fix which haplotype "
                        "is hap0"
                    ),
                }
            )
    return hints
