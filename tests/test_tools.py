"""测试 Arc 内置工具的校验、文件操作和进程清理。"""

import asyncio
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import arc_cli.tools as tools_module
from arc_cli.policy import ExecutionPolicy
from arc_cli.tools import (
    MAX_OUTPUT,
    ToolContext,
    ToolRegistry,
    create_builtin_tools,
    sanitized_subprocess_env,
)
from arc_cli.types import ToolCall


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.updates = []

        async def emit(text):
            self.updates.append(text)

        self.context = ToolContext(self.cwd, emit)
        self.registry = ToolRegistry(create_builtin_tools())

    async def execute(self, name, **args):
        return await self.registry.execute(ToolCall("test", name, args), self.context)

    async def test_write_read_edit(self):
        result = await self.execute("write", path="nested/a.txt", content="你好\nworld\n")
        self.assertFalse(result.is_error)
        result = await self.execute("read", path="nested/a.txt", offset=2, limit=1)
        self.assertEqual(result.content, "2: world")
        await self.execute("edit", path="nested/a.txt", old_text="world", new_text="Arc CLI")
        self.assertEqual((self.cwd / "nested/a.txt").read_text(), "你好\nArc CLI\n")

    async def test_exact_edit_ambiguity_and_permissions(self):
        path = self.cwd / "a"
        path.write_bytes(b"a\r\na\r\n")
        path.chmod(0o640)
        result = await self.execute("edit", path="a", old_text="a", new_text="b")
        self.assertTrue(result.is_error)
        self.assertEqual(path.read_bytes(), b"a\r\na\r\n")
        result = await self.execute("edit", path="a", old_text="a", new_text="b", replace_all=True)
        self.assertFalse(result.is_error)
        self.assertEqual(path.read_bytes(), b"b\r\nb\r\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    async def test_apply_patch_adds_updates_and_deletes_multiple_files(self):
        source = self.cwd / "source.py"
        source.write_text("alpha\nbeta\n")
        source.chmod(0o640)
        obsolete = self.cwd / "obsolete.py"
        obsolete.write_text("remove me\n")

        result = await self.execute(
            "apply_patch",
            operations=[
                {"type": "add", "path": "new.py", "content": "created\n"},
                {
                    "type": "update",
                    "path": "source.py",
                    "edits": [
                        {"old_text": "alpha", "new_text": "first"},
                        {"old_text": "beta", "new_text": "second"},
                    ],
                },
                {"type": "delete", "path": "obsolete.py"},
            ],
        )

        self.assertFalse(result.is_error)
        self.assertEqual((self.cwd / "new.py").read_text(), "created\n")
        self.assertEqual(source.read_text(), "first\nsecond\n")
        self.assertEqual(source.stat().st_mode & 0o777, 0o640)
        self.assertFalse(obsolete.exists())
        self.assertIn("Applied 3 file operations", result.content)

    async def test_apply_patch_preflight_failure_changes_nothing(self):
        source = self.cwd / "source.py"
        source.write_text("original\n")

        result = await self.execute(
            "apply_patch",
            operations=[
                {"type": "add", "path": "new.py", "content": "created\n"},
                {
                    "type": "update",
                    "path": "source.py",
                    "edits": [{"old_text": "missing", "new_text": "replacement"}],
                },
            ],
        )

        self.assertTrue(result.is_error)
        self.assertFalse((self.cwd / "new.py").exists())
        self.assertEqual(source.read_text(), "original\n")

    async def test_apply_patch_rejects_duplicate_and_ambiguous_targets(self):
        source = self.cwd / "source.py"
        source.write_text("same same\n")
        duplicate = await self.execute(
            "apply_patch",
            operations=[
                {"type": "delete", "path": "source.py"},
                {"type": "add", "path": "source.py", "content": "replacement\n"},
            ],
        )
        ambiguous = await self.execute(
            "apply_patch",
            operations=[
                {
                    "type": "update",
                    "path": "source.py",
                    "edits": [{"old_text": "same", "new_text": "changed"}],
                }
            ],
        )

        self.assertTrue(duplicate.is_error)
        self.assertTrue(ambiguous.is_error)
        self.assertEqual(source.read_text(), "same same\n")

    async def test_apply_patch_rolls_back_after_commit_failure(self):
        source = self.cwd / "source.py"
        source.write_text("original\n")
        real_atomic_write = tools_module._atomic_write
        calls = 0

        def fail_second_write(path, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated write failure")
            real_atomic_write(path, content)

        with patch("arc_cli.tools._atomic_write", side_effect=fail_second_write):
            result = await self.execute(
                "apply_patch",
                operations=[
                    {"type": "add", "path": "new.py", "content": "created\n"},
                    {
                        "type": "update",
                        "path": "source.py",
                        "edits": [{"old_text": "original", "new_text": "updated"}],
                    },
                ],
            )

        self.assertTrue(result.is_error)
        self.assertFalse((self.cwd / "new.py").exists())
        self.assertEqual(source.read_text(), "original\n")

    def test_apply_patch_effects_include_destructive_for_delete(self):
        tool = self.registry.validate(
            ToolCall(
                "effects",
                "apply_patch",
                {"operations": [{"type": "add", "path": "new.py", "content": "new"}]},
            )
        )
        self.assertEqual(tool.effects_for({"operations": [{"type": "add"}]}), ("write",))
        self.assertEqual(tool.effects_for({"operations": [{"type": "delete"}]}), ("write", "destructive"))

    async def test_apply_patch_delete_requires_authorization_in_restricted_mode(self):
        call = ToolCall(
            "delete",
            "apply_patch",
            {"operations": [{"type": "delete", "path": "obsolete.py"}]},
        )
        tool = self.registry.validate(call)
        decision = await ExecutionPolicy.restricted().authorize(call, tool, self.context)

        self.assertEqual(decision.status, "confirmation_required")
        self.assertEqual(decision.effects, ("write", "destructive"))

    async def test_schema_validation(self):
        for name, args in [
            ("read", {"path": "a", "offset": 0}),
            ("read", {"path": "a", "offset": True}),
            ("write", {"path": "a"}),
            ("bash", {"command": "true", "timeout": 0}),
            ("edit", {"path": "a", "old_text": "", "new_text": "b"}),
            ("apply_patch", {"operations": []}),
            (
                "apply_patch",
                {
                    "operations": [
                        {
                            "type": "update",
                            "path": "a",
                            "edits": [{"old_text": "", "new_text": "b"}],
                        }
                    ]
                },
            ),
            ("unknown", {}),
        ]:
            with self.subTest(name=name, args=args):
                result = await self.execute(name, **args)
                self.assertTrue(result.is_error)
        self.assertFalse((self.cwd / "a").exists())

    async def test_binary_large_and_missing_files(self):
        (self.cwd / "binary").write_bytes(b"\x00\x01")
        (self.cwd / "large").write_bytes(b"x" * (1024 * 1024 + 1))
        for name in ("binary", "large", "missing"):
            result = await self.execute("read", path=name)
            self.assertTrue(result.is_error)

    async def test_read_truncation(self):
        (self.cwd / "long").write_text("x" * (MAX_OUTPUT * 2))
        result = await self.execute("read", path="long")
        self.assertIn("truncated", result.content)
        self.assertLess(len(result.content), MAX_OUTPUT + 100)

    async def test_bash_cwd_exit_and_stream(self):
        result = await self.execute("bash", command="pwd; printf 'hello'; exit 7")
        self.assertTrue(result.is_error)
        self.assertIn(str(self.cwd), result.content)
        self.assertIn("Exit code: 7", result.content)
        self.assertIn("hello", "".join(self.updates))

    async def test_bash_never_inherits_provider_or_ambient_secrets(self):
        context = ToolContext(
            self.cwd,
            self.context.emit,
            {
                "PATH": os.environ.get("PATH", ""),
                "ARC_API_KEY": "arc-secret",
                "ARC_BASE_URL": "https://provider.invalid/v1",
                "OTHER_TOKEN": "ambient-secret",
                "SAFE_VALUE": "visible",
            },
        )
        result = await self.registry.execute(
            ToolCall(
                "env",
                "bash",
                {
                    "command": "printf '%s|%s|%s|%s' \"${ARC_API_KEY-missing}\" "
                    '"${ARC_BASE_URL-missing}" "${OTHER_TOKEN-missing}" '
                    '"${SAFE_VALUE-missing}"'
                },
            ),
            context,
        )
        self.assertEqual(result.content.splitlines()[0], "missing|missing|missing|visible")
        self.assertNotIn("secret", result.content)

    def test_sensitive_allowlist_cannot_reenable_provider_names(self):
        env = sanitized_subprocess_env(
            {"ARC_API_KEY": "no", "OTHER_TOKEN": "yes"},
            allow_sensitive=("ARC_API_KEY", "OTHER_TOKEN"),
        )
        self.assertNotIn("ARC_API_KEY", env)
        self.assertEqual(env["OTHER_TOKEN"], "yes")

    async def test_bash_sensitive_allowlist_is_explicit_and_provider_safe(self):
        context = ToolContext(
            self.cwd,
            self.context.emit,
            {"ARC_API_KEY": "never", "OTHER_TOKEN": "visible"},
            ("ARC_API_KEY", "OTHER_TOKEN"),
        )
        result = await self.registry.execute(
            ToolCall(
                "env-allow",
                "bash",
                {"command": 'printf \'%s|%s\' "${ARC_API_KEY-missing}" "${OTHER_TOKEN-missing}"'},
            ),
            context,
        )
        self.assertEqual(result.content.splitlines()[0], "missing|visible")

    async def test_bash_bounded_output(self):
        result = await self.execute("bash", command="head -c 50000 /dev/zero | tr '\\0' x")
        self.assertFalse(result.is_error)
        self.assertIn("truncated", result.content)
        self.assertLess(len(result.content), MAX_OUTPUT + 100)
        self.assertEqual(len("".join(self.updates)), MAX_OUTPUT)

    def test_bash_effects_distinguish_commands_from_path_names(self):
        tool = self.registry.validate(ToolCall("effects", "bash", {"command": "true"}))
        self.assertEqual(
            tool.effects_for(
                {"command": "cat ~/.ssh/config 2>/dev/null; echo marker; ls -la ~/.ssh/ 2>/dev/null"}
            ),
            ("process",),
        )
        self.assertEqual(tool.effects_for({"command": "ssh host"}), ("process", "external"))
        self.assertEqual(
            tool.effects_for({"command": "printf done && git fetch origin"}),
            ("process", "external"),
        )
        self.assertEqual(tool.effects_for({"command": "rm obsolete.txt"}), ("process", "destructive"))

    async def test_timeout_kills_descendants(self):
        marker = self.cwd / "should-not-exist"
        command = f"(sleep 0.3; touch {shlex.quote(str(marker))}) & wait"
        result = await self.execute("bash", command=command, timeout=0.05)
        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.content)
        await asyncio.sleep(0.4)
        self.assertFalse(marker.exists())

    async def test_cancellation_kills_process_group(self):
        started = asyncio.Event()

        async def emit(text):
            started.set()

        marker = self.cwd / "should-not-exist"
        command = f"(sleep 0.3; touch {shlex.quote(str(marker))}) & echo ready; wait"
        context = ToolContext(self.cwd, emit)
        task = asyncio.create_task(
            self.registry.execute(ToolCall("c", "bash", {"command": command}), context)
        )
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.4)
        self.assertFalse(marker.exists())

    async def test_absolute_paths_allowed_not_a_sandbox(self):
        path = self.cwd / "absolute"
        await self.execute("write", path=os.fspath(path), content="ok")
        self.assertEqual(path.read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
