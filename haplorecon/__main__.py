"""Command line entry point.

    python3 -m haplorecon serve [--port 8765] [--db FILE]
    python3 -m haplorecon solve FILE [--engine auto] [--time-limit 20] [--json]
    python3 -m haplorecon samples
    python3 -m haplorecon export FILE --out DIR

Everything operates on synthetic binary fragments.  No real genetic data is
read, generated, or represented.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import __version__
from .model import DataError, dataset_summary, parse_text
from .samples import SAMPLES
from .solver.solve import SolveOptions, solve_dataset

DEFAULT_DB = os.environ.get("HAPLORECON_DB", os.path.join(os.getcwd(), "haplorecon.sqlite3"))


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def cmd_solve(args: argparse.Namespace) -> int:
    try:
        text = _read_text(args.file)
    except OSError as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    try:
        ds = parse_text(text, name=os.path.basename(args.file))
    except DataError as exc:
        print(f"invalid input: {exc}", file=sys.stderr)
        return 2
    options = SolveOptions(
        time_limit_s=args.time_limit, engine=args.engine, max_nodes=args.max_nodes
    ).normalised()
    result = solve_dataset(ds, options)
    if args.json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    summary = dataset_summary(ds)
    print(f"dataset      {summary['name']}")
    print(f"fragments    {summary['n_fragments']}  positions {summary['n_positions']}")
    print(f"status       {result['status']}  (proven: {result['proven']})")
    print(f"cost         {result['cost']}   bounds [{result['lower_bound']}, {result['upper_bound']}]")
    print(f"components   {result['n_components']}  reused {result['reused_components']}")
    print()
    for comp in result["components"]:
        print(
            f"block {comp['index']}: {comp['start']}-{comp['end']}  "
            f"frags {comp['n_fragments']}  cost {comp['cost']}  "
            f"{comp['status']}  engine {comp['engine']}"
        )
        print(f"  hap0 {comp['hap0']}")
        print(f"  hap1 {comp['hap1']}")
        print(f"  flip-free: {comp['flip_free']}  errors: {comp['errors'] or 'none'}")
    if result["undetermined"]:
        print("\nundetermined:")
        for u in result["undetermined"]:
            rng = f"{u['start']}-{u['end']}" if u.get("start") is not None else "-"
            print(f"  [{u['kind']}] {rng}: {u['note']}")
    if result["conflicts"]["conflict_fragments"]:
        print("\nconflicts:")
        for u in result["conflicts"]["unavoidable"]:
            print(f"  unavoidable {u['fragment']} at {u['conflicting_positions']}")
        for row in result["conflicts"]["locks"]:
            if row["status"] == "contradicted":
                print(f"  lock p{row['position']} contradicted by {row['conflict_fragments']}")
    print(f"\nelapsed {result['gap_ms']} ms")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.db, host=args.host, port=args.port)
    return 0


def cmd_samples(args: argparse.Namespace) -> int:
    for s in SAMPLES:
        print(f"{s.name:16s} {s.title}")
        print(f"{'':16s} {s.description}")
        print(f"{'':16s} expected: {s.expectation}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    os.makedirs(args.out, exist_ok=True)
    for s in SAMPLES:
        path = os.path.join(args.out, f"{s.name}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(s.text)
        print(path)
    if not SAMPLES:
        print("no bundled samples available", file=sys.stderr)
        return 1
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Solve every bundled sample and print a compact table."""
    for s in SAMPLES:
        ds = parse_text(s.text, name=s.name)
        r = solve_dataset(ds, SolveOptions(time_limit_s=args.time_limit))
        print(
            f"{s.name:16s} comps={r['n_components']} status={r['status']:8s} "
            f"cost={r['cost']:3d} lb={r['lower_bound']:3d} "
            f"classes={r['equivalence']['n_solutions_modulo_labels']} "
            f"undetermined={len(r['undetermined'])}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="haplorecon",
        description="Reassemble two latent binary sequences from sparse synthetic fragments.",
    )
    parser.add_argument("--version", action="version", version=f"haplorecon {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the web application")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--db", default=DEFAULT_DB)
    p_serve.set_defaults(func=cmd_serve)

    p_solve = sub.add_parser("solve", help="solve one fragment file")
    p_solve.add_argument("file")
    p_solve.add_argument("--engine", choices=["auto", "enum", "bandb"], default="auto")
    p_solve.add_argument("--time-limit", type=float, default=20.0)
    p_solve.add_argument("--max-nodes", type=int, default=300_000)
    p_solve.add_argument("--json", action="store_true", help="emit the full result document")
    p_solve.set_defaults(func=cmd_solve)

    p_samples = sub.add_parser("samples", help="describe bundled synthetic samples")
    p_samples.set_defaults(func=cmd_samples)

    p_export = sub.add_parser("export", help="write bundled samples to a directory")
    p_export.add_argument("--out", default="examples_export")
    p_export.set_defaults(func=cmd_export)

    p_demo = sub.add_parser("demo", help="solve every bundled sample and summarise")
    p_demo.add_argument("--time-limit", type=float, default=10.0)
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
