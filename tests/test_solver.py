"""Solver correctness against an independent brute-force reference.

The reference (``helpers.reference_solve``) shares no code with the solver: it
enumerates orientation vectors and allele pairs directly from the observations.
For every instance we therefore know the true optimum *and* the full set of
optimal reconstructions, so the solver's cost, its reconstruction count and the
solutions it lists are all checked, not just the objective value.
"""
from __future__ import annotations

import itertools
import random
import time
import unittest

from helpers import canonicalise, load_example, random_dataset, reference_solve

from haplorecon.model import Dataset, Fragment, Lock, parse_text
from haplorecon.solver.base import build_components
from haplorecon.solver.component import _eval_bits, solve_component
from haplorecon.solver.dp import solve_dp
from haplorecon.solver.solve import SolveOptions, solve_dataset


def solve(ds: Dataset) -> dict:
    return solve_dataset(ds, SolveOptions(time_limit_s=20.0).normalised())


class DPExactnessTests(unittest.TestCase):
    """The sweep DP's optimum and its enumerated optimal set must be exact."""

    def _check(self, ds: Dataset) -> None:
        for comp in build_components(ds):
            n = len(comp.fragments)
            self.assertLessEqual(n, 16, "component too large for the reference")
            best, vectors = None, []
            for bits in itertools.product((0, 1), repeat=n):
                cost, _, _ = _eval_bits(comp, bits)
                if best is None or cost < best:
                    best, vectors = cost, [bits]
                elif cost == best:
                    vectors.append(bits)
            out = solve_dp(comp, time.perf_counter() + 10)
            self.assertIsNotNone(out)
            self.assertEqual(out.cost, best)
            self.assertEqual(out.n_opt_vectors, len(vectors))
            self.assertEqual(set(out.opt_bits), set(vectors))
            self.assertFalse(out.variants_hit_cap)

    def test_dp_matches_enumeration_on_examples(self):
        for name in ("two_blocks", "single_error", "lock_conflict", "merge_demo"):
            with self.subTest(sample=name):
                self._check(load_example(name))

    def test_dp_matches_enumeration_randomly(self):
        rng = random.Random(7)
        for trial in range(120):
            ds = random_dataset(rng, max_positions=12, max_fragments=9)
            with self.subTest(trial=trial):
                self._check(ds)


class DatasetOptimumTests(unittest.TestCase):
    """Whole-dataset cost, reconstruction count, and every listed solution."""

    def _check(self, ds: Dataset) -> None:
        best, expected, swap_symmetric = reference_solve(ds)
        result = solve(ds)
        self.assertEqual(result["status"], "optimal")
        self.assertTrue(result["proven"])
        self.assertEqual(result["cost"], best)

        eq = result["equivalence"]
        self.assertTrue(eq["classes_known"])
        self.assertEqual(eq["n_solutions_modulo_labels"], len(expected))

        seen = set()
        for sol in result["solutions"]:
            canon = canonicalise(sol["hap0"], sol["hap1"], swap_symmetric)
            self.assertIn(canon, expected, "listed a non-optimal reconstruction")
            self.assertNotIn(canon, seen, "listed the same reconstruction twice")
            seen.add(canon)
            self.assertEqual(sol["cost"], best)
        if not eq["representatives_capped"]:
            self.assertEqual(seen, expected, "listed set is not the whole optimum")

    def test_examples(self):
        for name in ("two_blocks", "single_error", "lock_conflict", "merge_demo"):
            with self.subTest(sample=name):
                self._check(load_example(name))

    def test_random_instances(self):
        rng = random.Random(5000)
        for trial in range(80):
            with self.subTest(trial=trial):
                self._check(random_dataset(rng, lock_probability=0.6))


