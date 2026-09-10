"""测试 Arc CLI 的命令行参数、配置加载和退出状态。"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        (self.cwd / "README.md").write_text("# Test project\nHello\n")
        self.env = {
            key: value for key, value in os.environ.items() if key not in ("MODEL", "API_KEY", "BASE_URL")
        }
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

    def test_missing_model_and_invalid_tools(self):
        result = self.cli("--no-session", "-p", "hi")
        self.assertEqual(result.returncode, 2)
        self.assertIn("MODEL", result.stderr)
        result = self.cli("--tools", "bogus", "-p", "hi")
        self.assertEqual(result.returncode, 2)

    def test_dotenv_configuration_is_loaded(self):
        (self.cwd / ".env").write_text("MODEL=test\nBASE_URL=not-a-url\nAPI_KEY=secret\n")
        result = self.cli("--no-session", "-p", "hi")
        self.assertEqual(result.returncode, 2)
        self.assertIn("base_url", result.stderr)


if __name__ == "__main__":
    unittest.main()
