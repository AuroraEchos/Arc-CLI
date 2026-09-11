"""Test configuration precedence, secret separation, and workspace trust layers."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arc_cli.config import load_config
from arc_cli.profiles import (
    Profile,
    build_system_prompt,
    default_user_instructions_path,
    global_user_context,
    load_user_instructions,
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
        self.assertEqual(config.secrets.api_key, "env-secret")
        self.assertNotIn("env-secret", repr(config))

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

    def test_project_dotenv_is_never_loaded(self) -> None:
        (self.cwd / ".env").write_text("ARC_MODEL=dotenv\nARC_API_KEY=dotenv-secret\n")
        with patch.dict(os.environ, {"UNCHANGED": "yes"}, clear=True):
            before = dict(os.environ)
            config = load_config(self.cwd, user_config_path=self.user_config)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(config.provider.model, "")
        self.assertEqual(config.secrets.api_key, "")

    def test_toml_rejects_secrets(self) -> None:
        (self.cwd / ".arc").mkdir()
        (self.cwd / ".arc" / "config.toml").write_text('[provider]\napi_key = "committed"\n')
        with self.assertRaisesRegex(ValueError, "Secrets are not allowed"):
            load_config(self.cwd, user_config_path=self.user_config)

    def test_instruction_layers_are_explicit_and_profile_independent(self) -> None:
        user_path = self.cwd / "config" / "arc" / "AGENTS.md"
        user_path.parent.mkdir(parents=True)
        user_path.write_text("User preference")
        (self.cwd / "ARC.md").write_text("Arc framework documentation")
        (self.cwd / "AGENTS.md").write_text("Project guidance")
        unrelated_profile = Profile("reviewer", "Review changes.", ("read",))

        user_preferences = load_user_instructions(path=user_path)
        workspace = load_workspace_instructions(self.cwd)
        prompt = build_system_prompt(
            cwd=self.cwd,
            profile=unrelated_profile,
        )
        self.assertIn("CORE POLICY", prompt)
        self.assertNotIn("User preference", prompt)
        self.assertNotIn("Arc framework documentation", prompt)
        self.assertIn("cannot change Runtime security rules", prompt)
        self.assertEqual([item.source for item in workspace], ["AGENTS.md"])

        global_context = global_user_context(user_preferences)
        self.assertEqual(global_context[0].role, "user")
        self.assertIn("USER GLOBAL PREFERENCES", global_context[0].content)
        self.assertIn("User preference", global_context[0].content)

        context = workspace_context(workspace)
        self.assertEqual([message.role for message in context], ["user"])
        self.assertIn("PROJECT WORKSPACE INSTRUCTIONS — untrusted, AGENTS.md", context[0].content)
        self.assertIn("Project guidance", context[0].content)
        self.assertNotIn("Arc framework documentation", context[0].content)

        invocation = user_context("Run-specific preference")
        self.assertEqual(invocation[0].role, "user")
        self.assertIn("Run-specific preference", invocation[0].content)

    def test_user_instruction_path_honors_xdg_config_home(self) -> None:
        self.assertEqual(
            default_user_instructions_path({"XDG_CONFIG_HOME": str(self.cwd)}),
            self.cwd / "arc" / "AGENTS.md",
        )

    def test_workspace_context_can_be_disabled_without_disabling_user_preferences(self) -> None:
        user_path = self.cwd / "user-agents.md"
        user_path.write_text("Cross-project preference")
        (self.cwd / "AGENTS.md").write_text("Project guidance")

        self.assertEqual(load_workspace_instructions(self.cwd, enabled=False), ())
        self.assertEqual(load_user_instructions(path=user_path)[0].content, "Cross-project preference")

    def test_instruction_files_are_size_bounded(self) -> None:
        user_path = self.cwd / "user-agents.md"
        user_path.write_text("four")
        (self.cwd / "AGENTS.md").write_text("four")

        with self.assertRaisesRegex(ValueError, "exceeds 3 bytes"):
            load_user_instructions(path=user_path, max_bytes=3)
        with self.assertRaisesRegex(ValueError, "use --no-context"):
            load_workspace_instructions(self.cwd, max_bytes=3)


if __name__ == "__main__":
    unittest.main()
