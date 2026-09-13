"""测试 OpenAI 兼容 Provider 的请求和 SSE 响应解析。"""

import json
import unittest

import httpx

from arc_cli.providers import OpenAIProvider, ProviderError, sse_data, wire_messages
from arc_cli.types import Message, ToolCall


def chunk(delta=None, finish=None, **extra):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra}


def sse(chunks, done=True):
    data = "".join("data: " + json.dumps(item) + "\n\n" for item in chunks)
    return (data + ("data: [DONE]\n\n" if done else "")).encode()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def stream(self, content, status=200, **provider_options):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(status, content=content, headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = OpenAIProvider(model="test", api_key="secret-value", client=client, **provider_options)
            events = [
                event
                async for event in provider.stream([Message("user", "hello")], system_prompt="test", tools=[])
            ]
            await provider.aclose()
            self.assertFalse(client.is_closed, "Injected client remains caller-owned")
        return events, requests

    async def test_text_usage_and_request(self):
        events, requests = await self.stream(
            sse(
                [
                    chunk({"content": "你好", "tool_calls": None}),
                    chunk(finish="stop"),
                    {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
                ]
            )
        )
        self.assertEqual(events[0].text, "你好")
        self.assertEqual(events[-1].usage.input_tokens, 12)
        payload = json.loads(requests[0].content)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertNotIn("thinking", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["model"], "test")
        self.assertEqual(requests[0].headers["authorization"], "Bearer secret-value")

    async def test_fragmented_interleaved_tools(self):
        events, _ = await self.stream(
            sse(
                [
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "b",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"pa'},
                                },
                                {
                                    "index": 0,
                                    "id": "a",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"path":'},
                                },
                            ]
                        }
                    ),
                    chunk(
                        {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"a"}'}},
                                {"index": 1, "function": {"arguments": 'th":"b"}'}},
                            ]
                        }
                    ),
                    chunk(finish="tool_calls"),
                ]
            )
        )
        self.assertEqual([e.tool_call.id for e in events if e.tool_call], ["a", "b"])
        self.assertEqual(events[0].tool_call.arguments, {"path": "a"})
        self.assertEqual(events[-1].stop_reason, "tool_use")

    async def test_eof_without_done_rejected(self):
        with self.assertRaises(ProviderError):
            await self.stream(sse([chunk({"content": "partial"}), chunk(finish="stop")], done=False))

    async def test_malformed_tool_arguments_rejected(self):
        for args in ('{"path":', "[]", '{"x":NaN}'):
            with self.subTest(args=args), self.assertRaises(ProviderError):
                await self.stream(
                    sse(
                        [
                            chunk(
                                {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "a",
                                            "function": {
                                                "name": "read",
                                                "arguments": args,
                                            },
                                        }
                                    ]
                                }
                            ),
                            chunk(finish="tool_calls"),
                        ]
                    )
                )

    async def test_http_error_does_not_leak_body(self):
        with self.assertRaises(ProviderError) as caught:
            await self.stream(b"secret-value sensitive body", status=401)
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))

    async def test_length_is_not_success(self):
        events, _ = await self.stream(sse([chunk({"content": "partial"}), chunk(finish="length")]))
        self.assertEqual(events[-1].stop_reason, "length")

    async def test_supported_finish_reasons_and_duplicate_ids(self):
        for finish in ("content_filter", "insufficient_system_resource", "aborted"):
            with self.subTest(finish=finish):
                events, _ = await self.stream(sse([chunk(finish=finish)]))
                self.assertEqual(events[-1].stop_reason, finish)
        with self.assertRaises(ProviderError):
            await self.stream(sse([chunk(finish="unknown_reason")]))
        calls = [
            {"index": i, "id": "same", "function": {"name": "read", "arguments": "{}"}} for i in range(2)
        ]
        with self.assertRaises(ProviderError):
            await self.stream(sse([chunk({"tool_calls": calls}), chunk(finish="tool_calls")]))

    async def test_sse_multiline_and_heartbeat(self):
        async def lines():
            for line in [": heartbeat", "event: message", 'data: {"a":', "data: 1}", "", "data: [DONE]", ""]:
                yield line

        parts = [part async for part in sse_data(lines())]
        self.assertEqual(json.loads(parts[0]), {"a": 1})
        self.assertEqual(parts[1], "[DONE]")

    def test_wire_roundtrip_tool_pair(self):
        messages = [
            Message(
                "assistant",
                tool_calls=(ToolCall("c", "read", {"path": "a"}),),
                reasoning_content="private reasoning",
            ),
            Message("tool", "text", tool_call_id="c", is_error=True),
        ]
        wire = wire_messages(messages, "system")
        self.assertEqual(wire[1]["tool_calls"][0]["function"]["arguments"], '{"path": "a"}')
        self.assertEqual(wire[1]["reasoning_content"], "private reasoning")
        self.assertEqual(wire[2]["tool_call_id"], "c")

    async def test_deepseek_thinking_effort_max_tokens_and_reasoning_stream(self):
        events, requests = await self.stream(
            sse(
                [
                    chunk({"reasoning_content": "think ", "content": None}),
                    chunk({"reasoning_content": "carefully", "content": "answer"}),
                    chunk(
                        finish="stop",
                        usage={"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29},
                    ),
                ]
            ),
            thinking="enabled",
            reasoning_effort="minimal",
            max_tokens=393_216,
        )
        self.assertEqual(
            [event.kind for event in events],
            [
                "reasoning_delta",
                "reasoning_delta",
                "text_delta",
                "done",
            ],
        )
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_tokens"], 393_216)
        self.assertEqual(events[-1].usage.input_tokens, 21)

    async def test_deepseek_defaults_to_non_thinking_mode(self):
        _, requests = await self.stream(
            sse([chunk(finish="stop")]),
            base_url="https://api.deepseek.com",
        )
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

        _, requests = await self.stream(
            sse([chunk(finish="stop")]),
            reasoning_effort="none",
        )
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["reasoning_effort"], "none")

    def test_reasoning_parameter_validation_and_aliases(self):
        self.assertEqual(
            OpenAIProvider(model="test", reasoning_effort="medium").reasoning_effort,
            "high",
        )
        self.assertEqual(OpenAIProvider(model="test", reasoning_effort="xhigh").reasoning_effort, "high")
        self.assertEqual(OpenAIProvider(model="test", reasoning_effort="max").thinking, "enabled")
        self.assertEqual(OpenAIProvider(model="test", reasoning_effort="none").thinking, "disabled")
        with self.assertRaises(ValueError):
            OpenAIProvider(model="test", thinking="disabled", reasoning_effort="high")
        with self.assertRaises(ValueError):
            OpenAIProvider(model="test", thinking="enabled", reasoning_effort="none")
        for max_tokens in (0, 393_217):
            with self.subTest(max_tokens=max_tokens), self.assertRaises(ValueError):
                OpenAIProvider(model="test", max_tokens=max_tokens)


if __name__ == "__main__":
    unittest.main()
