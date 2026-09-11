"""测试 Arc CLI 交互模式的命令调度和多任务行为。"""

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from arc_cli.agent import Arc
from arc_cli.cli import Renderer, interactive
from arc_cli.providers import FakeProvider
from arc_cli.runtime import ArcSession
from arc_cli.session import SessionStore
from arc_cli.tools import ToolRegistry, create_builtin_tools
from arc_cli.types import ProviderEvent


class InteractiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_commands_and_two_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            responses = [
                [
                    ProviderEvent("text_delta", text="first response"),
                    ProviderEvent("done", stop_reason="stop"),
                ],
                [
                    ProviderEvent("text_delta", text="second response"),
                    ProviderEvent("done", stop_reason="stop"),
                ],
            ]
            agent = Arc(FakeProvider(responses), ToolRegistry(create_builtin_tools()), cwd=cwd)
            session = ArcSession(agent, SessionStore.create(cwd))
            output = io.StringIO()
            with create_pipe_input() as pipe:
                terminal = PromptSession(input=pipe, output=DummyOutput())
                with (
                    patch("arc_cli.cli.PromptSession", return_value=terminal),
                    patch(
                        "arc_cli.cli.patch_terminal_stdout",
                        side_effect=contextlib.nullcontext,
                    ),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(output),
                ):
                    task = asyncio.create_task(interactive(session, "", Renderer("text")))
                    try:
                        pipe.send_text("first\n")
                        async with asyncio.timeout(3):
                            while len(agent.messages) < 2 or agent.is_running:
                                await asyncio.sleep(0.01)
                        pipe.send_text(
                            "/status\n/last-tool 20\n/tree\n/history\n/tools\n"
                            "/session\n/model\n/help\n/clear\n/new\n"
                        )
                        async with asyncio.timeout(3):
                            while agent.messages:
                                await asyncio.sleep(0.01)
                        pipe.send_text("second\n")
                        async with asyncio.timeout(3):
                            while len(agent.messages) < 2 or agent.is_running:
                                await asyncio.sleep(0.01)
                        pipe.send_text("/quit\n")
                        self.assertEqual(await asyncio.wait_for(task, 3), 0)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(agent.messages[0].content, "second")
            self.assertEqual(len(session.store.entries), 4)
            self.assertIn("ARC 0.1.1 · scripted", output.getvalue())
            self.assertIn("› task   ↳ steer", output.getvalue())
            self.assertIn("model    scripted", output.getvalue())
            self.assertIn("No tool result in this run.", output.getvalue())
            self.assertNotIn("error │", output.getvalue())


if __name__ == "__main__":
    unittest.main()
