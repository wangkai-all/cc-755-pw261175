"""Model, parsing, locks and serialisation."""
from __future__ import annotations

import json
import unittest

from helpers import ROOT  # noqa: F401  (puts the project on sys.path)

from haplorecon.model import (
    DataError,
    Dataset,
    Fragment,
    Lock,
    dataset_from_json,
    dataset_to_json,
    dataset_to_text,
    parse_text,
)


class ParsingTests(unittest.TestCase):
    def test_fasta_style(self):
        ds = parse_text(">a start=3\n010\n>b start=4\n11\n")
        self.assertEqual(ds.positions, [3, 4, 5])
        self.assertEqual(ds.by_id()["a"].alleles, {3: 0, 4: 1, 5: 0})
        self.assertEqual(ds.by_id()["b"].alleles, {4: 1, 5: 1})

    def test_tsv_style(self):
        ds = parse_text("a\t2\t011\nb\t3\t10\n")
        self.assertEqual(ds.by_id()["a"].alleles, {2: 0, 3: 1, 4: 1})
        self.assertEqual(ds.by_id()["b"].alleles, {3: 1, 4: 0})

    def test_missing_calls(self):
        ds = parse_text(">a start=0\n0?1.-N\n")
        self.assertEqual(ds.by_id()["a"].alleles, {0: 0, 2: 1})

    def test_comments_and_blank_lines_ignored(self):
        ds = parse_text("# note\n\n>a start=0\n01\n\n# trailing\n")
        self.assertEqual(ds.positions, [0, 1])

    def test_duplicate_fragment_id_rejected(self):
        with self.assertRaises(DataError):
            parse_text(">a start=0\n01\n>a start=2\n01\n")

    def test_empty_input_rejected(self):
        with self.assertRaises(DataError):
            parse_text("# only comments\n")

    def test_garbage_rejected(self):
        with self.assertRaises(DataError):
            parse_text("this is not fragment text\n")

    def test_negative_start_rejected(self):
        with self.assertRaises(DataError):
            parse_text(">a start=-2\n01\n")


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.ds = parse_text(">a start=0\n010\n>b start=1\n11\n", name="round trip")
        self.ds.notes = "notes here"
        self.ds.locks[2] = Lock(position=2, hap0=1, hap1=0)
        self.ds.validate()

    def test_text_round_trip(self):
        again = parse_text(dataset_to_text(self.ds), name=self.ds.name)
        self.assertEqual(again.positions, self.ds.positions)
        self.assertEqual(
            {f.fid: f.alleles for f in again.all_fragments()},
            {f.fid: f.alleles for f in self.ds.all_fragments()},
        )

    def test_json_round_trip(self):
        blob = json.loads(json.dumps(dataset_to_json(self.ds)))
        again = dataset_from_json(blob)
        self.assertEqual(again.positions, self.ds.positions)
        self.assertEqual(again.locks[2].hap0, 1)
        self.assertEqual(again.locks[2].hap1, 0)
        self.assertEqual(again.name, self.ds.name)
        self.assertEqual(again.notes, self.ds.notes)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.ds = parse_text(">a start=0\n010\n>b start=1\n110\n")

    def test_set_and_clear(self):
        self.ds.set_lock(Lock(position=1, hap0=1))
        self.assertIn(1, self.ds.locks)
        self.ds.clear_lock(1)
        self.assertNotIn(1, self.ds.locks)

    def test_empty_lock_is_removed(self):
        self.ds.set_lock(Lock(position=1, hap0=1))
        self.ds.set_lock(Lock(position=1))
        self.assertNotIn(1, self.ds.locks)

    def test_contradictory_lock_rejected(self):
        with self.assertRaises(DataError):
            self.ds.set_lock(Lock(position=1, hap0=0, hap1=0, distinct=True))

    def test_out_of_range_allele_rejected(self):
        with self.assertRaises(DataError):
            Lock(position=1, hap0=2).validate()

    def test_order_pinning_detection(self):
        self.assertFalse(Lock(position=1, distinct=True).pins_order)
        self.assertTrue(Lock(position=1, hap0=0).pins_order)
        self.assertTrue(Lock(position=1, hap1=1).pins_order)

    def test_lock_on_unobserved_position_rejected(self):
        # ``set_lock`` records the lock; ``validate`` is what rejects a lock at a
        # coordinate no fragment observes.
        self.ds.set_lock(Lock(position=99, hap0=0))
        with self.assertRaises(DataError):
            self.ds.validate()

    def test_covered_positions_do_not_shrink_when_locking(self):
        self.ds.set_lock(Lock(position=1, hap0=1))
        self.assertEqual(self.ds.coverage()[1], 2)


class CoverageTests(unittest.TestCase):
    def test_coverage_counts(self):
        ds = parse_text(">a start=0\n010\n>b start=1\n11\n")
        self.assertEqual(ds.coverage(), {0: 1, 1: 2, 2: 2})

    def test_connectors_are_fragments_too(self):
        ds = parse_text(">a start=0\n01\n")
        ds.connectors.append(Fragment(fid="c", alleles={1: 1, 2: 0}))
        ds.recompute_positions()
        self.assertEqual(len(ds.all_fragments()), 2)
        self.assertEqual(ds.positions, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
