"""Deterministic research pipeline for project onboarding and deep analysis."""

from .deep_analysis import (
    app_flow_path,
    deep_analysis_dir,
    deep_analysis_state_path,
    generate_partial_artifacts,
    read_deep_analysis_state,
    run_deep_analysis,
    user_summary_path,
    write_deep_analysis_state,
)
from .detection import detect_project_state
from .fingerprint import compute_fingerprint
from .onboarding import run_onboarding, should_run_onboarding
from .scanner import scan_workspace
from .writer import (
    context_digest_path,
    load_research_context,
    project_specs_path,
    refresh_research,
    research_state_path,
    write_project_research,
)

__all__ = [
    "app_flow_path",
    "compute_fingerprint",
    "context_digest_path",
    "deep_analysis_dir",
    "deep_analysis_state_path",
    "detect_project_state",
    "generate_partial_artifacts",
    "load_research_context",
    "project_specs_path",
    "read_deep_analysis_state",
    "refresh_research",
    "research_state_path",
    "run_deep_analysis",
    "run_onboarding",
    "scan_workspace",
    "should_run_onboarding",
    "user_summary_path",
    "write_deep_analysis_state",
    "write_project_research",
]
