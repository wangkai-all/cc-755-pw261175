"""Service layer: the use cases the UI calls.

Every state-changing operation follows the same pattern:

1. resolve the current input version of the dataset,
2. apply the edit to an in-memory copy,
3. validate it,
4. append a new immutable input version,
5. run a solve *job* that reuses unchanged components,
6. append a new immutable result version.

Anything invalid leaves both inputs and results untouched.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .model import (
    DataError,
    Dataset,
    Fragment,
    Lock,
    dataset_summary,
    parse_text,
)
from .solver.jobs import run_job, suggest_connector_positions
from .solver.solve import SolveOptions
from .store import Store, StoredResult, StoredVersion


class ServiceError(Exception):
    """User-facing error with a machine-readable kind."""

    def __init__(self, kind: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, object]:
        return {"error": self.kind, "message": self.message, "detail": self.detail}


def _options_from(payload: Optional[Dict[str, object]]) -> SolveOptions:
    payload = payload or {}
    try:
        return SolveOptions(
            time_limit_s=float(payload.get("time_limit_s", 20.0)),
            engine=str(payload.get("engine", "auto")),
            max_variants=int(payload.get("max_variants", 200)),
            include_phase=bool(payload.get("include_phase", True)),
            max_nodes=int(payload.get("max_nodes", 300_000)),
        ).normalised()
    except (TypeError, ValueError) as exc:
        raise ServiceError("bad_options", f"invalid solve options: {exc}")


@dataclass
class DatasetView:
    dataset_id: str
    name: str
    notes: str
    created_at: str
    versions: List[Dict[str, object]]
    latest_version: Dict[str, object]
    latest_result_version: Optional[int]
    dataset: Dataset

    def to_dict(self) -> Dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "notes": self.notes,
            "created_at": self.created_at,
            "versions": self.versions,
            "latest_version": self.latest_version,
            "latest_result_version": self.latest_result_version,
            "summary": dataset_summary(self.dataset),
            "dataset": _dataset_payload(self.dataset),
        }


def _dataset_payload(ds: Dataset) -> Dict[str, object]:
    return {
        "name": ds.name,
        "notes": ds.notes,
        "positions": list(ds.positions),
        "fragments": [
            {
                "fid": f.fid,
                "start": f.start,
                "end": f.end,
                "alleles": {str(p): a for p, a in sorted(f.alleles.items())},
                "span": f.span(),
                "connector": any(f.fid == c.fid for c in ds.connectors),
            }
            for f in ds.all_fragments()
        ],
        "locks": [
            {
                "position": ds.locks[p].position,
                "hap0": ds.locks[p].hap0,
                "hap1": ds.locks[p].hap1,
                "distinct": ds.locks[p].distinct,
                "note": ds.locks[p].note,
                "description": ds.locks[p].describe(),
            }
            for p in sorted(ds.locks)
        ],
        "connector_ids": [c.fid for c in ds.connectors],
    }


class Service:
    def __init__(self, store: Store):
        self.store = store

    # -- import -----------------------------------------------------------

    def import_text(
        self,
        text: str,
        name: Optional[str] = None,
        notes: str = "",
        options: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        if not text or not text.strip():
            raise ServiceError("empty_input", "the import text is empty")
        dataset_name = (name or "").strip() or "synthetic import"
        if len(text) > 4_000_000:
            raise ServiceError("too_large", "import text exceeds 4 MB")
        try:
            ds = parse_text(text, name=dataset_name)
        except DataError as exc:
            raise ServiceError("parse_error", str(exc))
        ds.notes = notes
        if len(ds.positions) > 20_000:
            raise ServiceError(
                "too_large",
                f"{len(ds.positions)} positions exceeds the 20000-position demo limit",
            )
        if len(ds.all_fragments()) > 5_000:
            raise ServiceError("too_large", "more than 5000 fragments is outside demo limits")
        ds.validate()
        opts = _options_from(options)
        did = self.store.create_dataset(ds, source="import")
        return self._run_and_store(did, ds, opts, action="import", payload={"name": dataset_name})

    # -- incremental actions ---------------------------------------------

    def add_connector(
        self,
        dataset_id: str,
        fid: str,
        alleles: Dict[int, int],
        options: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        ds = self._current_dataset(dataset_id)
        fid = (fid or "").strip()
        if not fid:
            raise ServiceError("bad_fragment", "connector id is required")
        if any(f.fid == fid for f in ds.all_fragments()):
            raise ServiceError("duplicate_id", f"fragment id {fid!r} already exists")
        if not alleles:
            raise ServiceError("bad_fragment", "connector needs at least one observation")
        for p, a in alleles.items():
            if not isinstance(p, int) or p < 0:
                raise ServiceError("bad_fragment", f"bad position {p!r}")
            if a not in (0, 1):
                raise ServiceError("bad_fragment", f"allele at {p} must be 0/1")
        known = set(ds.positions)
        unknown = sorted(set(alleles) - known)
        ds.connectors.append(Fragment(fid=fid, alleles=dict(alleles)))
        ds.recompute_positions()
        try:
            ds.validate()
        except DataError as exc:
            raise ServiceError("bad_fragment", str(exc))
        opts = _options_from(options)
        return self._run_and_store(
            dataset_id,
            ds,
            opts,
            action="add_connector",
            payload={"fid": fid, "alleles": {str(k): v for k, v in sorted(alleles.items())},
                     "extends_beyond_known_positions": unknown},
        )

    def lock_position(
        self,
        dataset_id: str,
        position: int,
        hap0: Optional[int] = None,
        hap1: Optional[int] = None,
        distinct: bool = False,
        note: str = "",
        options: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        ds = self._current_dataset(dataset_id)
        if position not in ds.positions:
            raise ServiceError(
                "bad_lock", f"position {position} has no observations; there is nothing to lock"
            )
        lock = Lock(position=position, hap0=hap0, hap1=hap1, distinct=bool(distinct), note=note)
        try:
            lock.validate()
        except DataError as exc:
            raise ServiceError("bad_lock", str(exc))
        if lock.is_empty():
            raise ServiceError("bad_lock", "a lock must pin hap0, hap1, or require distinctness")
        ds.set_lock(lock)
        opts = _options_from(options)
        return self._run_and_store(
            dataset_id,
            ds,
            opts,
            action="lock",
            payload={"position": position, "hap0": hap0, "hap1": hap1,
                     "distinct": bool(distinct), "note": note},
        )

    def unlock_position(
        self, dataset_id: str, position: int, options: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        ds = self._current_dataset(dataset_id)
        if position not in ds.locks:
            raise ServiceError("no_such_lock", f"position {position} is not locked")
        ds.clear_lock(position)
        opts = _options_from(options)
        return self._run_and_store(
            dataset_id, ds, opts, action="unlock", payload={"position": position}
        )

    def lock_conflicts(
        self, dataset_id: str, conflict: Union[Dict[str, object], Sequence[str]], options=None
    ) -> Dict[str, object]:
        """Pin the unordered pair at a position to what the reported result uses.

        This is the 'accept current reconstruction' action: it records a lock
        that is consistent with the chosen solution, useful when the user has
        inspected the evidence and wants to freeze the choice.
        """
        ds = self._current_dataset(dataset_id)
        result = self.current_result(dataset_id)
        if "position" in conflict:
            position = int(conflict["position"])  # type: ignore[arg-type]
        else:
            frag_ids = [str(x) for x in conflict]
            by_id = ds.by_id()
            positions = sorted({p for fid in frag_ids if fid in by_id for p in by_id[fid].alleles})
            if not positions:
                raise ServiceError("bad_lock", "no positions to lock from those fragments")
            position = positions[0]
        comp_row = next(
            (c for c in result.document.get("components", []) if str(position) in c["alleles"]),  # type: ignore[union-attr,index]
            None,
        )
        if comp_row is None:
            raise ServiceError("bad_lock", f"position {position} is not in the current result")
        pair = comp_row["alleles"][str(position)]  # type: ignore[index]
        ds.set_lock(Lock(position=position, hap0=int(pair[0]), hap1=int(pair[1]), distinct=True))
        opts = _options_from(options)
        return self._run_and_store(
            dataset_id, ds, opts, action="lock_pair",
            payload={"position": position, "hap0": pair[0], "hap1": pair[1]},
        )

    def resolve(
        self, dataset_id: str, options: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        """Re-solve the current inputs with new options; appends a result version."""
        ds = self._current_dataset(dataset_id)
        opts = _options_from(options)
        return self._run_and_store(
            dataset_id, ds, opts, action="resolve", payload={"options": opts.to_json()}
        )

    # -- reads ------------------------------------------------------------

    def current_dataset(self, dataset_id: str) -> Dataset:
        return self._current_dataset(dataset_id)

    def _current_dataset(self, dataset_id: str) -> Dataset:
        if not self.store.dataset_exists(dataset_id):
            raise ServiceError("not_found", f"dataset {dataset_id!r} not found")
        try:
            v = self.store.latest_version(dataset_id)
        except KeyError:
            raise ServiceError("not_found", f"dataset {dataset_id!r} has no input versions")
        return v.dataset

    def dataset_view(self, dataset_id: str) -> Dict[str, object]:
        row = self.store.dataset_row(dataset_id)
        version = self.store.latest_version(dataset_id)
        results = self.store.list_results(dataset_id)
        view = DatasetView(
            dataset_id=dataset_id,
            name=str(row["name"]),
            notes=str(row["notes"]),
            created_at=str(row["created_at"]),
            versions=self.store.list_versions(dataset_id),
            latest_version={
                "seq": version.seq,
                "created_at": version.created_at,
                "label": version.label,
                "signature": version.signature,
            },
            latest_result_version=results[0]["version"] if results else None,
            dataset=version.dataset,
        )
        return view.to_dict()

    def current_result(self, dataset_id: str, version: Optional[int] = None) -> StoredResult:
        if not self.store.dataset_exists(dataset_id):
            raise ServiceError("not_found", f"dataset {dataset_id!r} not found")
        try:
            return self.store.get_result(dataset_id, version)
        except KeyError as exc:
            raise ServiceError("no_result", str(exc))

    def result_payload(self, dataset_id: str, version: Optional[int] = None) -> Dict[str, object]:
        stored = self.current_result(dataset_id, version)
        job = self.store.get_job(stored.job_id)
        return {
            "dataset_id": dataset_id,
            "result_version": stored.version,
            "created_at": stored.created_at,
            "input_version_id": stored.input_version_id,
            "job": {
                "id": job["id"],
                "action": job["action"],
                "created_at": job["created_at"],
                "elapsed_ms": job["elapsed_ms"],
                "status": job["status"],
                "report": job["report"],
            },
            "solve": stored.solve,
            "summary": stored.summary,
            "result": stored.document,
            "result_versions": self.store.list_results(dataset_id),
            "dataset": _dataset_payload(self._current_dataset(dataset_id)),
        }

    def compare_results(
        self, dataset_id: str, a: int, b: int
    ) -> Dict[str, object]:
        ra = self.current_result(dataset_id, a)
        rb = self.current_result(dataset_id, b)
        da = ra.document
        db = rb.document

        def class_map(doc):
            out = {}
            for sol in doc.get("solutions", []):  # type: ignore[union-attr]
                out[sol["id"]] = (sol["hap0"], sol["hap1"])
            return out

        ha, hb = class_map(da), class_map(db)
        positions = sorted(set(da.get("positions", [])) | set(db.get("positions", [])))  # type: ignore[arg-type]
        pos_map_a = {r["position"]: r for r in da.get("positions_detail", [])}  # type: ignore[union-attr]
        pos_map_b = {r["position"]: r for r in db.get("positions_detail", [])}  # type: ignore[union-attr]
        diffs = []
        for p in positions:
            pa = pos_map_a.get(p)
            pb = pos_map_b.get(p)
            va = (pa["hap0"], pa["hap1"]) if pa else None
            vb = (pb["hap0"], pb["hap1"]) if pb else None
            if va != vb:
                diffs.append({"position": p, "a": va, "b": vb})
        return {
            "a": {"version": ra.version, "created_at": ra.created_at,
                  "cost": da.get("cost"), "status": da.get("status"),
                  "n_components": da.get("n_components")},
            "b": {"version": rb.version, "created_at": rb.created_at,
                  "cost": db.get("cost"), "status": db.get("status"),
                  "n_components": db.get("n_components")},
            "differing_positions": diffs[:500],
            "n_differing_positions": len(diffs),
            "identical": not diffs and da.get("cost") == db.get("cost"),
        }

    def compare_solutions(self, dataset_id: str, result_version: Optional[int],
                          a: int, b: int) -> Dict[str, object]:
        stored = self.current_result(dataset_id, result_version)
        doc = stored.document
        sols = {s["id"]: s for s in doc.get("solutions", [])}  # type: ignore[union-attr]
        if a not in sols or b not in sols:
            raise ServiceError("bad_solution", "solution id not found in this result version")
        sa, sb = sols[a], sols[b]
        positions = doc.get("positions", [])
        diffs = []
        for i, p in enumerate(positions):  # type: ignore[arg-type]
            pair_a = (sa["hap0"][i], sa["hap1"][i])
            pair_b = (sb["hap0"][i], sb["hap1"][i])
            if pair_a != pair_b:
                diffs.append(
                    {
                        "position": p,
                        "a": list(pair_a),
                        "b": list(pair_b),
                        "flipped_only": (pair_a[0], pair_a[1]) == (pair_b[1], pair_b[0]),
                    }
                )
        return {
            "a": {"id": a, "cost": sa["cost"]},
            "b": {"id": b, "cost": sb["cost"]},
            "differing_positions": diffs[:500],
            "n_differing_positions": len(diffs),
            "flip_only_difference": bool(diffs) and all(d["flipped_only"] for d in diffs),
        }

    def hints(self, dataset_id: str, result_version: Optional[int] = None) -> Dict[str, object]:
        stored = self.current_result(dataset_id, result_version)
        ds = self._current_dataset(dataset_id)
        return {"hints": suggest_connector_positions(stored.document, ds)}

    def export_input_text(self, dataset_id: str) -> Tuple[str, str]:
        from .model import dataset_to_text

        ds = self._current_dataset(dataset_id)
        v = self.store.latest_version(dataset_id)
        return dataset_to_text(ds), f"dataset-{dataset_id}-v{v.seq}.txt"

    def export_result_json(self, dataset_id: str, version: Optional[int] = None) -> Tuple[str, str]:
        payload = self.result_payload(dataset_id, version)
        return (
            json.dumps(payload, indent=2, sort_keys=True),
            f"dataset-{dataset_id}-result-v{payload['result_version']}.json",
        )

    # -- internals --------------------------------------------------------

    def _run_and_store(
        self,
        dataset_id: str,
        ds: Dataset,
        opts: SolveOptions,
        action: str,
        payload: Dict[str, object],
    ) -> Dict[str, object]:
        ds.validate()
        label = {
            "import": "import",
            "add_connector": f"connector {payload.get('fid')}",
            "lock": f"lock p{payload.get('position')}",
            "unlock": f"unlock p{payload.get('position')}",
            "lock_pair": f"lock pair p{payload.get('position')}",
        }.get(action, action)
        version = self.store.add_version(dataset_id, ds, label=label)

        previous = None
        results = self.store.list_results(dataset_id)
        if results:
            try:
                previous = self.store.get_result(dataset_id, results[0]["version"]).document
            except KeyError:
                previous = None

        job_id = self.store.start_job(dataset_id, version.id, action, payload)
        try:
            outcome = run_job(ds, opts, action, previous, payload)
        except Exception as exc:  # noqa: BLE001 - reported to the caller as a failed job
            self.store.finish_job(job_id, "failed", 0, {}, error=f"{type(exc).__name__}: {exc}")
            raise ServiceError("solve_failed", f"solver error: {exc}")
        res = outcome.result
        summary = {
            "status": res["status"],
            "proven": res["proven"],
            "cost": res["cost"],
            "lower_bound": res["lower_bound"],
            "upper_bound": res["upper_bound"],
            "n_components": res["n_components"],
            "n_positions": res["n_positions"],
            "n_fragments": res["dataset_summary"]["n_fragments"],  # type: ignore[index]
            "n_undetermined": len(res["undetermined"]),
            "n_conflicting_fragments": len(res["conflicts"]["conflict_fragments"]),  # type: ignore[index]
            "n_unavoidable_conflicts": res["conflicts"]["n_unavoidable"],  # type: ignore[index]
            "n_classes": res["equivalence"]["n_solutions_modulo_labels"],  # type: ignore[index]
            "elapsed_ms": res["gap_ms"],
            "action": action,
            "label": label,
        }
        self.store.finish_job(job_id, "ok", res["gap_ms"], outcome.report)
        rversion = self.store.add_result(job_id, dataset_id, version.id, opts.to_json(), summary, res)
        return self.result_payload(dataset_id, rversion)


# ``Union`` is only needed for a type annotation above; import lazily to keep
# the module import cheap for the CLI.
from typing import Union  # noqa: E402  (kept at the bottom intentionally)
