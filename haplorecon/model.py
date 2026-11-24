"""Core data structures for the two-haplotype reconstruction problem.

Everything in this project is synthetic or hand-authored.  It never reads,
derives or represents real human genetic data.

Vocabulary
----------
fragment   A short observed binary string covering a set of *positions*.  It is
           stored sparsely as {position: 0|1} plus an id.  Each fragment is
           assumed to come from exactly one of two latent sequences, but which
           one is a hidden variable.
position   A column index.  Positions are ordered; fragments may cover disjoint
           intervals, leaving gaps.
error      A fragment-position observation that disagrees with the allele of
           the haplotype the fragment was assigned to.  Every disagreement
           carries unit cost.
lock       A human-confirmed constraint on one position.  It may pin the
           ordered allele of haplotype 0, of haplotype 1, or require the pair
           to differ.  Locks are hard constraints; data that disagrees pays an
           error cost and is reported as a conflict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class DataError(ValueError):
    """Raised when imported fragment data or a lock is malformed."""


@dataclass(frozen=True)
class Fragment:
    fid: str
    alleles: Dict[int, int]  # position -> 0/1
    source_hint: Optional[str] = None  # displayed only, never used by solver

    @property
    def positions(self) -> List[int]:
        return sorted(self.alleles)

    @property
    def start(self) -> int:
        return min(self.alleles)

    @property
    def end(self) -> int:
        return max(self.alleles)

    def span(self) -> int:
        return max(self.alleles) - min(self.alleles) + 1

    def __len__(self) -> int:
        return len(self.alleles)


@dataclass(frozen=True)
class Lock:
    position: int
    hap0: Optional[int] = None
    hap1: Optional[int] = None
    distinct: bool = False
    note: str = ""

    def resolved(self) -> Tuple[Optional[int], Optional[int]]:
        """Return the ordered alleles implied by the lock.

        ``distinct`` is only used when it determines the missing side; when both
        sides are pinned it is checked by :meth:`validate`.
        """
        a0, a1 = self.hap0, self.hap1
        if self.distinct:
            if a0 is not None and a1 is None:
                a1 = 1 - a0
            elif a1 is not None and a0 is None:
                a0 = 1 - a1
        return a0, a1

    def is_empty(self) -> bool:
        return self.hap0 is None and self.hap1 is None and not self.distinct

    def validate(self) -> None:
        for name, val in (("hap0", self.hap0), ("hap1", self.hap1)):
            if val is not None and val not in (0, 1):
                raise DataError(f"lock at position {self.position}: {name} must be 0/1, got {val!r}")
        if self.distinct and self.hap0 is not None and self.hap1 is not None:
            if self.hap0 == self.hap1:
                raise DataError(
                    f"lock at position {self.position}: distinct=True but both haplotypes "
                    f"are pinned to {self.hap0}"
                )

    def describe(self) -> str:
        a0, a1 = self.resolved()
        if a0 is not None and a1 is not None:
            target = f"hap0={a0}, hap1={a1}"
        elif a0 is not None:
            target = f"hap0={a0}"
        elif a1 is not None:
            target = f"hap1={a1}"
        else:
            target = "haplotypes differ"
        return f"p{self.position}: {target}"

    @property
    def pins_order(self) -> bool:
        """True when the lock names an allele for a specific haplotype."""
        a0, a1 = self.resolved()
        return a0 is not None or a1 is not None


@dataclass
class Dataset:
    fragments: List[Fragment]
    positions: List[int]  # sorted union of observation positions
    name: str = "untitled"
    notes: str = ""
    locks: Dict[int, Lock] = field(default_factory=dict)
    connectors: List[Fragment] = field(default_factory=list)

    def all_fragments(self) -> List[Fragment]:
        return list(self.fragments) + list(self.connectors)

    def by_id(self) -> Dict[str, Fragment]:
        return {f.fid: f for f in self.all_fragments()}

    def coverage(self) -> Dict[int, int]:
        cov = {p: 0 for p in self.positions}
        for f in self.all_fragments():
            for p in f.alleles:
                cov[p] = cov.get(p, 0) + 1
        return cov

    def recompute_positions(self) -> None:
        self.positions = sorted({p for f in self.all_fragments() for p in f.alleles})

    def set_lock(self, lock: Lock) -> None:
        lock.validate()
        a0, a1 = lock.resolved()
        if a0 is None and a1 is None and not lock.distinct:
            self.locks.pop(lock.position, None)
        else:
            self.locks[lock.position] = lock

    def clear_lock(self, position: int) -> None:
        self.locks.pop(position, None)

    def ordered_lock_values(self) -> Tuple[Dict[int, int], Dict[int, int]]:
        """Return (hap0 fixed alleles, hap1 fixed alleles)."""
        l0: Dict[int, int] = {}
        l1: Dict[int, int] = {}
        for p, lock in self.locks.items():
            a0, a1 = lock.resolved()
            if a0 is not None:
                l0[p] = a0
            if a1 is not None:
                l1[p] = a1
        return l0, l1

    def validate(self) -> None:
        seen = set()
        for f in self.all_fragments():
            if f.fid in seen:
                raise DataError(f"duplicate fragment id {f.fid!r}")
            seen.add(f.fid)
            if not f.alleles:
                raise DataError(f"fragment {f.fid!r} has no observations")
            for p, a in f.alleles.items():
                if not isinstance(p, int) or p < 0:
                    raise DataError(f"fragment {f.fid!r}: bad position {p!r}")
                if a not in (0, 1):
                    raise DataError(f"fragment {f.fid!r}: allele at {p} must be 0/1, got {a!r}")
        known = set(self.positions)
        for p, lock in self.locks.items():
            if lock.position != p:
                raise DataError(f"lock keyed by {p} has position {lock.position}")
            lock.validate()
            if p not in known:
                raise DataError(f"lock at unobserved position {p}")
        if self.connectors:
            self.recompute_positions()


# ---------------------------------------------------------------------------
# Text import format
# ---------------------------------------------------------------------------
# Two accepted forms, one per line, blank lines and lines starting with '#'
# ignored.
#
#   FASTA-like:   >frag_id [start=N] [free-form note]
#                 1010 011?  ...        (continuation lines allowed)
#
#   TSV:          frag_id<TAB>start<TAB>0101?01
#
# '?' '-' '.' or 'N' marks a missing call (not an observation) and is dropped,
# so fragments stay sparse.  '|' and whitespace inside a sequence are visual
# spacers only.

_SEQ_JUNK = "| \t,"
_MISSING = set("?.-Nn")


def parse_fasta_sequence(fid: str, raw: str, start: int) -> Fragment:
    alleles: Dict[int, int] = {}
    pos = start
    for ch in raw:
        if ch in _SEQ_JUNK:
            continue
        if ch in _MISSING:
            pos += 1
            continue
        if ch == "0":
            alleles[pos] = 0
        elif ch == "1":
            alleles[pos] = 1
        else:
            raise DataError(f"fragment {fid!r}: unexpected character {ch!r}")
        pos += 1
    if not alleles:
        raise DataError(f"fragment {fid!r}: no called positions")
    return Fragment(fid=fid, alleles=alleles)


def parse_text(text: str, name: str = "imported") -> Dataset:
    lines = text.splitlines()
    fragments: List[Fragment] = []
    i = 0
    fmt: Optional[str] = None
    while i < len(lines):
        line = lines[i].strip()
        lineno = i + 1
        i += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith(">"):
            if fmt == "tsv":
                raise DataError(f"line {lineno}: mixed FASTA and TSV input")
            fmt = "fasta"
            parts = line[1:].strip().split()
            if not parts:
                raise DataError(f"line {lineno}: empty fragment header")
            fid = parts[0]
            start = 0
            for extra in parts[1:]:
                if extra.startswith("start="):
                    try:
                        start = int(extra.split("=", 1)[1])
                    except ValueError:
                        raise DataError(f"line {lineno}: bad start= value {extra!r}")
            seq_parts = []
            while i < len(lines):
                nxt = lines[i].strip()
                if nxt.startswith(">"):
                    break
                if nxt and "\t" in nxt and not _looks_like_seq(nxt):
                    break
                i += 1
                if not nxt or nxt.startswith("#"):
                    continue
                seq_parts.append(nxt)
            fragments.append(parse_fasta_sequence(fid, "".join(seq_parts), start))
        elif "\t" in line:
            if fmt == "fasta":
                raise DataError(f"line {lineno}: mixed FASTA and TSV input")
            fmt = "tsv"
            cols = line.split("\t")
            if len(cols) < 3:
                raise DataError(f"line {lineno}: TSV needs id, start, sequence")
            fid = cols[0].strip()
            try:
                start = int(cols[1].strip())
            except ValueError:
                raise DataError(f"line {lineno}: bad start column {cols[1]!r}")
            fragments.append(parse_fasta_sequence(fid, cols[2], start))
        else:
            raise DataError(f"line {lineno}: expected '>id' header or tab-separated row")
    if not fragments:
        raise DataError("no fragments found in input")
    positions = sorted({p for f in fragments for p in f.alleles})
    ds = Dataset(fragments=fragments, positions=positions, name=name)
    ds.validate()
    ds.recompute_positions()
    return ds


def _looks_like_seq(s: str) -> bool:
    body = "".join(ch for ch in s if ch not in _SEQ_JUNK)
    return bool(body) and all(ch in "01?.-Nn" for ch in body)


def dataset_to_text(ds: Dataset) -> str:
    out = [f"# {ds.name}"]
    out.append(f"# fragments: {len(ds.fragments)}  positions: {len(ds.positions)}")
    if ds.locks:
        out.append("# locks: " + "; ".join(ds.locks[p].describe() for p in sorted(ds.locks)))
    for f in ds.all_fragments():
        tag = " connector" if f in ds.connectors else ""
        out.append(f">{f.fid} start={f.start}{tag}")
        out.append(
            "".join(
                str(f.alleles[p]) if p in f.alleles else "?"
                for p in range(f.start, f.end + 1)
            )
        )
    return "\n".join(out) + "\n"


def dataset_summary(ds: Dataset) -> Dict[str, object]:
    cov = ds.coverage()
    spans = [f.span() for f in ds.all_fragments()]
    return {
        "name": ds.name,
        "n_fragments": len(ds.fragments),
        "n_connectors": len(ds.connectors),
        "n_positions": len(ds.positions),
        "n_observations": sum(len(f) for f in ds.all_fragments()),
        "min_pos": min(ds.positions) if ds.positions else None,
        "max_pos": max(ds.positions) if ds.positions else None,
        "mean_span": round(sum(spans) / len(spans), 2) if spans else 0.0,
        "uncovered_positions": sum(1 for c in cov.values() if c == 0),
        "max_coverage": max(cov.values()) if cov else 0,
        "n_locks": len(ds.locks),
    }


# ---------------------------------------------------------------------------
# JSON (de)serialisation -- the on-disk / database form
# ---------------------------------------------------------------------------

def dataset_to_json(ds: Dataset) -> Dict[str, object]:
    def frag_json(f: Fragment) -> Dict[str, object]:
        return {
            "fid": f.fid,
            "alleles": {str(p): a for p, a in sorted(f.alleles.items())},
            "source_hint": f.source_hint,
        }

    return {
        "schema": "haplorecon/dataset/1",
        "name": ds.name,
        "notes": ds.notes,
        "fragments": [frag_json(f) for f in ds.fragments],
        "connectors": [frag_json(f) for f in ds.connectors],
        "locks": [
            {
                "position": lock.position,
                "hap0": lock.hap0,
                "hap1": lock.hap1,
                "distinct": lock.distinct,
                "note": lock.note,
            }
            for lock in (ds.locks[p] for p in sorted(ds.locks))
        ],
    }


def _frag_from_json(obj: Dict[str, object]) -> Fragment:
    fid = str(obj.get("fid", "")).strip()
    if not fid:
        raise DataError("fragment without id")
    raw = obj.get("alleles") or {}
    if not isinstance(raw, dict):
        raise DataError(f"fragment {fid!r}: alleles must be an object")
    alleles: Dict[int, int] = {}
    for k, v in raw.items():
        try:
            p = int(k)
        except (TypeError, ValueError):
            raise DataError(f"fragment {fid!r}: bad position key {k!r}")
        if v not in (0, 1):
            raise DataError(f"fragment {fid!r}: allele at {p} must be 0/1")
        alleles[p] = int(v)
    return Fragment(fid=fid, alleles=alleles, source_hint=obj.get("source_hint"))


def dataset_from_json(obj: Dict[str, object]) -> Dataset:
    if not isinstance(obj, dict):
        raise DataError("dataset must be a JSON object")
    frags = [_frag_from_json(f) for f in (obj.get("fragments") or [])]
    conns = [_frag_from_json(f) for f in (obj.get("connectors") or [])]
    locks: Dict[int, Lock] = {}
    for raw in obj.get("locks") or []:
        try:
            pos = int(raw["position"])
        except (KeyError, TypeError, ValueError):
            raise DataError("lock without a valid position")
        lock = Lock(
            position=pos,
            hap0=raw.get("hap0"),
            hap1=raw.get("hap1"),
            distinct=bool(raw.get("distinct", False)),
            note=str(raw.get("note", "")),
        )
        locks[pos] = lock
    if not frags and not conns:
        raise DataError("dataset has no fragments")
    ds = Dataset(
        fragments=frags,
        connectors=conns,
        positions=[],
        name=str(obj.get("name") or "untitled"),
        notes=str(obj.get("notes") or ""),
        locks=locks,
    )
    ds.recompute_positions()
    ds.validate()
    return ds
