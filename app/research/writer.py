"""Deterministic writers for research artifacts and state."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.i18n import normalize_language


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def research_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root).expanduser().resolve() / ".agents" / "research"


def docs_dir(workspace_root: Path | str) -> Path:
    return Path(workspace_root).expanduser().resolve() / "docs"


def project_specs_path(workspace_root: Path | str, lang: str | None = None) -> Path:
    root = docs_dir(workspace_root)
    norm = normalize_language(lang) if lang else None
    primary = (norm or "en").split("-", 1)[0]
    if primary == "en":
        return root / "project_specs.md"
    safe = re.sub(r"[^a-z0-9-]", "-", primary)
    return root / f"project_specs.{safe}.md"


def research_report_path(workspace_root: Path | str, slug: str, lang: str | None = None) -> Path:
    safe_slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (slug or "report").strip()).strip("-") or "report"
    base = docs_dir(workspace_root) / "research"
    norm = normalize_language(lang) if lang else None
    primary = (norm or "en").split("-", 1)[0]
    if primary == "en":
        return base / f"{safe_slug}.md"
    safe_lang = re.sub(r"[^a-z0-9-]", "-", primary)
    return base / f"{safe_slug}.{safe_lang}.md"


def context_digest_path(workspace_root: Path | str) -> Path:
    return research_dir(workspace_root) / "context.md"


def research_state_path(workspace_root: Path | str) -> Path:
    return research_dir(workspace_root) / "state.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _md_list(items: Any) -> str:
    vals = [str(x) for x in _as_list(items) if str(x).strip()]
    if not vals:
        return "- (none)"
    return "\n".join(f"- {v}" for v in vals)


def _md_code_block(items: Any, lang: str = "text") -> str:
    vals = [str(x).rstrip() for x in _as_list(items) if str(x).strip()]
    if not vals:
        vals = ["(none)"]
    body = "\n".join(vals)
    return f"```{lang}\n{body}\n```"


def _normalize_structure_item(item: str) -> str | None:
    s = str(item or "").strip()
    if not s:
        return None
    for prefix in ("Repo:", "Dir:", "File:", "Path:"):
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    if s in {"", "(none)"}:
        return None
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if s.endswith("/") and s != "/":
        s = s.rstrip("/")
    return s or None


def _md_dir_tree(items: Any) -> str:
    vals = [str(x) for x in _as_list(items) if str(x).strip()]
    if not vals:
        return "```text\n(none)\n```"
    paths: list[str] = []
    root_label: str | None = None
    for v in vals:
        norm = _normalize_structure_item(v)
        if norm is None:
            continue
        if norm in {".", "/"}:
            root_label = root_label or norm
            continue
        paths.append(norm)
    if not paths and root_label is None:
        # fallback: render raw items in a text block
        return _md_code_block(vals, "text")
    root_label = root_label or "."
    tree: dict[str, Any] = {}
    for p in sorted(set(paths)):
        parts = [part for part in p.split("/") if part and part != "."]
        if not parts:
            continue
        node = tree
        for part in parts:
            node = node.setdefault(part, {})

    def render_node(node: dict[str, Any], prefix: str = "") -> list[str]:
        lines: list[str] = []
        keys = sorted(node.keys())
        for i, key in enumerate(keys):
            last = i == len(keys) - 1
            connector = "└── " if last else "├── "
            lines.append(f"{prefix}{connector}{key}")
            child = node[key]
            if child:
                ext = "    " if last else "│   "
                lines.extend(render_node(child, prefix + ext))
        return lines

    body_lines = [root_label] + render_node(tree)
    return "```text\n" + "\n".join(body_lines) + "\n```"


def _md_kv_table(rows: list[tuple[str, Any]]) -> str:
    out = ["| Key | Value |", "| --- | --- |"]
    for k, v in rows:
        val = str(v) if v is not None else ""
        val = val.replace("\n", "<br>")
        out.append(f"| {k} | {val} |")
    return "\n".join(out)


def render_project_specs_markdown(content: dict[str, Any], meta: dict[str, Any]) -> str:
    """Render canonical English project specs markdown deterministically."""
    title = str(content.get("title") or "Project Specs")
    digest = str(content.get("digest") or "").strip()
    sections = [
        "# " + title,
        "",
        "## Metadata",
        _md_kv_table(
            [
                ("Generated (UTC)", meta.get("generated_at") or ""),
                ("Commit/HEAD", meta.get("head") or meta.get("commit") or ""),
                ("Fingerprint", meta.get("fingerprint") or ""),
                ("Freshness", meta.get("freshness") or "fresh"),
                ("Language", "en"),
                ("Workspace", meta.get("workspace_root") or ""),
            ]
        ),
        "",
        "## Quick Context for Agents",
        str(content.get("quick_context") or digest or "(none)").strip() or "(none)",
        "",
        "## Business / What the Project Does",
        str(content.get("business") or content.get("what_project_does") or "(none)").strip() or "(none)",
        "",
        "## Repositories / Structure",
        _md_dir_tree(content.get("repositories_structure") or content.get("repos_structure") or content.get("structure")),
        "",
        "## Architecture / Data Flow",
        _md_list(content.get("architecture_data_flow") or content.get("architecture")),
        "",
        "## Domain Model / Rules",
        _md_list(content.get("domain_model_rules") or content.get("domain")),
        "",
        "## Ubiquitous Language",
    ]
    terms = _as_list(content.get("ubiquitous_language") or content.get("terms") or [])
    if terms:
        sections.append("| Term | Meaning | Where used |")
        sections.append("| --- | --- | --- |")
        for t in terms:
            if isinstance(t, dict):
                term = str(t.get("term") or "").replace("|", "/")
                meaning = str(t.get("meaning") or "").replace("|", "/").replace("\n", " ")
                where = str(t.get("where_used") or t.get("where") or "").replace("|", "/").replace("\n", " ")
                sections.append(f"| {term} | {meaning} | {where} |")
            else:
                s = str(t).replace("|", "/")
                sections.append(f"| {s} |  |  |")
    else:
        sections.append("- (none)")

    sections.extend(
        [
            "",
            "## Acronyms",
            _md_list(content.get("acronyms")),
            "",
            "## Stack / Versions",
            _md_list(content.get("stack_versions") or content.get("stack")),
            "",
            "## Data Sources / Integrations",
            _md_list(content.get("data_sources_integrations") or content.get("integrations")),
            "",
            "## Entry Points / Build / Test / Run",
            _md_code_block(content.get("entry_points_build_test_run") or content.get("entry_points"), "text"),
            "",
            "## Conventions / Patterns",
            _md_list(content.get("conventions_patterns") or content.get("conventions")),
            "",
            "## Key Files Index",
            _md_dir_tree(content.get("key_files_index") or content.get("key_files")),
            "",
            "## Risks / Debt / Gotchas",
            _md_list(content.get("risks_debt_gotchas") or content.get("risks")),
            "",
            "## FAQ / Where to Find",
            _md_list(content.get("faq_where_to_find") or content.get("faq")),
            "",
            "## Sources / Research Log",
            _md_list(content.get("sources_research_log") or content.get("sources")),
            "",
        ]
    )
    return "\n".join(sections).rstrip() + "\n"


def render_context_digest(content: dict[str, Any], meta: dict[str, Any], max_chars: int) -> str:
    """Render compact English digest for prompts within char budget."""
    max_chars = max(200, int(max_chars or 8000))
    title = str(content.get("title") or "Project Context Digest")
    quick = str(content.get("quick_context") or content.get("digest") or "").strip()
    business = str(content.get("business") or content.get("what_project_does") or "").strip()
    stack = [str(x) for x in _as_list(content.get("stack_versions") or content.get("stack")) if str(x).strip()]
    entry = [str(x) for x in _as_list(content.get("entry_points_build_test_run") or content.get("entry_points")) if str(x).strip()]
    key_files = [str(x) for x in _as_list(content.get("key_files_index") or content.get("key_files")) if str(x).strip()]
    risks = [str(x) for x in _as_list(content.get("risks_debt_gotchas") or content.get("risks")) if str(x).strip()]
    terms = _as_list(content.get("ubiquitous_language") or content.get("terms") or [])
    term_lines: list[str] = []
    for t in terms[:12]:
        if isinstance(t, dict):
            term_lines.append(f"{t.get('term','')}: {t.get('meaning','')}".strip(": "))
        else:
            term_lines.append(str(t))

    parts = [
        f"# {title}",
        f"Fingerprint: {meta.get('fingerprint','')} | Freshness: {meta.get('freshness','fresh')} | Generated: {meta.get('generated_at','')}",
        "",
        "## Quick context",
        quick or "(none)",
        "",
        "## Business",
        business or "(none)",
        "",
        "## Stack",
        "\n".join(f"- {x}" for x in stack[:12]) or "- (none)",
        "",
        "## Entry/build/test/run",
        "\n".join(f"- {x}" for x in entry[:12]) or "- (none)",
        "",
        "## Key files",
        "\n".join(f"- {x}" for x in key_files[:12]) or "- (none)",
        "",
        "## Domain terms",
        "\n".join(f"- {x}" for x in term_lines[:12]) or "- (none)",
        "",
        "## Risks/gotchas",
        "\n".join(f"- {x}" for x in risks[:10]) or "- (none)",
        "",
        f"Canonical specs: docs/project_specs.md",
    ]
    text = "\n".join(parts).strip() + "\n"
    if len(text) <= max_chars:
        return text
    # deterministic truncation preserving header/metadata and tail note
    suffix = "\n...[truncated to research_context_chars]\n"
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def _write_text(path: Path, text: str) -> None:
    _ensure_parent(path)
    path.write_text(text, encoding="utf-8")


def write_research_state(workspace_root: Path | str, state: dict[str, Any]) -> Path:
    path = research_state_path(workspace_root)
    _ensure_parent(path)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_research_state(workspace_root: Path | str) -> dict[str, Any]:
    path = research_state_path(workspace_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_research_context(workspace_root: Path | str) -> dict[str, Any]:
    """Load digest/spec paths and current state for prompt injection hooks."""
    root = Path(workspace_root).expanduser().resolve()
    state = read_research_state(root)
    digest_path = context_digest_path(root)
    specs_path = project_specs_path(root, "en")
    digest = ""
    try:
        if digest_path.is_file():
            digest = digest_path.read_text(encoding="utf-8")
    except Exception:
        digest = ""
    return {
        "state": state,
        "digest": digest,
        "digest_path": str(digest_path),
        "specs_path": str(specs_path),
        "fresh": bool(state.get("fresh")) if "fresh" in state else False,
        "onboarding_done": bool(state.get("onboarding_done")),
        "fingerprint": state.get("fingerprint"),
    }


def _detect_existing_localized_specs_lang(root: Path) -> str | None:
    """Return primary language of existing docs/project_specs.<lang>.md if unambiguous."""
    docs = docs_dir(root)
    if not docs.is_dir():
        return None
    langs: list[str] = []
    for p in docs.glob("project_specs.*.md"):
        name = p.name
        if not (name.startswith("project_specs.") and name.endswith(".md")):
            continue
        mid = name[len("project_specs.") : -len(".md")]
        norm = normalize_language(mid)
        if norm:
            primary = norm.split("-", 1)[0]
            if primary and primary != "en":
                langs.append(primary)
    uniq = sorted(set(langs))
    return uniq[0] if len(uniq) == 1 else None


def _resolve_harness_localized_lang(
    root: Path,
    interaction_language: str | None,
    harness_interaction_language: str | None = None,
) -> str | None:
    for candidate in (harness_interaction_language, interaction_language):
        norm = normalize_language(candidate) if candidate else None
        if norm:
            primary = norm.split("-", 1)[0]
            if primary != "en":
                return primary
    # fallback: if caller context lost language (en/None), prefer existing unambiguous localized specs
    detected = _detect_existing_localized_specs_lang(root)
    if detected:
        return detected
    state = read_research_state(root)
    if isinstance(state, dict):
        st_lang = normalize_language(state.get("localized_language")) if state.get("localized_language") else None
        if st_lang and st_lang.split("-", 1)[0] != "en":
            return st_lang.split("-", 1)[0]
        pending = state.get("localized_specs_pending_update")
        if isinstance(pending, dict):
            p_lang = normalize_language(pending.get("language")) if pending.get("language") else None
            if p_lang and p_lang.split("-", 1)[0] != "en":
                return p_lang.split("-", 1)[0]
    return None


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except Exception:
        return path.name


def _try_localize_canonical(canonical_md: str, lang: str, localize: Callable[[str, str], str] | None) -> tuple[str | None, str | None]:
    """Return (localized_markdown, unavailable_reason). Never falls back to canonical/marker."""
    if localize is None:
        return None, "translation_unavailable"
    try:
        out = localize(canonical_md, lang)
    except Exception as exc:  # noqa: BLE001
        return None, f"translation_error:{type(exc).__name__}"
    if out is None:
        return None, "translation_empty"
    if not isinstance(out, str):
        out = str(out)
    if not out.strip():
        return None, "translation_empty"
    # treat unchanged canonical as unavailable/failure signal for this workflow
    if out.strip() == canonical_md.strip():
        return None, "translation_unavailable"
    return out, None


def write_project_research(
    workspace_root: Path | str,
    content: dict[str, Any],
    *,
    fingerprint: str,
    freshness: str = "fresh",
    head: str | None = None,
    interaction_language: str | None = None,
    harness_interaction_language: str | None = None,
    context_max_chars: int = 8000,
    localize: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Write canonical/localized specs, digest and state deterministically."""
    root = Path(workspace_root).expanduser().resolve()
    generated_at = _utc_now_iso()
    meta = {
        "generated_at": generated_at,
        "fingerprint": fingerprint,
        "freshness": freshness,
        "head": head or "",
        "workspace_root": root.name or ".",
    }
    canonical_md = render_project_specs_markdown(content, meta)
    specs_en = project_specs_path(root, "en")
    _write_text(specs_en, canonical_md)

    target_lang = _resolve_harness_localized_lang(root, interaction_language, harness_interaction_language)
    localized_path: Path | None = project_specs_path(root, target_lang) if target_lang else None
    localized_written = False
    localized_updated = False
    localized_pending: dict[str, Any] | None = None
    localized_notice: str | None = None

    if target_lang and localized_path is not None:
        exists = localized_path.is_file()
        translated, reason = _try_localize_canonical(canonical_md, target_lang, localize)
        if translated is not None:
            _write_text(localized_path, translated)
            localized_written = True
            localized_updated = True
        else:
            if exists:
                # translation unavailable: do not overwrite existing localized copy; record evidence
                localized_pending = {
                    "language": target_lang,
                    "path": _relative_to_root(localized_path, root),
                    "reason": reason or "translation_unavailable",
                    "canonical_updated_at": generated_at,
                    "head": head or "",
                    "fingerprint": fingerprint,
                }
                localized_notice = (
                    f"Localized specs pending update: {localized_pending['path']} (lang={target_lang}, "
                    f"reason={localized_pending['reason']}). Update before next task execution."
                )
            else:
                # no existing localized copy; cannot create without translation -> evidence for user/agents
                localized_pending = {
                    "language": target_lang,
                    "path": _relative_to_root(localized_path, root),
                    "reason": reason or "translation_unavailable",
                    "canonical_updated_at": generated_at,
                    "head": head or "",
                    "fingerprint": fingerprint,
                }
                localized_notice = (
                    f"Localized specs pending update: {localized_pending['path']} (lang={target_lang}, "
                    f"reason={localized_pending['reason']}). Update before next task execution."
                )

    digest = render_context_digest(content, meta, max_chars=context_max_chars)
    digest_path = context_digest_path(root)
    _write_text(digest_path, digest)

    prev_state = read_research_state(root)
    state = {
        "fingerprint": fingerprint,
        "fresh": freshness == "fresh",
        "freshness": freshness,
        "onboarding_done": True,
        "generated_at": generated_at,
        "head": head or "",
        "paths": {
            "specs_en": _relative_to_root(specs_en, root),
            "specs_localized": _relative_to_root(localized_path, root) if (localized_path and localized_path.exists()) else None,
            "context": _relative_to_root(digest_path, root),
            "state": _relative_to_root(research_state_path(root), root),
        },
        "language": "en",
        "localized_language": target_lang if (localized_path and localized_path.exists()) else None,
    }
    # preserve unrelated prior fields (e.g. synthesis info may be added by caller; keep last_access_head etc.)
    if isinstance(prev_state, dict):
        for k, v in prev_state.items():
            if k in state:
                continue
            state[k] = v
    # overwrite/clear pending localized update evidence according to this write
    if localized_pending is not None:
        state["localized_specs_pending_update"] = localized_pending
        state["localized_specs_pending_notice"] = localized_notice
    else:
        state.pop("localized_specs_pending_update", None)
        state.pop("localized_specs_pending_notice", None)
    state_path = write_research_state(root, state)
    return {
        "specs_path": str(specs_en),
        "localized_specs_path": str(localized_path) if (localized_path and localized_path.exists()) else None,
        "context_path": str(digest_path),
        "state_path": str(state_path),
        "state": state,
        "localized_updated": localized_updated,
        "localized_pending_update": localized_pending,
        "localized_notice": localized_notice,
    }


