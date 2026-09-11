"""测试 Arc 会话树的持久化、分支、恢复和压缩行为。"""

import json
import tempfile
import unittest
from contextlib import aclosing
from pathlib import Path

from arc_cli.agent import Arc
from arc_cli.providers import FakeProvider
from arc_cli.runtime import ArcSession
from arc_cli.session import SessionStore, latest_session_path, new_session_path
from arc_cli.tools import Tool, ToolRegistry, ToolResult
from arc_cli.types import Message, ProviderEvent, ToolCall, ToolSpec, Usage


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name)
        self.path = self.cwd / "session.jsonl"

    def store(self):
        store = SessionStore.create(self.cwd, self.path)
        self.addCleanup(store.close)
        return store

    def test_roundtrip_branch_and_checkpoint(self):
        store = self.store()
        first = store.append(Message("user", "first"))
        old = store.append(Message("assistant", "old branch", usage=Usage(2, 3)))
        store.branch(first[:8])
        store.append(Message("assistant", "new branch"))
        self.assertEqual(len(store.entries), 3)
        self.assertEqual(store.messages()[-1].content, "new branch")
        store.checkpoint([Message("summary", "summary"), Message("user", "recent")])
        self.assertEqual(store.messages()[0].role, "summary")
        store.close()
        restored = SessionStore.open(self.path)
        self.addCleanup(restored.close)
        self.assertEqual(restored.messages()[-1].content, "recent")
        restored.branch(old)
        self.assertEqual(restored.messages()[-1].usage, Usage(2, 3))
        tree = restored.tree_lines()
        self.assertEqual(len(tree), 4)
        self.assertTrue(any("●" in line and "current" in line for line in tree))
        self.assertTrue(any("├─" in line or "└─" in line for line in tree))

    def test_exclusive_create_and_single_writer(self):
        store = self.store()
        with self.assertRaises(FileExistsError):
            SessionStore.create(self.cwd, self.path)
        with self.assertRaisesRegex(ValueError, "writer"):
            SessionStore.open(self.path)
        store.close()
        reopened = SessionStore.open(self.path)
        reopened.close()

    def test_corrupt_tail_refused_without_changing_file(self):
        store = self.store()
        store.append(Message("user", "keep"))
        store.close()
        with self.path.open("ab") as handle:
            handle.write(b'{"type":')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            SessionStore.open(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_parent_refused(self):
        store = self.store()
        store.close()
        with self.path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "x",
                        "parent": "missing",
                        "message": Message("user", "bad").to_dict(),
                    }
                )
                + "\n"
            )
        with self.assertRaises(ValueError):
            SessionStore.open(self.path)

    def test_partial_tool_branch_repaired_without_reexecution(self):
        store = self.store()
        store.append(Message("assistant", tool_calls=(ToolCall("a", "write", {}), ToolCall("b", "bash", {}))))
        store.append(Message("tool", "done", tool_call_id="a"))
        messages = store.messages()
        self.assertEqual(messages[-1].tool_call_id, "b")
        self.assertTrue(messages[-1].is_error)
        self.assertEqual(len(store.entries), 2)

    def test_argument_snapshot(self):
        store = self.store()
        arguments = {"path": "original"}
        store.append(Message("assistant", tool_calls=(ToolCall("a", "read", arguments),)))
        arguments["path"] = "changed"
        self.assertEqual(store.messages()[0].tool_calls[0].arguments["path"], "original")

    def test_latest_and_root(self):
        with self.assertRaises(ValueError):
            latest_session_path(self.cwd)
        path = new_session_path(self.cwd)
        store = SessionStore.create(self.cwd, path)
        self.addCleanup(store.close)
        store.append(Message("user", "hi"))
        self.assertEqual(latest_session_path(self.cwd), path)
        store.branch("root")
        self.assertEqual(store.messages(), [])

    async def test_runtime_saves_stream_close(self):
        store = self.store()
        provider = FakeProvider(
            [[ProviderEvent("text_delta", text="partial"), ProviderEvent("done", stop_reason="stop")]]
        )
        session = ArcSession(Arc(provider, ToolRegistry(), cwd=self.cwd), store)
        async with aclosing(session.run("go")) as events:
            async for event in events:
                if event.type == "message_update":
                    break
        self.assertEqual(store.messages()[-1].stop_reason, "aborted")
        self.assertEqual(store.messages()[-1].content, "partial")
        self.assertFalse(session.agent.is_running)

    async def test_runtime_cancellation_persists_tool_results(self):
        store = self.store()
        provider = FakeProvider(
            [
                [
                    ProviderEvent("tool_call", tool_call=ToolCall("a", "unknown", {})),
                    ProviderEvent("done", stop_reason="tool_use"),
                ]
            ]
        )
        session = ArcSession(Arc(provider, ToolRegistry(), cwd=self.cwd), store)
        async with aclosing(session.run("go")) as events:
            async for event in events:
                if event.type == "tool_execution_start":
                    break
        self.assertTrue(store.messages()[-1].is_error)
        self.assertEqual(store.messages()[-1].tool_call_id, "a")
        self.assertEqual(len(store.entries), 3)

    async def test_tool_completion_is_saved_if_event_consumer_crashes(self):
        store = self.store()

        async def execute(args, context):
            return ToolResult("completed")

        tools = ToolRegistry([Tool(ToolSpec("work", "Work", {"type": "object"}), execute)])
        provider = FakeProvider(
            [
                [
                    ProviderEvent("tool_call", tool_call=ToolCall("done", "work", {})),
                    ProviderEvent("done", stop_reason="tool_use"),
                ]
            ]
        )
        session = ArcSession(Arc(provider, tools, cwd=self.cwd), store)
        with self.assertRaisesRegex(RuntimeError, "renderer crashed"):
            async with aclosing(session.run("go")) as events:
                async for event in events:
                    if event.type == "tool_execution_end":
                        raise RuntimeError("renderer crashed")
        tool_results = [message for message in store.messages() if message.role == "tool"]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0].content, "completed")

    async def test_compaction_failure_preserves_history(self):
        store = self.store()
        for index in range(4):
            store.append(Message("user", str(index)))
        provider = FakeProvider([RuntimeError("network down")])
        session = ArcSession(Arc(provider, ToolRegistry(), cwd=self.cwd), store)
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            await session.compact()
        self.assertEqual(self.path.read_bytes(), before)

    def test_message_validation(self):
        message = Message("assistant", "hi", (ToolCall("a", "read", {"path": "x"}),), usage=Usage(1, 2))
        self.assertEqual(Message.from_dict(json.loads(json.dumps(message.to_dict()))), message)
        for data in (
            {"role": "invalid"},
            {"role": "user", "content": 123},
            {"role": "user", "usage": {"input_tokens": True}},
        ):
            with self.assertRaises(ValueError):
                Message.from_dict(data)

    def test_closed_store_refuses_silent_writes(self):
        store = self.store()
        store.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            store.append(Message("user", "must not silently disappear"))


if __name__ == "__main__":
    unittest.main()
