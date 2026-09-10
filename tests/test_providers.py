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
    async def stream(self, content, status=200):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(status, content=content, headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = OpenAIProvider(model="test", api_key="secret-value", client=client)
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

    async def test_invalid_finish_and_duplicate_ids(self):
        with self.assertRaises(ProviderError):
            await self.stream(sse([chunk(finish="content_filter")]))
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
            Message("assistant", tool_calls=(ToolCall("c", "read", {"path": "a"}),)),
            Message("tool", "text", tool_call_id="c", is_error=True),
        ]
        wire = wire_messages(messages, "system")
        self.assertEqual(wire[1]["tool_calls"][0]["function"]["arguments"], '{"path": "a"}')
        self.assertEqual(wire[2]["tool_call_id"], "c")


if __name__ == "__main__":
    unittest.main()
