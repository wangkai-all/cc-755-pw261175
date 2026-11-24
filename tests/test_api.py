"""HTTP API end-to-end: spawn the real server, talk to it with urllib.

These tests exercise the wire format the UI consumes -- import, connector,
lock, unlock, compare, hints, versions, exports, and the error paths.  Nothing
is mocked: the server is a real wsgiref process on an ephemeral port over a
temporary SQLite file.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import ROOT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as res:
                if res.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - retry until the deadline
            last = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not start: {last}")


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_fd, cls.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(cls.db_fd)
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "haplorecon", "serve", "--port", str(cls.port),
             "--db", cls.db_path],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        try:
            _wait_for(cls.base + "/api/samples")
        except RuntimeError:
            cls.proc.kill()
            err = cls.proc.stderr.read() if cls.proc.stderr else ""
            raise RuntimeError(f"server failed to start:\n{err}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=5)
        if cls.proc.stderr:
            cls.proc.stderr.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cls.db_path + suffix)
            except OSError:
                pass

    # -- helpers ----------------------------------------------------------

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body: dict):
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                raw = res.read()
                return res.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)

    def import_sample(self, name: str) -> dict:
        status, sample = self.get(f"/api/samples/{name}")
        self.assertEqual(status, 200)
        status, payload = self.post("/api/import", {"text": sample["text"], "name": name})
        self.assertEqual(status, 201, payload)
        return payload

    def raw(self, path: str):
        req = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.headers, res.read()


class ImportTests(ServerCase):
    def test_import_returns_input_and_result_versions(self):
        payload = self.import_sample("two_blocks")
        self.assertIn("dataset_id", payload)
        self.assertEqual(payload["result_version"], 1)
        self.assertEqual(payload["job"]["action"], "import")
        result = payload["result"]
        self.assertEqual(result["status"], "optimal")
        self.assertTrue(result["proven"])
        self.assertEqual(result["n_components"], 2)
        self.assertEqual(result["equivalence"]["n_solutions_modulo_labels"], 8)
        self.assertEqual(len(payload["dataset"]["fragments"]), 9)

    def test_index_and_static_assets_are_served(self):
        status, headers, body = self.raw("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Fragment reassembly", body)
        status, headers, body = self.raw("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"__app", body)

    def test_samples_are_listed_with_provenance_text(self):
        status, data = self.get("/api/samples")
        self.assertEqual(status, 200)
        names = [s["name"] for s in data]
        self.assertIn("two_blocks", names)
        for s in data:
            self.assertTrue(s["description"])
            self.assertTrue(s["expectation"])

    def test_dataset_list_reports_latest_result(self):
        payload = self.import_sample("single_error")
        status, data = self.get("/api/datasets")
        self.assertEqual(status, 200)
        row = next(d for d in data["datasets"] if d["id"] == payload["dataset_id"])
        self.assertEqual(row["latest_result"]["version"], 1)
        self.assertEqual(row["latest_result"]["cost"], 1)

    def test_bad_input_is_rejected_without_creating_a_dataset(self):
        status, body = self.post("/api/import", {"text": "not fragment text"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "parse_error")
        status, body = self.post("/api/import", {"text": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "empty_input")

    def test_bad_options_are_rejected(self):
        status, body = self.post(
            "/api/import",
            {"text": ">a start=0\n01\n", "options": {"engine": "magic"}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_options")
        status, body = self.post(
            "/api/import",
            {"text": ">a start=0\n01\n", "options": {"time_limit_s": 0}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_options")


class EditFlowTests(ServerCase):
    def test_connector_merges_blocks_and_appends_versions(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, payload2 = self.post(
            f"/api/datasets/{did}/connectors",
            {"fid": "bridge", "alleles": {"4": 0, "5": 1, "6": 0, "7": 1}},
        )
        self.assertEqual(status, 201, payload2)
        self.assertEqual(payload2["result_version"], 2)
        self.assertEqual(payload2["result"]["n_components"], 1)
        self.assertIn("bridges blocks", payload2["job"]["report"]["summary"])
        status, view = self.get(f"/api/datasets/{did}")
        self.assertEqual(len(view["versions"]), 2)
        status, results = self.get(f"/api/datasets/{did}/results")
        self.assertEqual([r["version"] for r in results["results"]], [2, 1])

    def test_lock_and_unlock_round_trip(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        before = payload["result"]["equivalence"]
        self.assertTrue(before["global_flip_free"])
        status, locked = self.post(
            f"/api/datasets/{did}/locks",
            {"position": 0, "hap0": 0, "hap1": 1, "distinct": True},
        )
        self.assertEqual(status, 201, locked)
        eq = locked["result"]["equivalence"]
        self.assertFalse(eq["global_flip_free"])
        self.assertTrue(eq["labels_fixed_by_lock"])
        self.assertEqual([l["position"] for l in locked["dataset"]["locks"]], [0])
        status, unlocked = self.post(f"/api/datasets/{did}/unlock", {"position": 0})
        self.assertEqual(status, 201, unlocked)
        self.assertEqual(unlocked["dataset"]["locks"], [])
        self.assertTrue(unlocked["result"]["equivalence"]["global_flip_free"])

    def test_connector_that_contradicts_a_lock_reports_conflict_fragments(self):
        payload = self.import_sample("lock_conflict")
        did = payload["dataset_id"]
        self.assertEqual(payload["result"]["conflicts"]["n_unavoidable"], 0)
        status, out = self.post(
            f"/api/datasets/{did}/locks", {"position": 0, "hap0": 1, "hap1": 0}
        )
        self.assertEqual(status, 201)
        status, out = self.post(
            f"/api/datasets/{did}/locks", {"position": 6, "hap0": 1, "hap1": 0}
        )
        self.assertEqual(status, 201)
        conflicts = out["result"]["conflicts"]
        self.assertGreater(conflicts["n_unavoidable"], 0)
        self.assertIn("x1", conflicts["conflict_fragments"])

    def test_duplicate_connector_id_is_rejected(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, body = self.post(
            f"/api/datasets/{did}/connectors", {"fid": "b0", "alleles": {"4": 0}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "duplicate_id")

    def test_lock_on_unobserved_position_is_rejected(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, body = self.post(f"/api/datasets/{did}/locks", {"position": 999, "hap0": 0})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_lock")

    def test_unlock_of_unlocked_position_is_rejected(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, body = self.post(
            f"/api/datasets/{did}/unlock", {"position": 0}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "no_such_lock")

    def test_resolve_appends_a_result_with_new_options(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, out = self.post(
            f"/api/datasets/{did}/resolve", {"options": {"engine": "enum", "time_limit_s": 30}}
        )
        self.assertEqual(status, 201)
        self.assertEqual(out["result_version"], 2)
        self.assertEqual(out["job"]["action"], "resolve")
        self.assertEqual(out["result"]["options"]["engine"], "enum")
        self.assertEqual(out["result"]["cost"], payload["result"]["cost"])


class ReadOnlyApiTests(ServerCase):
    def test_compare_solutions_explains_a_flip(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        # Solutions 0 and 2 differ only by swapping the labels in the second
        # block; the comparator names that explicitly.
        status, out = self.get(
            f"/api/datasets/{did}/compare?result=1&a=0&b=2"
        )
        self.assertEqual(status, 200)
        self.assertGreater(out["n_differing_positions"], 0)
        self.assertTrue(out["flip_only_difference"])

    def test_compare_solutions_rejects_unknown_ids(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, body = self.get(f"/api/datasets/{did}/compare?result=1&a=0&b=999")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_solution")
        status, body = self.get(f"/api/datasets/{did}/compare?result=1&a=0")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "bad_argument")

    def test_compare_versions_describes_the_merge(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        self.post(f"/api/datasets/{did}/connectors",
                  {"fid": "bridge", "alleles": {"4": 0, "5": 1, "6": 0, "7": 1}})
        status, out = self.get(f"/api/datasets/{did}/versions/compare?a=1&b=2")
        self.assertEqual(status, 200)
        self.assertEqual(out["a"]["n_components"], 2)
        self.assertEqual(out["b"]["n_components"], 1)
        self.assertTrue(out["n_differing_positions"])

    def test_hints_list_gap_and_flip_suggestions(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, out = self.get(f"/api/datasets/{did}/hints?result=1")
        self.assertEqual(status, 200)
        kinds = {h["kind"] for h in out["hints"]}
        self.assertIn("gap", kinds)
        self.assertIn("flip_resolution", kinds)

    def test_job_log_records_every_action(self):
        payload = self.import_sample("merge_demo")
        did = payload["dataset_id"]
        self.post(f"/api/datasets/{did}/connectors",
                  {"fid": "link", "alleles": {"4": 0, "5": 1}})
        status, out = self.get(f"/api/datasets/{did}/jobs")
        self.assertEqual(status, 200)
        actions = [j["action"] for j in out["jobs"]]
        self.assertEqual(actions, ["add_connector", "import"])
        for j in out["jobs"]:
            self.assertEqual(j["status"], "ok")
            self.assertGreaterEqual(j["result_version"], 1)

    def test_exports_carry_download_headers(self):
        payload = self.import_sample("two_blocks")
        did = payload["dataset_id"]
        status, headers, body = self.raw(f"/api/datasets/{did}/export/input")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn(b">b0 start=0", body)
        status, headers, body = self.raw(f"/api/datasets/{did}/export/result?version=1")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["result_version"], 1)
        self.assertEqual(doc["result"]["cost"], 0)

    def test_unknown_dataset_and_route_return_404(self):
        status, body = self.get("/api/datasets/ds_nope")
        self.assertEqual(status, 404)
        status, body = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_wrong_method_is_405(self):
        status, body = self.get("/api/import")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "method_not_allowed")


if __name__ == "__main__":
    unittest.main()
