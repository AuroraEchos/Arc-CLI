"""测试 Arc CLI 的命令行参数、配置加载和退出状态。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arc_cli.agent import DEFAULT_SYSTEM_PROMPT
from arc_cli.cli import parser


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        (self.cwd / "README.md").write_text("# Test project\nHello\n")
        provider_names = {
            "ARC_MODEL",
            "ARC_API_KEY",
            "ARC_BASE_URL",
        }
        self.env = {key: value for key, value in os.environ.items() if key not in provider_names}
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def cli(self, *args, input=""):
        return subprocess.run(
            [sys.executable, "-m", "arc_cli", "--cwd", str(self.cwd), *args],
            input=input,
            text=True,
            capture_output=True,
            timeout=10,
            env=self.env,
        )

    def test_help_and_version(self):
        self.assertEqual(self.cli("--help").returncode, 0)
        self.assertIn("0.1.0", self.cli("--version").stdout)
        self.assertIn("--no-color", self.cli("--help").stdout)
        self.assertIn("--no-markdown", self.cli("--help").stdout)
        self.assertIn("--policy {autonomous,restricted}", self.cli("--help").stdout)
        self.assertIn("--instructions", self.cli("--help").stdout)
        self.assertNotIn("--allow-external", self.cli("--help").stdout)

    def test_system_remains_an_alias_for_run_instructions(self):
        self.assertEqual(parser().parse_args(["--system", "legacy"]).instructions, "legacy")
        self.assertEqual(parser().parse_args(["--instructions", "current"]).instructions, "current")

    def test_default_prompt_is_a_general_assistant_with_discretionary_tools(self):
        prompt = DEFAULT_SYSTEM_PROMPT.lower()
        self.assertIn("assistant running inside the arc runtime", prompt)
        self.assertIn("decide whether tools are necessary", prompt)
        self.assertIn("general knowledge directly", prompt)
        self.assertIn("runtime authorization is authoritative", prompt)
        self.assertNotIn("coding agent", prompt)

    def test_policy_defaults_to_autonomous_and_can_be_restricted(self):
        self.assertEqual(parser().parse_args([]).policy, "autonomous")
        self.assertEqual(parser().parse_args(["--policy", "restricted"]).policy, "restricted")

    def test_missing_model_and_invalid_tools(self):
        result = self.cli("--no-session", "-p", "hi")
        self.assertEqual(result.returncode, 2)
        self.assertIn("MODEL", result.stderr)
        result = self.cli("--tools", "bogus", "-p", "hi")
        self.assertEqual(result.returncode, 2)

    def test_dotenv_configuration_is_loaded(self):
        (self.cwd / ".env").write_text("ARC_MODEL=test\nARC_BASE_URL=not-a-url\nARC_API_KEY=secret\n")
        result = self.cli("--no-session", "-p", "hi")
        self.assertEqual(result.returncode, 2)
        self.assertIn("base_url", result.stderr)


if __name__ == "__main__":
    unittest.main()
