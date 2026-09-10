"""测试 Arc CLI 终端渲染、状态摘要与动态活动提示。"""

import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arc_cli.agent import Arc
from arc_cli.providers import FakeProvider
from arc_cli.runtime import ArcSession
from arc_cli.session import SessionStore
from arc_cli.terminal import BANNER, Renderer
from arc_cli.tools import ToolRegistry, create_builtin_tools
from arc_cli.types import Event, Message, ToolCall


class TtyBuffer(io.StringIO):
    """提供可测试 ANSI 和动态输出的内存 TTY。"""

    def isatty(self) -> bool:
        return True


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)

    def session(self, *messages: Message) -> ArcSession:
        store = SessionStore.create(self.cwd)
        for message in messages:
            store.append(message)
        arc = Arc(FakeProvider([]), ToolRegistry(create_builtin_tools()), cwd=self.cwd)
        return ArcSession(arc, store)

    def test_banner_and_status_are_aligned_and_informative(self) -> None:
        output = io.StringIO()
        session = self.session(Message("user", "inspect this project"))
        renderer = Renderer("text", stdout=output, stderr=output)

        renderer.show_banner(session)
        renderer.show_status(session, busy=False)

        text = output.getvalue()
        self.assertIn(BANNER, text)
        self.assertIn("Arc CLI 0.1.0", text)
        self.assertIn("terminal-native agent runtime", text)
        self.assertIn("model    scripted", text)
        self.assertIn("tools    read write edit bash", text)
        self.assertIn("cwd is not a sandbox", text)
        self.assertIn("messages 1", text)
        self.assertIn("state    idle", text)

    def test_assistant_stream_has_lightweight_identity_in_interactive_mode(self) -> None:
        output = io.StringIO()
        renderer = Renderer("text", stdout=output, stderr=output)
        renderer.emit(Event("message_start", {"role": "assistant"}))
        renderer.emit(Event("message_update", {"delta": "first\nsecond"}))
        renderer.emit(Event("message_end", {"message": {"role": "assistant"}}))
        self.assertEqual(output.getvalue(), "arc │ first\n    │ second\n")

    def test_fragmented_multiline_message_is_written_as_complete_lines(self) -> None:
        output = io.StringIO()
        renderer = Renderer("text", stdout=output, stderr=output)
        renderer.emit(Event("message_start", {"role": "assistant"}))
        for fragment in ("# 上", "海大学", "简介\n\n上海", "大学是", "一所高校。"):
            renderer.emit(Event("message_update", {"delta": fragment}))
        renderer.emit(Event("message_end", {"message": {"role": "assistant"}}))

        self.assertEqual(
            output.getvalue(),
            "arc │ # 上海大学简介\n    │ \n    │ 上海大学是一所高校。\n",
        )

    def test_one_shot_output_remains_script_friendly(self) -> None:
        output = io.StringIO()
        renderer = Renderer("text", interactive=False, stdout=output, stderr=io.StringIO())
        renderer.emit(Event("message_start", {"role": "assistant"}))
        renderer.emit(Event("message_update", {"delta": "answer"}))
        renderer.emit(Event("message_end", {"message": {"role": "assistant"}}))
        self.assertEqual(output.getvalue(), "answer\n")

    def test_one_shot_fragmented_output_preserves_plain_text(self) -> None:
        output = io.StringIO()
        renderer = Renderer("text", interactive=False, stdout=output, stderr=io.StringIO())
        renderer.emit(Event("message_start", {"role": "assistant"}))
        for fragment in ("第一", "行\n第", "二行\n"):
            renderer.emit(Event("message_update", {"delta": fragment}))
        renderer.emit(Event("message_end", {"message": {"role": "assistant"}}))
        self.assertEqual(output.getvalue(), "第一行\n第二行\n")

    def test_tool_output_is_summarized_and_full_result_is_recoverable(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        renderer = Renderer("text", stdout=output, stderr=errors)
        call = {"id": "c1", "name": "bash", "arguments": {"command": "pytest -q"}}
        renderer.emit(Event("tool_execution_start", {"tool_call": call}))
        renderer.emit(Event("tool_execution_update", {"call_id": "c1", "delta": "noisy progress"}))
        renderer.emit(
            Event(
                "tool_execution_end",
                {
                    "call_id": "c1",
                    "content": "one\ntwo\nthree\n18 passed in 2.41s\n[Exit code: 0]",
                    "is_error": False,
                },
            )
        )

        summary = errors.getvalue()
        self.assertIn("╭─ bash · pytest -q", summary)
        self.assertIn("Exit code: 0", summary)
        self.assertIn("use /last-tool", summary)
        self.assertNotIn("noisy progress", summary)
        self.assertIn("╰─ done", summary)

        renderer.show_last_tool(2)
        full = output.getvalue()
        self.assertIn("one\ntwo", full)
        self.assertIn("… 3 more lines", full)

    def test_read_and_error_tools_have_specific_summaries(self) -> None:
        output = io.StringIO()
        renderer = Renderer("text", stdout=output, stderr=output)
        renderer.emit(
            Event(
                "tool_execution_start",
                {"tool_call": {"id": "r", "name": "read", "arguments": {"path": "src/core.py"}}},
            )
        )
        renderer.emit(
            Event(
                "tool_execution_end",
                {
                    "call_id": "r",
                    "content": "1: first\n2: second\n[More lines: use offset=3]",
                    "is_error": False,
                },
            )
        )
        renderer.emit(
            Event(
                "tool_execution_start",
                {"tool_call": {"id": "e", "name": "edit", "arguments": {"path": "src/core.py"}}},
            )
        )
        renderer.emit(
            Event(
                "tool_execution_end",
                {"call_id": "e", "content": "old_text not found", "is_error": True},
            )
        )
        text = output.getvalue()
        self.assertIn("returned 2 numbered lines", text)
        self.assertIn("more available from line 3", text)
        self.assertIn("old_text not found", text)
        self.assertIn("╰─ error", text)

    def test_history_summarizes_tool_payloads(self) -> None:
        output = io.StringIO()
        renderer = Renderer("text", stdout=output, stderr=output)
        call = ToolCall("c", "bash", {"command": "pytest -q"})
        renderer.show_history(
            [
                Message("user", "run tests"),
                Message("assistant", tool_calls=(call,), stop_reason="tool_use"),
                Message("tool", "large output", tool_call_id="c", is_error=True),
            ]
        )
        text = output.getvalue()
        self.assertIn("[1] user", text)
        self.assertIn("[3] tool:bash error", text)
        self.assertIn("pytest -q", text)
        self.assertNotIn("large output", text)

    def test_json_mode_is_unchanged(self) -> None:
        output = io.StringIO()
        renderer = Renderer("json", stdout=output, stderr=io.StringIO())
        renderer.emit(Event("message_update", {"delta": "hello"}))
        self.assertEqual(json.loads(output.getvalue()), {"type": "message_update", "delta": "hello"})

    async def test_spinner_refreshes_one_tty_line_and_clears(self) -> None:
        output = TtyBuffer()
        errors = TtyBuffer()
        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            os.environ.pop("NO_COLOR", None)
            renderer = Renderer("text", stdout=output, stderr=errors)
            renderer.emit(Event("turn_start", {"turn": 1}))
            await asyncio.sleep(0.18)
            renderer.emit(Event("message_update", {"delta": "ready"}))
            await asyncio.sleep(0)

        dynamic = errors.getvalue()
        self.assertIn("thinking", dynamic)
        self.assertIn("\r\x1b[2K", dynamic)
        self.assertTrue(any(frame in dynamic for frame in ("⠋", "⠙", "⠹")))
        self.assertIn("\x1b[", dynamic)

    def test_no_color_environment_disables_ansi(self) -> None:
        output = TtyBuffer()
        with patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}, clear=False):
            renderer = Renderer("text", stdout=output, stderr=output)
            renderer.show_banner(self.session())
        self.assertNotIn("\x1b[", output.getvalue())


if __name__ == "__main__":
    unittest.main()
