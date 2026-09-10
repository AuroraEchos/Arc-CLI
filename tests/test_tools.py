"""测试 Arc 内置工具的校验、文件操作和进程清理。"""

import asyncio
import os
import shlex
import tempfile
import unittest
from pathlib import Path

from arc_cli.tools import MAX_OUTPUT, ToolContext, ToolRegistry, create_builtin_tools
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

    async def test_schema_validation(self):
        for name, args in [
            ("read", {"path": "a", "offset": 0}),
            ("read", {"path": "a", "offset": True}),
            ("write", {"path": "a"}),
            ("bash", {"command": "true", "timeout": 0}),
            ("edit", {"path": "a", "old_text": "", "new_text": "b"}),
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

    async def test_bash_bounded_output(self):
        result = await self.execute("bash", command="head -c 50000 /dev/zero | tr '\\0' x")
        self.assertFalse(result.is_error)
        self.assertIn("truncated", result.content)
        self.assertLess(len(result.content), MAX_OUTPUT + 100)
        self.assertEqual(len("".join(self.updates)), MAX_OUTPUT)

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
