"""Headless UI integration: the real page and script against the real server.

``ui_runner.js`` loads ``index.html`` and ``app.js`` into a DOM shim (there is
no browser here), fires ``DOMContentLoaded`` and evaluates a probe that drives
the UI.  ``domshim.js`` throws on unsupported DOM usage, so a green run means
the script's actual DOM calls work, not a mock's.

The probe (``ui_flow.js``) imports a sample, inspects the coverage / sequence /
fragment views, compares two equivalent solutions, adds a connector, pins a
position, and re-opens the accumulated versions.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from helpers import ROOT

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


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


@unittest.skipIf(NODE is None, "node is not installed; the headless DOM runner needs it")
class UIFlowTests(unittest.TestCase):
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

    @classmethod
    def run_probe(cls) -> dict:
        proc = subprocess.run(
            [NODE, str(HERE / "ui_runner.js"), cls.base, str(HERE / "ui_flow.js")],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            raise AssertionError(
                f"ui_runner failed ({proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        payload = json.loads(proc.stdout)
        if not payload.get("ok"):
            raise AssertionError(f"probe failed: {payload.get('error')}\nstderr:\n{proc.stderr}")
        return payload["result"]

    def test_full_flow(self):
        out = self.run_probe()

        imported = out["imported"]
        self.assertEqual(imported["status"], "optimal")
        self.assertEqual(imported["cost"], 0)
        self.assertEqual(imported["blocks"], 2)
        self.assertEqual(imported["activeTab"], ["tab-reconstruct"])
        self.assertEqual(imported["sequenceBlocks"], 2)
        self.assertEqual(imported["fragmentRows"], 9)
        self.assertGreater(imported["canvasOps"], 100, "coverage map was not drawn")
        self.assertEqual(imported["solutionOptions"], 8)
        self.assertEqual(imported["hints"], 3)
        stats = dict(s.split("=", 1) for s in imported["stats"])
        self.assertEqual(stats["status"], "optimal")
        self.assertEqual(stats["blocks"], "2")
        self.assertEqual(stats["solutions"], "8")

        self.assertIn("differing position", out["compareBeforeLock"])

        merged = out["afterConnector"]
        self.assertEqual(merged["blocks"], 1)
        self.assertEqual(merged["action"], "add_connector")
        self.assertEqual(merged["version"], 2)
        self.assertIn("bridges blocks", merged["report"])
        self.assertEqual(merged["connectorRows"], 1)

        locked = out["afterLock"]
        self.assertEqual(locked["locks"], ["p3: hap0=1, hap1=0"])
        self.assertEqual(locked["lockRows"], 1)
        self.assertTrue(locked["labelsFixed"])
        self.assertEqual(locked["classesAfter"], locked["classesBefore"])

        errors = out["errorFixture"]
        self.assertEqual(errors["cost"], 1)
        self.assertEqual(errors["errorRows"], 1)
        self.assertEqual(errors["errorChips"], ["p8"])
        self.assertIn("gap", errors["undeterminedKinds"])

        versions = out["versions"]
        self.assertEqual(versions["inputs"], 3)
        self.assertEqual(versions["results"], 3)
        self.assertEqual(versions["optionValues"], ["1", "2", "3"])
        self.assertIn("differing position", out["versionCompare"])
        self.assertEqual(out["jobRows"], 3)
        links = out["exportLinks"]
        self.assertIn("/export/input", links["input"])
        self.assertIn("/export/result?version=", links["result"])


if __name__ == "__main__":
    unittest.main()