def write_research_report(
    workspace_root: Path | str,
    slug: str,
    report_markdown: str,
    *,
    interaction_language: str | None = None,
    localize: Callable[[str, str], str] | None = None,
) -> dict[str, str | None]:
    """Write reusable point-research report (English canonical + localized copy)."""
    root = Path(workspace_root).expanduser().resolve()
    en_path = research_report_path(root, slug, "en")
    _write_text(en_path, report_markdown if report_markdown.endswith("\n") else report_markdown + "\n")
    localized_path = None
    lang = normalize_language(interaction_language) or "en"
    primary = lang.split("-", 1)[0]
    if primary != "en":
        loc = research_report_path(root, slug, primary)
        text = report_markdown
        if localize is not None:
            try:
                text = localize(report_markdown, primary)
            except Exception:
                text = report_markdown
        else:
            text = (
                f"> Localized copy requested for language '{primary}'. "
                "Automatic translation unavailable; canonical English content follows.\n\n" + report_markdown
            )
        _write_text(loc, text if text.endswith("\n") else text + "\n")
        localized_path = str(loc)
    return {"specs_path": str(en_path), "localized_specs_path": localized_path}


def refresh_research(workspace_root: Path | str, settings: Any | None = None, **kwargs: Any) -> dict[str, Any]:
    """Force regeneration helper (delegates to onboarding.run_onboarding)."""
    from .onboarding import run_onboarding

    return run_onboarding(workspace_root, settings=settings, refresh=True, **kwargs)


__all__ = [
    "context_digest_path",
    "load_research_context",
    "project_specs_path",
    "read_research_state",
    "refresh_research",
    "render_context_digest",
    "render_project_specs_markdown",
    "research_dir",
    "research_report_path",
    "research_state_path",
    "write_project_research",
    "write_research_report",
    "write_research_state",
]
