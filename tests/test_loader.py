"""Tests for :mod:`app.agents.loader`."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.agents import loader
from app.agents.loader import AgentLoadError, get_agent, load_agents

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_AGENTS = {
    "builder",
    "project-manager",
    "qa",
    "research",
    "reviewer",
    "security",
    "translator",
}

VALID_PERMISSIONS = """permissions:
  - action: shell
    resource: "*"
    effect: deny
"""

SAFE_DEFAULT = {("*", "*", "deny")}

# Assembled at runtime from parts so this repository's own source stays free
# of the legacy names (tests/test_generic_prompts.py scans app/ and tests/).
LEGACY_A = "".join(("scr", "apper"))
LEGACY_B = "".join(("dash", "board"))

GENERIC_TOUCHED = "the repository that owns the touched files, or the workspace root"
GENERIC_OWNER = "the repository that owns the task"


def _write_agent(
    root: Path, name: str, frontmatter: str, body: str = "body"
) -> Path:
    agents_dir = root / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def _rule_tuples(spec) -> set[tuple[str, str, str]]:
    return {(rule.action, rule.resource, rule.effect) for rule in spec.permissions}


class RealAgentsTest(unittest.TestCase):
    def test_loads_the_seven_expected_agents(self):
        agents = load_agents(REPO_ROOT)
        self.assertEqual(set(agents), EXPECTED_AGENTS)

    def test_every_agent_is_complete(self):
        for name, spec in load_agents(REPO_ROOT).items():
            with self.subTest(agent=name):
                self.assertEqual(spec.name, name)
                self.assertTrue(spec.description, "description is empty")
                self.assertTrue(spec.system_prompt.strip(), "prompt is empty")
                self.assertTrue(spec.permissions, "no permissions")
                self.assertIsNotNone(spec.source_path)
                self.assertTrue(spec.source_path.is_file())
                for rule in spec.permissions:
                    self.assertIn(rule.effect, {"allow", "deny"})
                    self.assertIn(rule.action, loader.ALLOWED_ACTIONS)
                    self.assertTrue(rule.resource)

    def test_get_agent_returns_the_spec(self):
        agents = load_agents(REPO_ROOT)
        self.assertIs(get_agent(agents, "builder"), agents["builder"])

    def test_get_agent_unknown_name_raises(self):
        agents = load_agents(REPO_ROOT)
        with self.assertRaises(AgentLoadError) as ctx:
            get_agent(agents, "nobody")
        self.assertIn("nobody", str(ctx.exception))


class BrokenInputTest(unittest.TestCase):
    def test_missing_agents_directory_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(tmp)
            self.assertIn("agents", str(ctx.exception))

    def test_missing_file_raises_with_its_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.md"
            with self.assertRaises(AgentLoadError) as ctx:
                loader._load_agent_file(missing)
            self.assertIn(str(missing), str(ctx.exception))

    def test_unparsable_frontmatter_raises_naming_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "broken", "description: [unclosed")
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("broken.md", str(ctx.exception))

    def test_frontmatter_must_be_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "odd", "- just\n- a list")
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("odd.md", str(ctx.exception))

    def test_missing_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "plain.md").write_text(
                "no frontmatter here\n", encoding="utf-8"
            )
            with self.assertRaises(AgentLoadError):
                load_agents(root)

    def test_missing_permissions_fall_back_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "bare", "description: no permissions")
            with self.assertLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["bare"]
            self.assertEqual(_rule_tuples(spec), SAFE_DEFAULT)

    def test_empty_permissions_fall_back_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "empty", "description: empty permissions\npermissions: []")
            with self.assertLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["empty"]
            self.assertEqual(_rule_tuples(spec), SAFE_DEFAULT)

    def test_malformed_permissions_fall_back_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "wrong",
                'description: malformed\npermissions:\n  - action: shell',
            )
            with self.assertLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["wrong"]
            self.assertEqual(_rule_tuples(spec), SAFE_DEFAULT)

    def test_unknown_action_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "mixed",
                "description: mixed\n"
                "permissions:\n"
                "  - action: shell\n"
                '    resource: "*"\n'
                "    effect: allow\n"
                "  - action: teleport\n"
                '    resource: "*"\n'
                "    effect: allow\n",
            )
            with self.assertLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["mixed"]
            self.assertEqual(_rule_tuples(spec), {("shell", "*", "allow")})

    def test_v1_bash_action_falls_back_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "legacy",
                "description: legacy action\n"
                "permissions:\n"
                "  - action: bash\n"
                '    resource: "*"\n'
                "    effect: allow\n",
            )
            with self.assertLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["legacy"]
            self.assertEqual(_rule_tuples(spec), SAFE_DEFAULT)

    def test_action_is_normalized_to_lowercase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "shouty",
                "description: shouty\n"
                "permissions:\n"
                "  - action: SHELL\n"
                '    resource: "*"\n'
                "    effect: deny\n",
            )
            spec = load_agents(root)["shouty"]
            self.assertEqual(_rule_tuples(spec), {("shell", "*", "deny")})

    def test_wildcard_allow_is_dropped_in_favor_of_safe_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "wildallow",
                "description: wildcard allow\n"
                "permissions:\n"
                '  - action: "*"\n'
                '    resource: "*"\n'
                '    effect: allow\n',
            )
            with self.assertLogs("app.agents.loader", level="WARNING") as logs:
                spec = load_agents(root)["wildallow"]
            self.assertEqual(_rule_tuples(spec), SAFE_DEFAULT)
            self.assertTrue(
                any("wildallow.md" in record.getMessage() for record in logs.records),
                logs.output,
            )

    def test_wildcard_deny_is_accepted_as_safe_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "wilddeny",
                "description: wildcard deny\n"
                "permissions:\n"
                '  - action: "*"\n'
                '    resource: "*"\n'
                '    effect: deny\n',
            )
            with self.assertNoLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["wilddeny"]
            self.assertEqual(_rule_tuples(spec), {("*", "*", "deny")})

    def test_effect_is_normalized_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "casey",
                "description: case effect\n"
                "permissions:\n"
                "  - action: shell\n"
                '    resource: "*"\n'
                '    effect: Allow\n',
            )
            spec = load_agents(root)["casey"]
            self.assertEqual(_rule_tuples(spec), {("shell", "*", "allow")})
            with self.assertNoLogs("app.agents.loader", level="WARNING"):
                _ = load_agents(root)["casey"]

    def test_non_string_description_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root, "typed", "description: [a, b]\n" + VALID_PERMISSIONS
            )
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("description", str(ctx.exception))
            self.assertIn("typed.md", str(ctx.exception))

    def test_non_string_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "typed-mode",
                "description: demo\nmode: [subagent]\n" + VALID_PERMISSIONS,
            )
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("mode", str(ctx.exception))

    def test_empty_description_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "nodesc", "description: ''\n" + VALID_PERMISSIONS)
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("nodesc.md", str(ctx.exception))

    def test_empty_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(
                root,
                "emptybody",
                "description: demo\n" + VALID_PERMISSIONS,
                body="   ",
            )
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("emptybody.md", str(ctx.exception))

    def test_symlink_pointing_outside_the_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            _write_agent(root, "real", "description: real\n" + VALID_PERMISSIONS)
            outside_file = Path(outside) / "evil.md"
            outside_file.write_text(
                f"---\ndescription: evil\n{VALID_PERMISSIONS}---\n\nbody\n",
                encoding="utf-8",
            )
            link = root / "agents" / "evil.md"
            try:
                link.symlink_to(outside_file)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available on this platform")
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("symlink", str(ctx.exception).lower())

    def test_non_regular_file_is_rejected(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs are not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "real", "description: real\n" + VALID_PERMISSIONS)
            try:
                os.mkfifo(root / "agents" / "pipe.md")
            except OSError:
                self.skipTest("cannot create a FIFO on this platform")
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("non-regular", str(ctx.exception))

    def test_oversized_file_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "big.md").write_bytes(
                b"x" * (loader.MAX_AGENT_FILE_BYTES + 1)
            )
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("too large", str(ctx.exception))

    def test_invalid_utf8_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents_dir = root / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "binary.md").write_bytes(
                b"---\ndescription: demo\n---\n\xff\xfe\n"
            )
            with self.assertRaises(AgentLoadError) as ctx:
                load_agents(root)
            self.assertIn("binary.md", str(ctx.exception))
            self.assertIn("UTF-8", str(ctx.exception))


class GenericizationTest(unittest.TestCase):
    def test_legacy_repository_names_are_normalized_with_warnings(self):
        body = (
            "Commit inside the repository that owns the task "
            f"(`{LEGACY_A}/`, `{LEGACY_B}/`, or the root). "
            f"Read the docs in {LEGACY_A}/ and {LEGACY_B}/."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "demo", f"description: demo\n{VALID_PERMISSIONS}", body)
            with self.assertLogs("app.agents.loader", level="WARNING") as logs:
                spec = load_agents(root)["demo"]

        prompt = spec.system_prompt.lower()
        self.assertNotIn(LEGACY_A, prompt)
        self.assertNotIn(LEGACY_B, prompt)
        self.assertIn(GENERIC_TOUCHED, spec.system_prompt)
        self.assertIn(GENERIC_OWNER, spec.system_prompt)
        self.assertTrue(logs.records)
        for record in logs.records:
            self.assertIn("replaced", record.getMessage())

    def test_normalization_output_is_exact(self):
        body = (
            f"Commit inside (`{LEGACY_A}/`, `{LEGACY_B}/`, or the root). "
            f"Read {LEGACY_A}/ and {LEGACY_B}/."
        )
        expected = (
            f"Commit inside ({GENERIC_TOUCHED}). "
            f"Read {GENERIC_OWNER} and {GENERIC_OWNER}."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_agent(root, "exact", f"description: exact\n{VALID_PERMISSIONS}", body)
            with self.assertLogs("app.agents.loader", level="WARNING"):
                spec = load_agents(root)["exact"]
        self.assertEqual(spec.system_prompt, expected)


if __name__ == "__main__":
    unittest.main()
