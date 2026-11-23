"""Sandboxed shell and git helpers for agents.

Residual risk: path validation and git execution are not atomic (TOCTOU).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from .policy import ShellPolicy, detect_owner_repo, is_safe_path


_SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
_MAX_OUTPUT = 8 * 1024
_REF_RE = re.compile(r"^[A-Za-z0-9._/^~-]+$")


def _sanitize_env(env: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            result[key] = value
    if isinstance(env, dict):
        for key, value in env.items():
            # PATH and HOME come from os.environ only; ignore caller overrides.
            if key in _SAFE_ENV_KEYS and key not in ("PATH", "HOME"):
                result[key] = str(value)
    return result




def _validate_ref(ref: Any) -> bool:
    s = "" if ref is None else str(ref)
    if not s:
        return True
    if s.startswith("-"):
        return False
    return bool(_REF_RE.fullmatch(s))


def _path_arg_in_workspace(value: str, ws: Path, cwd_str: str) -> bool:
    """Validar un argumento de ruta (p.ej. ``git -C <dir>``) contra el workspace."""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(cwd_str) / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = candidate
    return is_safe_path(candidate, ws) and is_safe_path(resolved, ws)


def run_shell(command: str, cwd: Any, policy: Any, timeout: int, env: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> dict[str, Any]:
    """Run a command under a sanitized environment and policy."""
    if workspace_root is None:
        workspace_root = root if root is not None else ws_root

    shell_policy: ShellPolicy
    if isinstance(policy, ShellPolicy):
        shell_policy = policy
    else:
        shell_policy = ShellPolicy(policy)

    if not shell_policy.allows_command(command):
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": 1,
            "timed_out": False,
            "blocked": True,
        }

    # Resolve and validate cwd.
    try:
        cwd_path = Path(cwd) if cwd is not None else Path(".")
        if not cwd_path.is_absolute():
            base = Path(workspace_root).expanduser().resolve() if workspace_root is not None else Path.cwd()
            cwd_path = base / cwd_path
        if workspace_root is not None:
            ws = Path(workspace_root).expanduser().resolve()
            if not is_safe_path(cwd_path, ws):
                return {
                    "stdout": "",
                    "stderr": f"ERROR: path is outside the workspace: {cwd}",
                    "exit_code": 1,
                    "timed_out": False,
                    "blocked": False,
                }
        resolved_cwd = cwd_path.resolve(strict=False)
        if workspace_root is not None:
            ws = Path(workspace_root).expanduser().resolve()
            if not is_safe_path(resolved_cwd, ws):
                return {
                    "stdout": "",
                    "stderr": f"ERROR: path is outside the workspace: {cwd}",
                    "exit_code": 1,
                    "timed_out": False,
                    "blocked": False,
                }
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            return {
                "stdout": "",
                "stderr": f"ERROR: invalid working directory: {cwd}",
                "exit_code": 1,
                "timed_out": False,
                "blocked": False,
            }
        cwd_str = str(resolved_cwd)
    except Exception as exc:  # noqa: BLE001
        return {
            "stdout": "",
            "stderr": f"ERROR: invalid working directory: {exc}",
            "exit_code": 1,
            "timed_out": False,
            "blocked": False,
        }

    # shlex split; no shell=True fallback.
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return {
            "stdout": "",
            "stderr": f"ERROR: cannot parse command: {exc}",
            "exit_code": 127,
            "timed_out": False,
            "blocked": False,
        }

    # El jail cubre cwd Y argumentos de ruta (p.ej. ``git -C <dir>``,
    # ``--git-dir``/``--work-tree``): un comando permitido por política no
    # puede alcanzar repositorios fuera del workspace.
    if workspace_root is not None:
        ws = Path(workspace_root).expanduser().resolve()
        for i, tok in enumerate(args):
            path_arg: str | None = None
            if tok in ("-C", "--git-dir", "--work-tree"):
                if i + 1 < len(args):
                    path_arg = args[i + 1]
            elif tok.startswith("--git-dir=") or tok.startswith("--work-tree="):
                path_arg = tok.split("=", 1)[1]
            if path_arg:
                if not _path_arg_in_workspace(path_arg, ws, cwd_str):
                    return {
                        "stdout": "",
                        "stderr": f"comando rechazado: ruta fuera del workspace permitido: {path_arg}",
                        "exit_code": 1,
                        "timed_out": False,
                        "blocked": False,
                    }

    safe_env = _sanitize_env(env)

    stdout = ""
    stderr = ""
    exit_code = 1
    timed_out = False
    try:
        proc = subprocess.run(
            args,
            shell=False,
            cwd=cwd_str,
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        # kill is implicit for run; ensure process terminated
        try:
            if exc.process is not None:
                exc.process.kill()
        except Exception:  # noqa: BLE001
            pass
        exit_code = 124
    except FileNotFoundError as exc:
        stderr = f"ERROR: command not found: {exc}"
        exit_code = 127
    except OSError as exc:
        stderr = f"ERROR: {exc}"
        exit_code = 1

    if len(stdout) > _MAX_OUTPUT:
        stdout = stdout[:_MAX_OUTPUT]
    if len(stderr) > _MAX_OUTPUT:
        stderr = stderr[:_MAX_OUTPUT]

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "blocked": False,
    }


_call_run_shell = run_shell


def _run_git(args: list[str], repo: Any, timeout: int = 60, policy: Any = None, shell_policy: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> str:
    if workspace_root is None:
        workspace_root = root if root is not None else ws_root
    if policy is None:
        policy = shell_policy

    try:
        repo_path = Path(repo)
        if not repo_path.is_absolute() and workspace_root is not None:
            repo_path = Path(workspace_root).expanduser().resolve() / repo_path
        if workspace_root is not None:
            ws = Path(workspace_root).expanduser().resolve()
            if not is_safe_path(repo_path, ws):
                return f"ERROR: path is outside the workspace: {repo}"
        resolved_repo = repo_path.resolve(strict=False)
        if workspace_root is not None:
            ws = Path(workspace_root).expanduser().resolve()
            if not is_safe_path(resolved_repo, ws):
                return f"ERROR: path is outside the workspace: {repo}"
        if not resolved_repo.exists() or not resolved_repo.is_dir():
            return f"ERROR: invalid repository path: {repo}"
        repo_path = resolved_repo
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: invalid repository path: {exc}"

    # Execution-time policy check.
    if policy is not None:
        shell_policy = policy if isinstance(policy, ShellPolicy) else ShellPolicy(policy)
        command_str = "git " + " ".join(str(a) for a in (args or []))
        if not shell_policy.allows_command(command_str):
            return f"BLOCKED: command not allowed by policy: {command_str}"

    safe_env = _sanitize_env(None)
    try:
        proc = subprocess.run(
            ["git", *args],
            shell=False,
            cwd=str(repo_path),
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    out = (proc.stdout or "")
    err = (proc.stderr or "")
    combined = out
    if err.strip():
        combined = (combined + "\n" + err).strip()
    if not combined.strip():
        combined = f"(exit {proc.returncode})"
    if len(combined) > _MAX_OUTPUT:
        combined = combined[:_MAX_OUTPUT]
    return combined.strip()


def git_status(repo: Any, timeout: int = 60, policy: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> str:
    return _run_git(["status"], repo, timeout=timeout, policy=policy, workspace_root=workspace_root, root=root, ws_root=ws_root)


def git_diff(repo: Any, *args: Any, timeout: int = 60, policy: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> str:
    for a in args:
        if not _validate_ref(a):
            return f"ERROR: invalid ref: {a}"
    return _run_git(["diff", *list(args)], repo, timeout=timeout, policy=policy, workspace_root=workspace_root, root=root, ws_root=ws_root)


def git_log(repo: Any, n: int = 10, timeout: int = 60, policy: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> str:
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 10
    if count < 1:
        count = 1
    if count > 1000:
        count = 1000
    return _run_git(["log", f"-{count}"], repo, timeout=timeout, policy=policy, workspace_root=workspace_root, root=root, ws_root=ws_root)


def git_show(repo: Any, ref: Any = "HEAD", timeout: int = 60, policy: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> str:
    if not _validate_ref(ref):
        return f"ERROR: invalid ref: {ref}"
    return _run_git(["show", str(ref)], repo, timeout=timeout, policy=policy, workspace_root=workspace_root, root=root, ws_root=ws_root)


def git_commit(message: str, files: Any, repo_path: Any, approved: bool = False, timeout: int = 60, policy: Any = None, shell_policy: Any = None, workspace_root: Any = None, root: Any = None, ws_root: Any = None) -> str:
    if not approved:
        return "COMMIT NOT APPROVED"
    if workspace_root is None:
        workspace_root = root if root is not None else ws_root
    if policy is None:
        policy = shell_policy

    # Resolve repo.
    try:
        repo = Path(repo_path)
        if not repo.is_absolute() and workspace_root is not None:
            repo = Path(workspace_root).expanduser().resolve() / repo
        if workspace_root is not None:
            ws = Path(workspace_root).expanduser().resolve()
            if not is_safe_path(repo, ws):
                return f"ERROR: path is outside the workspace: {repo_path}"
        resolved_repo = repo.resolve(strict=False)
        if workspace_root is not None:
            ws = Path(workspace_root).expanduser().resolve()
            if not is_safe_path(resolved_repo, ws):
                return f"ERROR: path is outside the workspace: {repo_path}"
        if not resolved_repo.exists() or not resolved_repo.is_dir():
            return f"ERROR: invalid repository path: {repo_path}"
        repo = resolved_repo
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: invalid repository path: {exc}"

    file_list: list[str]
    if isinstance(files, (list, tuple)):
        file_list = [str(f) for f in files]
    elif isinstance(files, str):
        file_list = [files]
    else:
        file_list = [str(files)]
    if not file_list:
        return "ERROR: no files specified"
    if not message:
        return "ERROR: empty commit message"
    if "\n" in str(message) or "\r" in str(message):
        return "ERROR: commit message must not contain newlines"

    # Validate each file entry.
    repo_resolved = repo.resolve()
    for entry in file_list:
        if not str(entry).strip():
            return "ERROR: empty file entry"
        entry_str = str(entry)
        if entry_str.startswith(":") or entry_str.startswith("!"):
            return f"ERROR: unsafe file entry: {entry}"
        if any(ch in entry_str for ch in "*?[]"):
            return f"ERROR: unsafe file entry: {entry}"
        if any(magic in entry_str for magic in (":(", ":!", ":/")):
            return f"ERROR: unsafe file entry: {entry}"
        p = Path(entry)
        if p.is_absolute():
            return f"ERROR: absolute path not allowed: {entry}"
        if ".." in p.parts:
            return f"ERROR: path traversal not allowed: {entry}"
        candidate = (repo_resolved / p).resolve()
        if not candidate.is_relative_to(repo_resolved):
            return f"ERROR: path escapes repository: {entry}"

    # Execution-time policy check.
    if policy is not None:
        shell_policy = policy if isinstance(policy, ShellPolicy) else ShellPolicy(policy)
        commit_cmd = "git commit -m " + str(message)
        if not shell_policy.allows_command(commit_cmd):
            return f"BLOCKED: command not allowed by policy: {commit_cmd}"

    safe_env = _sanitize_env(None)
    # Stage only specified files.
    try:
        add_proc = subprocess.run(
            ["git", "add", "--", *file_list],
            shell=False,
            cwd=str(repo),
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    if add_proc.returncode != 0:
        return (add_proc.stderr or add_proc.stdout or "git add failed").strip()
    try:
        commit_proc = subprocess.run(
            ["git", "commit", "-m", message],
            shell=False,
            cwd=str(repo),
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {exc}"
    out = (commit_proc.stdout or "")
    err = (commit_proc.stderr or "")
    combined = (out + "\n" + err).strip()
    if len(combined) > _MAX_OUTPUT:
        combined = combined[:_MAX_OUTPUT]
    if commit_proc.returncode != 0:
        return combined or "git commit failed"
    return combined or "committed"


def _has_allow(policy: Any, action: str, commands: list[str]) -> bool:
    """Return True if any allow rule for action matches any probe command.

    This checks for the presence of an allow (not the final allow/deny decision)
    so tool exposure works with deny-precedence policies that also include
    specific allows (e.g. ``shell deny *`` plus ``git diff* allow``). Runtime
    execution still enforces full deny-precedence checks.
    """
    action_l = (action or "").strip().lower()
    for cmd in commands:
        for rule in getattr(getattr(policy, "policy", policy), "rules", []):
            rule_action = (getattr(rule, "action", "") or "").strip().lower()
            if not (rule_action == action_l or rule_action == "*"):
                continue
            if (getattr(rule, "effect", "") or "").strip().lower() != "allow":
                continue
            try:
                import fnmatch

                if fnmatch.fnmatchcase(str(cmd), rule.resource):
                    return True
            except Exception:  # noqa: BLE001
                pass
    return False


def make_shell_tool(workspace_root: Any, policy: Any, timeout: int) -> Any:
    root = Path(workspace_root).expanduser().resolve()
    shell_policy = policy if isinstance(policy, ShellPolicy) else ShellPolicy(policy)

    @tool
    def run_shell(command: str, cwd: str = ".") -> str:
        """Run a shell command if the policy allows it."""
        if not shell_policy.allows_command(command):
            return f"BLOCKED: command not allowed by policy: {command}"
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            cwd_path = root / cwd_path
        if not is_safe_path(cwd_path, root):
            return f"ERROR: path is outside the workspace: {cwd}"
        try:
            resolved_cwd = cwd_path.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved_cwd = cwd_path
        if not is_safe_path(resolved_cwd, root):
            return f"ERROR: path is outside the workspace: {cwd}"
        result = _call_run_shell(command, str(resolved_cwd), policy, timeout, workspace_root=root)
        if result.get("blocked"):
            return f"BLOCKED: command not allowed by policy: {command}"
        parts = [f"exit_code: {result.get('exit_code')}"]
        if result.get("timed_out"):
            parts.append("timed_out: True")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        parts.append(f"stdout:\n{stdout}")
        parts.append(f"stderr:\n{stderr}")
        return "\n".join(parts)

    return run_shell


_git_status_impl = git_status
_git_diff_impl = git_diff
_git_log_impl = git_log
_git_show_impl = git_show
_git_commit_impl = git_commit


def make_git_tools(workspace_root: Any, policy: Any) -> list[Any]:
    root = Path(workspace_root).expanduser().resolve()
    tools: list[Any] = []

    status_cmds = ["git status", "git -C . status"]
    diff_cmds = ["git diff", "git -C . diff"]
    log_cmds = ["git log", "git -C . log"]
    show_cmds = ["git show", "git -C . show"]
    commit_cmds = ["git commit -m x", "git -C . commit -m x"]

    if _has_allow(policy, "shell", status_cmds):
        @tool
        def git_status(repo_path: str = ".") -> str:
            """Show git status for a repository."""
            repo = _safe_repo(repo_path, root)
            if isinstance(repo, str):
                return repo
            return _git_status_impl(repo, policy=policy, workspace_root=root)

        tools.append(git_status)

    if _has_allow(policy, "shell", diff_cmds):
        @tool
        def git_diff(repo_path: str = ".", ref: str = "") -> str:
            """Show git diff for a repository."""
            repo = _safe_repo(repo_path, root)
            if isinstance(repo, str):
                return repo
            if ref:
                return _git_diff_impl(repo, ref, policy=policy, workspace_root=root)
            return _git_diff_impl(repo, policy=policy, workspace_root=root)

        tools.append(git_diff)

    if _has_allow(policy, "shell", log_cmds):
        @tool
        def git_log(repo_path: str = ".", n: int = 10) -> str:
            """Show recent git log entries."""
            repo = _safe_repo(repo_path, root)
            if isinstance(repo, str):
                return repo
            return _git_log_impl(repo, n, policy=policy, workspace_root=root)

        tools.append(git_log)

    if _has_allow(policy, "shell", show_cmds):
        @tool
        def git_show(repo_path: str = ".", ref: str = "HEAD") -> str:
            """Show a git commit or object."""
            repo = _safe_repo(repo_path, root)
            if isinstance(repo, str):
                return repo
            return _git_show_impl(repo, ref, policy=policy, workspace_root=root)

        tools.append(git_show)

    if _has_allow(policy, "shell", commit_cmds):
        @tool
        def git_commit(message: str, files: Any, repo_path: str = ".", approved: bool = False) -> str:
            """Commit only the specified files; requires approved=True."""
            if not approved:
                return "COMMIT NOT APPROVED"
            repo = _safe_repo(repo_path, root)
            if isinstance(repo, str):
                return repo
            return _git_commit_impl(message, files, repo, approved=True, policy=policy, workspace_root=root)

        tools.append(git_commit)

    return tools


def _safe_repo(repo_path: Any, root: Path) -> Any:
    candidate = Path(repo_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not is_safe_path(candidate, root):
        return f"ERROR: path is outside the workspace: {repo_path}"
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = candidate
    if not is_safe_path(resolved, root):
        return f"ERROR: path is outside the workspace: {repo_path}"
    return detect_owner_repo(resolved, root)
