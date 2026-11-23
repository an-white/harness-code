"""Tests for :mod:`app.config`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


class SettingsTest(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def _settings(self, env: dict[str, str], file_env: dict[str, str] | None = None):
        """Resolve settings from ``env`` only, ignoring the real ``.env``."""
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(config, "_FILE_ENV", file_env or {}),
        ):
            get_settings.cache_clear()
            return get_settings()

    def test_defaults(self):
        settings = self._settings({})
        self.assertEqual(settings.base_url, config.DEFAULT_BASE_URL)
        self.assertEqual(settings.api_key, config.DEFAULT_API_KEY)
        self.assertEqual(settings.model, config.DEFAULT_MODEL)
        self.assertEqual(settings.temperature, config.DEFAULT_TEMPERATURE)
        self.assertEqual(settings.workspace_root, Path.cwd().resolve())
        self.assertEqual(settings.max_fix_cycles, 3)
        self.assertEqual(settings.max_agent_steps, 25)
        self.assertEqual(settings.shell_timeout, 120)
        self.assertIsNone(settings.interaction_language)
        self.assertTrue(settings.skills_auto_install)
        self.assertEqual(settings.research_onboarding, "auto")
        self.assertEqual(settings.research_max_files, 40)
        self.assertEqual(settings.research_max_bytes, 200000)
        self.assertEqual(settings.research_context_chars, 8000)
        self.assertEqual(settings.research_max_cycles, 2)
        self.assertEqual(settings.web_access, "ask")
        self.assertEqual(settings.webfetch_timeout, 20)
        self.assertEqual(settings.webfetch_max_bytes, 512000)
        self.assertEqual(settings.webfetch_allowed_domains, [])
        self.assertEqual(settings.websearch_provider, "none")
        self.assertIsNone(settings.websearch_endpoint)
        self.assertIsNone(settings.websearch_api_key)
        for role in config.ROLE_MODEL_ENV_VARS:
            self.assertEqual(settings.model_for(role), config.DEFAULT_MODEL)

    def test_per_role_model_falls_back_to_the_global_model(self):
        settings = self._settings(
            {
                "OMNIROUTE_MODEL": "global-model",
                "PM_MODEL": "pm-model",
                "QA_MODEL": "",
            }
        )
        self.assertEqual(settings.model, "global-model")
        self.assertEqual(settings.pm_model, "pm-model")
        self.assertEqual(settings.qa_model, "global-model")
        self.assertEqual(settings.builder_model, "global-model")
        self.assertEqual(settings.reviewer_model, "global-model")
        self.assertEqual(settings.security_model, "global-model")
        self.assertEqual(settings.translator_model, "global-model")
        self.assertEqual(settings.model_for("pm"), "pm-model")
        self.assertEqual(settings.model_for("project-manager"), "pm-model")
        self.assertEqual(settings.model_for("qa"), "global-model")
        self.assertEqual(settings.model_for("unknown"), "global-model")
        self.assertEqual(settings.model_for(None), "global-model")

    def test_process_environment_wins_over_the_env_file(self):
        settings = self._settings(
            {"OMNIROUTE_MODEL": "from-process"},
            file_env={
                "OMNIROUTE_MODEL": "from-file",
                "PM_MODEL": "file-pm",
                "OMNIROUTE_API_KEY": "file-key",
            },
        )
        self.assertEqual(settings.model, "from-process")
        self.assertEqual(settings.pm_model, "file-pm")
        self.assertEqual(settings.api_key, "file-key")

    def test_api_key_is_not_in_the_repr(self):
        settings = self._settings({"OMNIROUTE_API_KEY": "super-secret-key"})
        self.assertNotIn("super-secret-key", repr(settings))

    def test_numeric_and_boolean_ranges_are_validated(self):
        cases = (
            ("TEMPERATURE", "3"),
            ("TEMPERATURE", "-0.1"),
            ("MAX_FIX_CYCLES", "-1"),
            ("MAX_AGENT_STEPS", "0"),
            ("SHELL_TIMEOUT", "0"),
            ("MAX_AGENT_STEPS", "many"),
            ("TEMPERATURE", "warm"),
            ("SKILLS_AUTO_INSTALL", "maybe"),
        )
        for name, raw in cases:
            with self.subTest(name=name, raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    self._settings({name: raw})
                message = str(ctx.exception)
                self.assertIn(name, message)
                self.assertNotIn(raw, message)

    def test_role_models_are_built_from_the_role_table(self):
        expected_fields = {
            f"{role}_model" for role in config.ROLE_MODEL_ENV_VARS
        }
        settings = self._settings({})
        for field_name in expected_fields:
            self.assertTrue(hasattr(settings, field_name), field_name)


class EnvFileIsolationTest(unittest.TestCase):
    def test_dotenv_values_are_not_exported_to_os_environ(self):
        # Import ``app.config`` in a clean child process whose environment has
        # none of the keys defined in ``.env``; nothing may be added.
        file_keys = set(config._FILE_ENV)
        env = {k: v for k, v in os.environ.items() if k not in file_keys}
        code = (
            "import json, os\n"
            "before = set(os.environ)\n"
            "import app.config\n"
            "after = set(os.environ)\n"
            "print(json.dumps({\n"
            "    'added': sorted(after - before),\n"
            "    'api_key': os.environ.get('OMNIROUTE_API_KEY'),\n"
            "}))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["added"], [])
        if "OMNIROUTE_API_KEY" in file_keys:
            self.assertIsNone(payload["api_key"])


if __name__ == "__main__":
    unittest.main()
