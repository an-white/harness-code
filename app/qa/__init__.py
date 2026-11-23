"""Deterministic QA verification primitives."""

from .checks import (
    capture_task_baseline,
    check_python_syntax,
    detect_scope_violations,
    discover_git_repos,
    run_verification_commands,
)

__all__ = [
    "capture_task_baseline",
    "check_python_syntax",
    "detect_scope_violations",
    "discover_git_repos",
    "run_verification_commands",
]
