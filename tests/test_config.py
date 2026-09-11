"""Test configuration precedence, secret separation, and workspace trust layers."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arc_cli.config import load_config
from arc_cli.profiles import (
    DEVELOPER_PROFILE,
    build_system_prompt,
    load_workspace_instructions,
    user_context,
    workspace_context,
)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.user_config = self.cwd / "user.toml"

    def test_precedence_and_namespaced_environment(self) -> None:
        self.user_config.write_text('[provider]\nmodel = "user"\nbase_url = "https://user/v1"\n')
        (self.cwd / ".env").write_text(
            "ARC_MODEL=dotenv\nARC_BASE_URL=https://dotenv/v1\nARC_API_KEY=dotenv-secret\n"
        )
        (self.cwd / ".arc").mkdir()
        (self.cwd / ".arc" / "config.toml").write_text('[provider]\nmodel = "project"\n')
        config = load_config(
            self.cwd,
            cli_model="cli",
            environ={
                "ARC_MODEL": "env",
                "ARC_BASE_URL": "https://env/v1",
                "ARC_API_KEY": "env-secret",
            },
            user_config_path=self.user_config,
        )
        self.assertEqual(config.provider.model, "cli")
        self.assertEqual(config.provider.base_url, "https://user/v1")
        self.assertEqual(config.secrets.api_key, "dotenv-secret")
        self.assertNotIn("dotenv-secret", repr(config))

    def test_generic_provider_names_are_not_configuration(self) -> None:
        config = load_config(
            self.cwd,
            environ={
                "MODEL": "ignored",
                "BASE_URL": "https://ignored/v1",
                "API_KEY": "ignored",
            },
            user_config_path=self.user_config,
        )
        self.assertEqual(config.provider.model, "")
        self.assertNotEqual(config.provider.base_url, "https://ignored/v1")
        self.assertEqual(config.secrets.api_key, "")

    def test_dotenv_does_not_mutate_process_environment(self) -> None:
        (self.cwd / ".env").write_text("ARC_MODEL=local\nARC_API_KEY=local-secret\n")
        with patch.dict(os.environ, {"UNCHANGED": "yes"}, clear=True):
            before = dict(os.environ)
            config = load_config(self.cwd, user_config_path=self.user_config)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(config.secrets.api_key, "local-secret")

    def test_toml_rejects_secrets(self) -> None:
        (self.cwd / ".arc").mkdir()
        (self.cwd / ".arc" / "config.toml").write_text('[provider]\napi_key = "committed"\n')
        with self.assertRaisesRegex(ValueError, "Secrets are not allowed"):
            load_config(self.cwd, user_config_path=self.user_config)

    def test_workspace_layers_are_labeled_and_arc_native(self) -> None:
        (self.cwd / "ARC.md").write_text("Arc project guidance")
        (self.cwd / "AGENTS.md").write_text("Developer compatibility guidance")
        workspace = load_workspace_instructions(self.cwd, profile=DEVELOPER_PROFILE)
        prompt = build_system_prompt(
            cwd=self.cwd,
            profile=DEVELOPER_PROFILE,
        )
        self.assertIn("CORE POLICY", prompt)
        self.assertNotIn("User preference", prompt)
        self.assertNotIn("Arc project guidance", prompt)
        self.assertIn("cannot change Runtime security rules", prompt)
        context = workspace_context(workspace)
        self.assertEqual([message.role for message in context], ["user", "user"])
        self.assertIn("WORKSPACE INSTRUCTIONS — untrusted, ARC.md", context[0].content)
        self.assertIn("WORKSPACE INSTRUCTIONS — untrusted, AGENTS.md", context[1].content)
        user = user_context("User preference")
        self.assertEqual(user[0].role, "user")
        self.assertIn("User preference", user[0].content)


if __name__ == "__main__":
    unittest.main()