class CostSharingTests(unittest.TestCase):
    """A fragment's whole observation vector picks its side; no per-column vote."""

    def test_multi_site_evidence_decides_side(self):
        # e is 1110: its first three calls look like a; its last call looks like
        # b.  A per-column vote would therefore put e on both sides at once.
        # Scored as one unit, e must sit entirely on one side and pay exactly one
        # error, which is the optimum.
        ds = parse_text(">a start=0\n1111\n>b start=0\n0000\n>e start=0\n1110\n")
        result = solve(ds)
        self.assertEqual(result["cost"], 1)
        sol = result["solutions"][0]
        # e is on one side only, and the whole fragment (not just its bad call)
        # is what the orientation decides.
        self.assertIn(sol["orientation"]["e"], (0, 1))
        self.assertEqual(sol["orientation"]["a"], 1 - 0)
        n_errors = sum(len(v) for v in sol["errors"].values())
        self.assertEqual(n_errors, 1)

    def test_column_vote_would_cost_more(self):
        # Column-majority gives 1110 / 0001; scored against whole fragments it
        # costs 2.  The joint optimum is 1, so the solver must beat the vote.
        ds = parse_text(">a start=0\n1111\n>b start=0\n0000\n>e start=0\n1110\n")
        frags = ds.all_fragments()
        votes = []
        for p in ds.positions:
            calls = [f.alleles[p] for f in frags]
            votes.append(1 if calls.count(1) > calls.count(0) else 0)
        vote0 = "".join(map(str, votes))
        vote1 = "".join("1" if c == "0" else "0" for c in vote0)
        vote_cost = 0
        for f in frags:
            obs = "".join(str(f.alleles[p]) for p in ds.positions)
            vote_cost += min(
                sum(x != y for x, y in zip(obs, vote0)),
                sum(x != y for x, y in zip(obs, vote1)),
            )
        self.assertEqual(vote_cost, 2)
        self.assertLess(solve(ds)["cost"], vote_cost)

    def test_one_corrupted_call_is_charged_once(self):
        ds = parse_text(
            ">a start=0\n0011\n>b start=0\n0011\n"
            ">c start=0\n1100\n>d start=0\n1100\n>e start=0\n1011\n"
        )
        result = solve(ds)
        self.assertEqual(result["cost"], 1)
        sol = result["solutions"][0]
        self.assertEqual(sorted([sol["hap0"], sol["hap1"]]), ["0011", "1100"])
        self.assertEqual(sol["errors"], {"e": [0]})


class ComponentTests(unittest.TestCase):
    def test_disconnected_blocks_are_independent(self):
        ds = load_example("two_blocks")
        comps = build_components(ds)
        self.assertEqual(len(comps), 2)
        self.assertLess(max(comps[0].positions), min(comps[1].positions))
        result = solve(ds)
        self.assertEqual(result["n_components"], 2)
        self.assertEqual(result["cost"], sum(c["cost"] for c in result["components"]))

    def test_connector_merges_blocks(self):
        ds = load_example("two_blocks")
        self.assertEqual(len(build_components(ds)), 2)
        ds.connectors.append(Fragment(fid="bridge", alleles={4: 0, 5: 1, 6: 0, 7: 1}))
        ds.recompute_positions()
        self.assertEqual(len(build_components(ds)), 1)

    def test_no_stitching_across_a_gap(self):
        result = solve(load_example("two_blocks"))
        gaps = [u for u in result["undetermined"] if u["kind"] == "gap"]
        self.assertTrue(gaps, "the uncovered gap must be reported as undetermined")
        # The two blocks must remain separate blocks in the output.
        self.assertEqual(len(result["coverage_blocks"]), 2)


class LockConstraintTests(unittest.TestCase):
    def test_full_lock_forces_the_pair(self):
        ds = parse_text(">a start=0\n01\n>b start=0\n01\n")
        ds.set_lock(Lock(position=0, hap0=1, hap1=0))
        result = solve(ds)
        sol = result["solutions"][0]
        self.assertEqual((sol["hap0"][0], sol["hap1"][0]), ("1", "0"))

    def test_order_pin_removes_the_global_swap(self):
        ds = load_example("two_blocks")
        before = solve(ds)["equivalence"]
        self.assertTrue(before["global_flip_free"])
        self.assertEqual(before["label_quotient"], 2)
        ds.set_lock(Lock(position=0, hap0=0))
        after = solve(ds)["equivalence"]
        self.assertTrue(after["labels_fixed_by_lock"])
        self.assertFalse(after["global_flip_free"])
        self.assertEqual(after["label_quotient"], 1)
        self.assertEqual(
            after["n_solutions_modulo_labels"], after["raw_classes_product"]
        )

    def test_distinct_lock_rules_out_equal_alleles(self):
        # Distinct at p0 forbids (0,0) and (1,1); pinning p1 to (0,1) forces the
        # two observed fragments apart, which costs one error whichever way the
        # rest is assigned.
        ds = parse_text(">x start=0\n00\n>y start=0\n01\n")
        ds.set_lock(Lock(position=0, distinct=True))
        ds.set_lock(Lock(position=1, hap0=0, hap1=1))
        result = solve(ds)
        sol = result["solutions"][0]
        self.assertNotEqual(sol["hap0"][0], sol["hap1"][0])
        self.assertEqual(result["cost"], 1)

    def test_contradictory_locks_report_conflict_fragments(self):
        ds = load_example("lock_conflict")
        ds.set_lock(Lock(position=0, hap0=1, hap1=0))
        ds.set_lock(Lock(position=6, hap0=1, hap1=0))
        result = solve(ds)
        conflicts = result["conflicts"]
        self.assertGreater(conflicts["n_unavoidable"], 0)
        self.assertIn("x1", conflicts["conflict_fragments"])

    def test_satisfiable_locks_have_no_conflict(self):
        ds = load_example("two_blocks")
        ds.set_lock(Lock(position=0, hap0=0, hap1=1))
        result = solve(ds)
        self.assertEqual(result["conflicts"]["n_unavoidable"], 0)


