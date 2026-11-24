"""SQLite persistence: datasets, imports, solve jobs, result versions.

Nothing here touches real genetic data.  Every dataset is synthetic or
hand-authored; the schema stores exactly what the user imported plus the
derived results, so that a report can always be traced back to inputs.

Schema (all timestamps are UTC ISO-8601 strings)
    datasets(id, name, notes, created_at, source)
    dataset_versions(id, dataset_id, seq, created_at, document_json, signature)
        Every edit (import, connector, lock) appends a version of the *input*.
        Inputs are immutable once written, so any result points at the exact
        document it was computed from.
    jobs(id, dataset_id, input_version_id, action, payload_json, created_at,
         status, error, elapsed_ms, report_json, change_json)
    results(id, job_id, dataset_id, input_version_id, version, created_at,
            document_json (result), solve_json (options), summary_json)
        Result versions are append-only.  ``version`` increases per dataset.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .model import Dataset, DataError, dataset_from_json, dataset_to_json

DEFAULT_DB = os.environ.get("HAPLORECON_DB", "haplorecon.sqlite3")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS datasets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id    TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    document_json TEXT NOT NULL,
    signature     TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    UNIQUE(dataset_id, seq)
);

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    dataset_id       TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    input_version_id INTEGER REFERENCES dataset_versions(id),
    action           TEXT NOT NULL,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL,
    error            TEXT,
    elapsed_ms       INTEGER NOT NULL DEFAULT 0,
    report_json      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    dataset_id       TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    input_version_id INTEGER REFERENCES dataset_versions(id),
    version          INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    solve_json       TEXT NOT NULL,
    summary_json     TEXT NOT NULL,
    document_json    TEXT NOT NULL,
    UNIQUE(dataset_id, version)
);

CREATE INDEX IF NOT EXISTS idx_versions_dataset ON dataset_versions(dataset_id, seq);
CREATE INDEX IF NOT EXISTS idx_jobs_dataset ON jobs(dataset_id, created_at);
CREATE INDEX IF NOT EXISTS idx_results_dataset ON results(dataset_id, version);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{secrets.token_hex(8)}"


@dataclass
class StoredVersion:
    id: int
    dataset_id: str
    seq: int
    created_at: str
    signature: str
    label: str
    dataset: Dataset


@dataclass
class StoredResult:
    id: int
    version: int
    job_id: str
    dataset_id: str
    input_version_id: Optional[int]
    created_at: str
    solve: Dict[str, object]
    summary: Dict[str, object]
    document: Dict[str, object]


class Store:
    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- datasets ---------------------------------------------------------

    def create_dataset(self, ds: Dataset, source: str = "import") -> str:
        with self._lock:
            did = _new_id("ds")
            self._conn.execute(
                "INSERT INTO datasets(id, name, notes, source, created_at) VALUES (?,?,?,?,?)",
                (did, ds.name, ds.notes, source, utcnow()),
            )
            self._conn.commit()
        return did

    def dataset_exists(self, dataset_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM datasets WHERE id=?", (dataset_id,)
            ).fetchone()
        return row is not None

    def dataset_row(self, dataset_id: str) -> Dict[str, object]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if row is None:
            raise KeyError(f"dataset {dataset_id!r} not found")
        return dict(row)

    def list_datasets(self) -> List[Dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT d.*,
                          (SELECT COUNT(*) FROM dataset_versions v WHERE v.dataset_id=d.id) AS n_versions,
                          (SELECT COUNT(*) FROM results r WHERE r.dataset_id=d.id) AS n_results
                     FROM datasets d ORDER BY d.created_at DESC, d.id"""
            ).fetchall()
        return [dict(r) for r in rows]

    # -- input versions ---------------------------------------------------

    def add_version(self, dataset_id: str, ds: Dataset, label: str = "") -> StoredVersion:
        from .solver.solve import dataset_signature

        doc = dataset_to_json(ds)
        sig = dataset_signature(ds)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM dataset_versions WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            cur = self._conn.execute(
                """INSERT INTO dataset_versions(dataset_id, seq, created_at, document_json, signature, label)
                   VALUES (?,?,?,?,?,?)""",
                (dataset_id, seq, utcnow(), json.dumps(doc), sig, label),
            )
            self._conn.commit()
            vid = int(cur.lastrowid)
        return StoredVersion(vid, dataset_id, seq, utcnow(), sig, label, ds)

    def get_version(self, dataset_id: str, seq: int) -> StoredVersion:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM dataset_versions WHERE dataset_id=? AND seq=?",
                (dataset_id, seq),
            ).fetchone()
        if row is None:
            raise KeyError(f"dataset {dataset_id!r} has no version {seq}")
        ds = dataset_from_json(json.loads(row["document_json"]))
        return StoredVersion(
            int(row["id"]), dataset_id, int(row["seq"]), row["created_at"],
            row["signature"], row["label"], ds,
        )

    def latest_version(self, dataset_id: str) -> StoredVersion:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) AS m FROM dataset_versions WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
        if row is None or row["m"] is None:
            raise KeyError(f"dataset {dataset_id!r} has no versions")
        return self.get_version(dataset_id, int(row["m"]))

    def list_versions(self, dataset_id: str) -> List[Dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT v.seq, v.created_at, v.signature, v.label,
                          (SELECT COUNT(*) FROM results r WHERE r.input_version_id=v.id) AS n_results
                     FROM dataset_versions v WHERE v.dataset_id=? ORDER BY v.seq""",
                (dataset_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- jobs -------------------------------------------------------------

    def start_job(self, dataset_id: str, input_version_id: Optional[int],
                  action: str, payload: Dict[str, object]) -> str:
        jid = _new_id("job")
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id, dataset_id, input_version_id, action, payload_json,
                                     created_at, status)
                   VALUES (?,?,?,?,?,?,'running')""",
                (jid, dataset_id, input_version_id, action, json.dumps(payload), utcnow()),
            )
            self._conn.commit()
        return jid

    def finish_job(self, job_id: str, status: str, elapsed_ms: int,
                   report: Dict[str, object], error: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, elapsed_ms=?, report_json=?, error=? WHERE id=?",
                (status, int(elapsed_ms), json.dumps(report), error, job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Dict[str, object]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"job {job_id!r} not found")
        out = dict(row)
        out["payload"] = json.loads(out.pop("payload_json") or "{}")
        out["report"] = json.loads(out.pop("report_json") or "{}")
        return out

    def list_jobs(self, dataset_id: str, limit: int = 50) -> List[Dict[str, object]]:
        # Jobs created in the same second share a timestamp, so the rowid
        # (insertion order) breaks the tie and keeps "newest first" true.
        with self._lock:
            rows = self._conn.execute(
                """SELECT j.id, j.action, j.created_at, j.status, j.error, j.elapsed_ms,
                          j.input_version_id,
                          (SELECT v.seq FROM dataset_versions v WHERE v.id=j.input_version_id) AS input_seq,
                          (SELECT MAX(r.version) FROM results r WHERE r.job_id=j.id) AS result_version
                     FROM jobs j WHERE j.dataset_id=? ORDER BY j.rowid DESC LIMIT ?""",
                (dataset_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- results ----------------------------------------------------------

    def add_result(self, job_id: str, dataset_id: str, input_version_id: Optional[int],
                   solve: Dict[str, object], summary: Dict[str, object],
                   document: Dict[str, object]) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS m FROM results WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
            version = int(row["m"]) + 1
            cur = self._conn.execute(
                """INSERT INTO results(job_id, dataset_id, input_version_id, version,
                                       created_at, solve_json, summary_json, document_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    job_id, dataset_id, input_version_id, version, utcnow(),
                    json.dumps(solve), json.dumps(summary), json.dumps(document),
                ),
            )
            self._conn.commit()
            return version

    def get_result(self, dataset_id: str, version: Optional[int] = None) -> StoredResult:
        with self._lock:
            if version is None:
                row = self._conn.execute(
                    "SELECT * FROM results WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
                    (dataset_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM results WHERE dataset_id=? AND version=?",
                    (dataset_id, int(version)),
                ).fetchone()
        if row is None:
            raise KeyError(
                f"dataset {dataset_id!r} has no result"
                + (f" version {version}" if version is not None else "")
            )
        return StoredResult(
            id=int(row["id"]),
            version=int(row["version"]),
            job_id=row["job_id"],
            dataset_id=row["dataset_id"],
            input_version_id=row["input_version_id"],
            created_at=row["created_at"],
            solve=json.loads(row["solve_json"]),
            summary=json.loads(row["summary_json"]),
            document=json.loads(row["document_json"]),
        )

    def list_results(self, dataset_id: str) -> List[Dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT r.version, r.created_at, r.job_id, r.summary_json, r.input_version_id,
                          v.seq AS input_seq, j.action
                     FROM results r
                     LEFT JOIN dataset_versions v ON v.id = r.input_version_id
                     LEFT JOIN jobs j ON j.id = r.job_id
                     WHERE r.dataset_id=? ORDER BY r.version DESC""",
                (dataset_id,),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            out.append(item)
        return out
