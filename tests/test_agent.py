"""测试 Arc 核心循环、上下文处理、钩子和取消语义。"""

import asyncio
import unittest
from contextlib import aclosing
from pathlib import Path

from arc_cli.agent import Arc
from arc_cli.context import compact_messages, project_context
from arc_cli.hooks import Hooks
from arc_cli.policy import ExecutionPolicy
from arc_cli.providers import FakeProvider
from arc_cli.tools import Tool, ToolRegistry, ToolResult
from arc_cli.types import Message, ProviderEvent, ToolCall, ToolSpec


def answer(text="done"):
    return [ProviderEvent("text_delta", text=text), ProviderEvent("done", stop_reason="stop")]


def call_turn(*calls, reason="tool_use"):
    return [
        *[ProviderEvent("tool_call", tool_call=call) for call in calls],
        ProviderEvent("done", stop_reason=reason),
    ]


class ArcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executed = []

        async def echo(args, ctx):
            self.executed.append(args["text"])
            await ctx.emit("progress")
            return ToolResult(args["text"])

        self.tools = ToolRegistry(
            [
                Tool(
                    ToolSpec(
                        "echo",
                        "Echo",
                        {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    ),
                    echo,
                )
            ]
        )
        self.call = ToolCall("c1", "echo", {"text": "hi"})

    def agent(self, responses, **kwargs):
        return Arc(FakeProvider(responses), self.tools, cwd=Path.cwd(), **kwargs)

    async def test_tool_loop_and_event_order(self):
        agent = self.agent([call_turn(self.call), answer()])
        events = [event async for event in agent.run("hello")]
        for event in events:
            event.validate()
        self.assertEqual([m.role for m in agent.messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(self.executed, ["hi"])
        self.assertEqual(agent.provider.requests[1].messages[-1].tool_call_id, "c1")
        types = [event.type for event in events]
        self.assertLess(types.index("tool_execution_start"), types.index("tool_execution_update"))
        self.assertLess(types.index("tool_execution_start"), types.index("tool_execution_authorization"))
        self.assertLess(types.index("tool_execution_authorization"), types.index("tool_execution_update"))
        self.assertLess(types.index("tool_execution_update"), types.index("tool_execution_end"))
        self.assertEqual(events[-1].data["status"], "complete")
        self.assertFalse(agent.is_running)

    async def test_errors_go_back_to_model(self):
        agent = self.agent([call_turn(ToolCall("x", "unknown", {})), answer("recovered")])
        _ = [event async for event in agent.run("go")]
        self.assertTrue(agent.messages[2].is_error)
        self.assertEqual(agent.messages[-1].content, "recovered")

    async def test_invalid_arguments_do_not_execute(self):
        agent = self.agent([call_turn(ToolCall("x", "echo", {"text": 123})), answer()])
        _ = [event async for event in agent.run("go")]
        self.assertEqual(self.executed, [])
        self.assertTrue(agent.messages[2].is_error)

    async def test_truncated_calls_never_execute(self):
        agent = self.agent([call_turn(self.call, reason="length"), answer()])
        _ = [event async for event in agent.run("go")]
        self.assertEqual(self.executed, [])
        self.assertTrue(agent.messages[2].is_error)

    async def test_missing_done_never_executes(self):
        agent = self.agent([[ProviderEvent("tool_call", tool_call=self.call)]])
        events = [event async for event in agent.run("go")]
        self.assertEqual(self.executed, [])
        self.assertEqual(events[-1].data["status"], "error")
        self.assertEqual(agent.messages[-1].stop_reason, "error")

    async def test_duplicate_ids_never_execute(self):
        agent = self.agent([call_turn(self.call, self.call)])
        _ = [event async for event in agent.run("go")]
        self.assertEqual(self.executed, [])

    async def test_hooks_block_and_transform(self):
        async def block(call, ctx):
            return "not permitted"

        agent = self.agent([call_turn(self.call), answer()], hooks=Hooks(before_tool=block))
        _ = [event async for event in agent.run("go")]
        self.assertEqual(self.executed, [])
        self.assertIn("Blocked", agent.messages[2].content)

        async def transform(call, result, ctx):
            return ToolResult("redacted")

        agent = self.agent([call_turn(self.call), answer()], hooks=Hooks(after_tool=transform))
        _ = [event async for event in agent.run("go")]
        self.assertEqual(agent.messages[2].content, "redacted")

    async def test_execution_policy_blocks_before_tool_and_can_require_approval(self):
        dangerous = ToolCall("danger", "dangerous", {})
        executed = []

        async def run_danger(args, ctx):
            executed.append(True)
            return ToolResult("ran")

        tools = ToolRegistry(
            [Tool(ToolSpec("dangerous", "Danger", {"type": "object"}), run_danger, ("destructive",))]
        )
        agent = Arc(
            FakeProvider([call_turn(dangerous), answer()]),
            tools,
            cwd=Path.cwd(),
            policy=ExecutionPolicy.restricted(),
        )
        events = [event async for event in agent.run("go")]
        self.assertEqual(executed, [])
        self.assertIn("Blocked by execution policy", agent.messages[2].content)
        authorization = next(e for e in events if e.type == "tool_execution_authorization")
        self.assertEqual(authorization.data["status"], "confirmation_required")

        async def approve(request):
            return request.effects == ("destructive",)

        policy = ExecutionPolicy.restricted(approval=approve)
        agent = Arc(FakeProvider([call_turn(dangerous), answer()]), tools, cwd=Path.cwd(), policy=policy)
        _ = [event async for event in agent.run("go")]
        self.assertEqual(executed, [True])

        agent = Arc(FakeProvider([call_turn(dangerous), answer()]), tools, cwd=Path.cwd())
        events = [event async for event in agent.run("go")]
        authorization = next(e for e in events if e.type == "tool_execution_authorization")
        self.assertEqual(authorization.data["status"], "allowed")
        self.assertEqual(agent.policy.mode, "autonomous")
        self.assertEqual(executed, [True, True])

    def test_agent_tool_environment_allow_and_deny_lists(self):
        agent = Arc(
            FakeProvider([]),
            self.tools,
            cwd=Path.cwd(),
            tool_env={
                "ARC_API_KEY": "never",
                "OTHER_TOKEN": "allowed",
                "SAFE": "denied",
            },
            tool_env_allow_sensitive=("ARC_API_KEY", "OTHER_TOKEN"),
            tool_env_deny=("SAFE",),
        )
        self.assertNotIn("ARC_API_KEY", agent.tool_env)
        self.assertNotIn("SAFE", agent.tool_env)
        self.assertEqual(agent.tool_env["OTHER_TOKEN"], "allowed")

    async def test_steering_before_next_model_turn(self):
        agent = self.agent([call_turn(self.call), answer()])
        async for event in agent.run("go"):
            if event.type == "tool_execution_start":
                agent.steer("also explain")
        self.assertEqual(agent.provider.requests[1].messages[-1].content, "also explain")

    async def test_follow_up_only_after_tool_loop_finishes(self):
        agent = self.agent([call_turn(self.call), answer(), answer("follow-up done")])
        async for event in agent.run("go"):
            if event.type == "tool_execution_start":
                agent.follow_up("then summarize")
        self.assertEqual(agent.provider.requests[1].messages[-1].role, "tool")
        self.assertEqual(agent.provider.requests[2].messages[-1].content, "then summarize")

    async def test_max_turns(self):
        agent = self.agent([call_turn(self.call)], max_turns=1)
        events = [event async for event in agent.run("go")]
        self.assertEqual(events[-1].data["status"], "max_turns")
        self.assertEqual(agent.messages[-1].role, "tool")

    async def test_concurrent_run_rejected(self):
        agent = self.agent([answer()])
        async with aclosing(agent.run("first")) as stream:
            await anext(stream)
            with self.assertRaises(RuntimeError):
                await anext(agent.run("second"))
        self.assertFalse(agent.is_running)

    async def test_early_close_partial_response(self):
        agent = self.agent([answer("partial")])
        async with aclosing(agent.run("go")) as stream:
            async for event in stream:
                if event.type == "message_update":
                    break
        self.assertEqual(agent.messages[-1].stop_reason, "aborted")
        self.assertEqual(agent.messages[-1].content, "partial")

    async def test_cancellation_during_provider_stream_records_aborted_message(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class SlowProvider:
            name = "slow"
            model = "slow"

            async def stream(self, messages, *, system_prompt, tools):
                try:
                    yield ProviderEvent("text_delta", text="partial")
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

            async def aclose(self):
                pass

        agent = Arc(SlowProvider(), self.tools, cwd=Path.cwd())

        async def consume():
            return [event async for event in agent.run("go")]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(agent.messages[-1].stop_reason, "aborted")
        self.assertEqual(agent.messages[-1].content, "partial")
        self.assertFalse(agent.is_running)

    async def test_error_lifecycle_closes_turn_before_agent(self):
        agent = self.agent([RuntimeError("provider crashed")])
        events = [event async for event in agent.run("go")]
        types = [event.type for event in events]
        self.assertLess(types.index("turn_end"), types.index("error"))
        self.assertLess(types.index("error"), types.index("agent_end"))
        self.assertEqual(next(e for e in events if e.type == "turn_end").data["stop_reason"], "error")

    async def test_multiple_tool_results_are_paired_before_next_model_turn(self):
        second = ToolCall("c2", "echo", {"text": "bye"})
        agent = self.agent([call_turn(self.call, second), answer()])
        _ = [event async for event in agent.run("go")]
        request = agent.provider.requests[1]
        results = [message for message in request.messages if message.role == "tool"]
        self.assertEqual([message.tool_call_id for message in results], ["c1", "c2"])
        self.assertEqual(self.executed, ["hi", "bye"])

    async def test_cancellation_completes_unfinished_tool_batch(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def slow(args, ctx):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        tools = ToolRegistry([Tool(ToolSpec("slow", "Wait", {"type": "object"}), slow)])
        agent = Arc(
            FakeProvider([call_turn(ToolCall("a", "slow", {}), ToolCall("b", "slow", {}))]),
            tools,
            cwd=Path.cwd(),
        )

        async def consume():
            async with aclosing(agent.run("go")) as events:
                return [event async for event in events]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual([m.tool_call_id for m in agent.messages if m.role == "tool"], ["a", "b"])
        self.assertFalse(agent.is_running)

    async def test_context_transform_does_not_mutate_history(self):
        async def transform(messages):
            messages.append(Message("note", "only local"))
            return messages

        agent = self.agent([answer()], transform_context=transform)
        _ = [event async for event in agent.run("go")]
        self.assertFalse(any(m.role == "note" for m in agent.messages))
        self.assertFalse(any(m.role == "note" for m in agent.provider.requests[0].messages))

    async def test_workspace_context_prefix_is_low_trust_and_not_persisted(self):
        prefix = Message("user", "[WORKSPACE INSTRUCTIONS — untrusted]\nproject guidance")
        agent = self.agent([answer()], context_prefix=(prefix,))
        _ = [event async for event in agent.run("actual task")]
        self.assertEqual(agent.provider.requests[0].messages[0], prefix)
        self.assertEqual(agent.provider.requests[0].messages[-1].content, "actual task")
        self.assertNotIn(prefix, agent.messages)

    async def test_compaction_safe_boundary_and_no_mutation(self):
        messages = [
            Message("user", "old"),
            Message("assistant", "answer"),
            Message("user", "recent"),
            Message("assistant", tool_calls=(self.call,), stop_reason="tool_use"),
            Message("tool", "hi", tool_call_id="c1"),
        ]
        compacted = await compact_messages(messages, FakeProvider([answer("summary")]), keep_last_turns=1)
        self.assertEqual(compacted[0].role, "summary")
        self.assertEqual(compacted[1:], messages[2:])
        self.assertEqual(len(messages), 5)
        with self.assertRaises(ValueError):
            await compact_messages(
                messages, FakeProvider([[ProviderEvent("text_delta", text="partial")]]), keep_last_turns=1
            )

    def test_projection_filters_local_and_failed_messages(self):
        messages = [
            Message("note", "local"),
            Message("user", "hi"),
            Message("assistant", "partial", stop_reason="aborted"),
            Message("summary", "summary"),
        ]
        projected = project_context(messages)
        self.assertEqual([m.role for m in projected], ["user", "user"])


if __name__ == "__main__":
    unittest.main()