class FlipAmbiguityTests(unittest.TestCase):
    def test_each_block_keeps_its_own_flip(self):
        result = solve(load_example("two_blocks"))
        flips = [u for u in result["undetermined"] if u["kind"] == "flip"]
        self.assertEqual(len(flips), 2)

    def test_flip_classes_are_reported_once_per_block(self):
        ds = load_example("two_blocks")
        eq = solve(ds)["equivalence"]
        # Each block has 4 ordered optimal reconstructions (two label assignments
        # times two optimal allele pairs at the gap-adjacent position), so the
        # ordered product is 16 and the global label swap pairs them into 8.
        per = [p["classes"] for p in eq["per_component"]]
        self.assertEqual(per, [4, 4])
        self.assertEqual(eq["raw_classes_product"], 16)
        self.assertEqual(eq["palindromic_reconstructions"], 0)
        self.assertEqual(eq["n_solutions_modulo_labels"], 8)
        self.assertEqual(eq["representatives_listed"], 8)
        self.assertFalse(eq["representatives_capped"])

    def test_palindromic_reconstruction_is_its_own_swap(self):
        ds = parse_text(">a start=0\n00\n>b start=0\n00\n")
        eq = solve(ds)["equivalence"]
        self.assertTrue(eq["classes_known"])
        # (00, 00) is fixed by the swap; (00, 01), (00, 10), (00, 11) come in
        # swapped pairs, so Burnside gives (7 + 1) // 2 = 4.
        self.assertEqual(eq["palindromic_reconstructions"], 1)
        self.assertEqual(eq["raw_classes_product"], 7)
        self.assertEqual(eq["n_solutions_modulo_labels"], 4)
        self.assertEqual(
            eq["n_solutions_modulo_labels"],
            (eq["raw_classes_product"] + eq["palindromic_reconstructions"]) // 2,
        )


class BoundsAndTimeoutTests(unittest.TestCase):
    def test_large_instance_is_solved_exactly_or_reports_bounds(self):
        ds = load_example("long_scaffold")
        result = solve_dataset(ds, SolveOptions(time_limit_s=10.0).normalised())
        self.assertIn(result["status"], ("optimal", "timeout"))
        self.assertLessEqual(result["lower_bound"], result["cost"])
        self.assertLessEqual(result["cost"], result["upper_bound"])
        if result["status"] == "optimal":
            self.assertEqual(result["lower_bound"], result["cost"])

    def test_tiny_budget_reports_timeout_with_bounds(self):
        # ``max_nodes=1`` exhausts the budget on the first node, so the search
        # cannot prove anything and must say so rather than report a fake optimum.
        ds = load_example("long_scaffold")
        result = solve_dataset(
            ds, SolveOptions(time_limit_s=1.0, engine="bandb", max_nodes=1).normalised()
        )
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["proven"])
        self.assertIsNotNone(result["lower_bound"])
        self.assertIsNotNone(result["upper_bound"])
        self.assertLessEqual(result["lower_bound"], result["upper_bound"])
        self.assertGreaterEqual(result["upper_bound"], result["cost"])
        self.assertTrue(result["timed_out_components"])
        self.assertTrue(any("not proven" in w for w in result["warnings"]))

    def test_negative_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            SolveOptions(time_limit_s=0.0).normalised()
        with self.assertRaises(ValueError):
            SolveOptions(engine="nope").normalised()

    def test_component_solution_statuses_are_consistent(self):
        ds = load_example("long_scaffold")
        result = solve_dataset(ds, SolveOptions(time_limit_s=10.0).normalised())
        for comp in result["components"]:
            self.assertEqual(comp["status"] == "optimal", comp["proven"])
            if comp["proven"]:
                self.assertEqual(comp["lower_bound"], comp["cost"])


class EngineAgreementTests(unittest.TestCase):
    def test_enumeration_and_dp_agree(self):
        ds = parse_text(">a start=0\n010\n>b start=1\n101\n>c start=2\n010\n")
        comp = build_components(ds)[0]
        deadline = time.perf_counter() + 20
        via_dp = solve_component(comp, deadline, engine="dp")
        via_enum = solve_component(comp, deadline, engine="enum")
        self.assertEqual(via_dp.cost, via_enum.cost)
        self.assertEqual(via_dp.n_orientation_classes, via_enum.n_orientation_classes)


if __name__ == "__main__":
    unittest.main()
