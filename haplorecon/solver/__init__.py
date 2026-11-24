"""Solver package: exact component solvers plus incremental job orchestration."""
from .base import Component, build_components, describe_components, lock_error_lower_bound
from .component import CompSolution, ENUM_MAX_N, solve_component
from .jobs import JobOutcome, run_job, suggest_connector_positions
from .solve import SolveOptions, solve_dataset

__all__ = [
    "Component",
    "CompSolution",
    "ENUM_MAX_N",
    "JobOutcome",
    "SolveOptions",
    "build_components",
    "describe_components",
    "lock_error_lower_bound",
    "run_job",
    "solve_component",
    "solve_dataset",
    "suggest_connector_positions",
]
