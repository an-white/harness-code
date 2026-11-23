"""Permission policy, shell policy and path safety helpers."""

from __future__ import annotations

import fnmatch
import os
import shlex
from pathlib import Path
from typing import Any

from app.agents.loader import AgentSpec, PermissionRule


class PermissionPolicy:
    """Permission policy with deny precedence and a default effect."""

    def __init__(self, rules: list[PermissionRule], default_effect: str = "deny"):
        self.rules = list(rules)
        self.default_effect = (default_effect or "deny").strip().lower()

    def allows(self, action: str, resource: str) -> bool:
        action_l = (action or "").strip().lower()
        denied = False
        allowed = False
        for rule in self.rules:
            rule_action = (rule.action or "").strip().lower()
            if not (rule_action == action_l or rule_action == "*"):
                continue
            try:
                matched = fnmatch.fnmatchcase(str(resource), rule.resource)
            except (TypeError, ValueError):
                matched = False
            if not matched:
                continue
            effect = (rule.effect or "").strip().lower()
            if effect == "deny":
                denied = True
            elif effect == "allow":
                allowed = True
        if denied:
            return False
        if allowed:
            return True
        return self.default_effect == "allow"


class ShellPolicy:
    """Wrapper that evaluates shell rules against a raw command string."""

    def __init__(self, policy: PermissionPolicy):
        self.policy = policy

    def allows_command(self, command: str) -> bool:
        raw = "" if command is None else str(command)
        raw_allowed = self.policy.allows("shell", raw)
        # Canonicalize via shlex so spacing/basename tricks cannot bypass rules.
        try:
            parts = shlex.split(raw)
        except ValueError:
            return False
        if parts:
            canonical = " ".join(
                [os.path.basename(str(parts[0]))] + [str(p) for p in parts[1:]]
            )
        else:
            canonical = ""
        canonical_allowed = self.policy.allows("shell", canonical)
        if not raw_allowed and not canonical_allowed:
            # Still run token-level checks below to allow cases like
            # `env echo hi` when `echo*` is allowed, unless denied.
            token_allow_match = False
        else:
            token_allow_match = False

        # Token-level evaluation: deny if any token/basename/value-basename
        # matches a deny rule (closes wrapper/path-qualified bypasses such as
        # `env /usr/bin/bad` or `sh -c /bin/bad` when `bad*` is denied).
        # Also track token-level allow matches so wrapped commands like
        # `env echo hi` can be allowed when the target token is allowed,
        # without rejecting legitimate multi-token allow rules that match on
        # raw/canonical forms.
        for token in parts:
            candidates = [str(token)]
            base = os.path.basename(str(token))
            if base not in candidates:
                candidates.append(base)
            if "=" in str(token):
                _, _, value = str(token).partition("=")
                value_base = os.path.basename(value)
                if value_base and value_base not in candidates:
                    candidates.append(value_base)
            for rule in self.policy.rules:
                rule_action = (getattr(rule, "action", "") or "").strip().lower()
                if not (rule_action == "shell" or rule_action == "*"):
                    continue
                effect = (getattr(rule, "effect", "") or "").strip().lower()
                for candidate in candidates:
                    try:
                        matched = fnmatch.fnmatchcase(candidate, rule.resource)
                    except (TypeError, ValueError):
                        matched = False
                    if not matched:
                        continue
                    if effect == "deny":
                        return False
                    if effect == "allow":
                        token_allow_match = True
        return raw_allowed or canonical_allowed or token_allow_match


def build_policy(spec: AgentSpec) -> PermissionPolicy:
    return PermissionPolicy(list(spec.permissions), default_effect="deny")


def build_shell_policy(spec: AgentSpec) -> ShellPolicy:
    return ShellPolicy(build_policy(spec))


def is_safe_path(path: Any, workspace_root: Any) -> bool:
    """Resolve symlinks and ensure the path stays inside the workspace root."""
    try:
        root = Path(workspace_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        return resolved.is_relative_to(root)
    except ValueError:
        return False


def detect_owner_repo(path: Any, workspace_root: Any) -> Path:
    """Walk up from ``path`` looking for a ``.git`` entry.

    Returns the ancestor containing ``.git``, or ``workspace_root`` if none is
    found. Never escapes ``workspace_root``.
    """
    try:
        root = Path(workspace_root).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(workspace_root)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved = candidate
    if not resolved.is_relative_to(root):
        resolved = root
    if resolved.is_file():
        cur = resolved.parent
    else:
        cur = resolved
    while True:
        if not cur.is_relative_to(root):
            cur = root
        git_entry = cur / ".git"
        try:
            if git_entry.exists():
                return cur
        except OSError:
            pass
        if cur == root:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return root
