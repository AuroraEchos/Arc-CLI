"""将模型服务协议转换为 Arc 运行时的 Provider 事件。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal

import httpx

from arc_cli.types import JsonObject, Message, ProviderEvent, StopReason, ToolCall, ToolSpec, Usage

ThinkingMode = Literal["enabled", "disabled"]
ReasoningEffort = Literal["none", "low", "high", "max"]
REASONING_EFFORT_ALIASES: dict[str, ReasoningEffort] = {
    "none": "none",
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
}


def normalize_reasoning_effort(value: str | None) -> ReasoningEffort | None:
    """Normalize DeepSeek/OpenAI-compatible reasoning effort aliases."""

    if value is None:
        return None
    try:
        return REASONING_EFFORT_ALIASES[value]
    except KeyError:
        choices = ", ".join(REASONING_EFFORT_ALIASES)
        raise ValueError(f"reasoning_effort must be one of: {choices}") from None


class ProviderError(RuntimeError):
    """表示模型传输或响应协议不满足运行时约束。"""

    pass


async def sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """SSE 消息由空行分隔；一个消息允许有多行 data:，注释/心跳忽略。"""
    parts: list[str] = []
    size = 0
    async for line in lines:
        if not line:
            if parts:
                yield "\n".join(parts)
            parts = []
            size = 0
        elif line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            size += len(value)
            if size > 2_000_000:
                raise ProviderError("SSE event exceeds size limit")
            parts.append(value)
    if parts:
        yield "\n".join(parts)


def wire_messages(messages: Sequence[Message], system_prompt: str) -> list[JsonObject]:
    """将统一消息结构转换为 Chat Completions 请求消息。"""

    result: list[JsonObject] = [{"role": "system", "content": system_prompt}]
    for message in messages:
        if message.role not in ("user", "assistant", "tool"):
            raise ValueError("Call project_context before the provider")
        item: JsonObject = {"role": message.role, "content": message.content}
        if message.role == "assistant" and message.reasoning_content:
            item["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False, allow_nan=False),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "tool":
            item["tool_call_id"] = message.tool_call_id
        result.append(item)
    return result


class OpenAIProvider:
    """提供严格、流式的 OpenAI Chat Completions 兼容适配。"""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60,
        thinking: ThinkingMode | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        normalized_effort = normalize_reasoning_effort(reasoning_effort)
        if thinking not in (None, "enabled", "disabled"):
            raise ValueError("thinking must be enabled or disabled")
        if max_tokens is not None and not 1 <= max_tokens <= 393_216:
            raise ValueError("max_tokens must be between 1 and 393216")
        if normalized_effort == "none" and thinking == "enabled":
            raise ValueError("reasoning_effort=none conflicts with thinking=enabled")
        if normalized_effort not in (None, "none") and thinking == "disabled":
            raise ValueError("reasoning_effort enables thinking and conflicts with thinking=disabled")
        if not model.strip() or timeout <= 0:
            raise ValueError("model and timeout must be valid")
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
            raise ValueError("base_url must be HTTP(S) without embedded credentials")
        if url.query or url.fragment:
            raise ValueError("base_url must not contain query parameters or a fragment")
        self.model = model
        if thinking is None:
            if normalized_effort not in (None, "none"):
                thinking = "enabled"
            elif (
                normalized_effort == "none"
                or url.host.endswith("deepseek.com")
                or model.startswith("deepseek-")
            ):
                thinking = "disabled"
        self.thinking: ThinkingMode | None = thinking
        self.reasoning_effort = normalized_effort
        self._key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient()

    async def aclose(self) -> None:
        """关闭由当前 Provider 创建的 HTTP 客户端。"""

        if self._owns_client:
            await self._client.aclose()

    async def stream(
        self, messages: Sequence[Message], *, system_prompt: str, tools: Sequence[ToolSpec]
    ) -> AsyncGenerator[ProviderEvent, None]:
        """发送模型请求，并将 SSE 响应转换为统一事件流。"""

        payload: JsonObject = {
            "model": self.model,
            "messages": wire_messages(messages, system_prompt),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        headers = {"Accept": "text/event-stream"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        calls: dict[int, dict[str, str]] = {}
        finish = None
        usage = None
        ended = False
        size = 0
        try:
            async with self._client.stream(
                "POST", self._url, json=payload, headers=headers, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    # 不输出服务器原文/请求 URL，避免日志带出凭证和代理密钥。
                    raise ProviderError(
                        f"Model HTTP {response.status_code}; check endpoint, model and credentials"
                    )
                async for data in sse_data(response.aiter_lines()):
                    if data == "[DONE]":
                        ended = True
                        break
                    size += len(data)
                    if size > 4_000_000:
                        raise ProviderError("Model stream exceeds 4 MB limit")
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise ProviderError("Provider returned an error or invalid stream object")
                    raw_usage = chunk.get("usage")
                    if isinstance(raw_usage, dict):
                        values = [raw_usage.get("prompt_tokens", 0), raw_usage.get("completion_tokens", 0)]
                        if not all(type(value) is int and value >= 0 for value in values):
                            raise ProviderError("Invalid token usage")
                        usage = Usage(*values)
                    choices = chunk.get("choices", [])
                    if not isinstance(choices, list):
                        raise ProviderError("Invalid choices")
                    for choice in choices:
                        if choice.get("index", 0) != 0:
                            raise ProviderError("Multiple choices are unsupported")
                        if finish is not None:
                            raise ProviderError("Received a choice after finish_reason")
                        delta = choice.get("delta", {})
                        reasoning_content = delta.get("reasoning_content")
                        if reasoning_content:
                            if not isinstance(reasoning_content, str):
                                raise ProviderError("Invalid reasoning_content delta")
                            yield ProviderEvent("reasoning_delta", text=reasoning_content)
                        content = delta.get("content") or delta.get("refusal")
                        if content:
                            if not isinstance(content, str):
                                raise ProviderError("Invalid content delta")
                            yield ProviderEvent("text_delta", text=content)
                        # Some compatible providers emit an explicit null instead of omitting the field.
                        for fragment in delta.get("tool_calls") or []:
                            index = fragment["index"]
                            if type(index) is not int or index < 0 or index > 127:
                                raise ProviderError("Invalid tool call index")
                            call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if fragment.get("type", "function") != "function":
                                raise ProviderError("Only function tools are supported")
                            function = fragment.get("function", {})
                            call["id"] += fragment.get("id") or ""
                            call["name"] += function.get("name") or ""
                            call["arguments"] += function.get("arguments") or ""
                        finish = choice.get("finish_reason")
            if not ended or finish not in (
                "stop",
                "tool_calls",
                "length",
                "content_filter",
                "insufficient_system_resource",
                "aborted",
            ):
                raise ProviderError("Incomplete or unsupported model response; no tools executed")
            if (calls and finish == "stop") or (finish == "tool_calls" and not calls):
                raise ProviderError("Inconsistent finish_reason and tool calls")
            parsed: list[ToolCall] = []
            for index in sorted(calls):
                call = calls[index]
                arguments = json.loads(call["arguments"])
                if not isinstance(arguments, dict) or not call["id"] or not call["name"]:
                    raise ProviderError("Incomplete tool call")
                # json.loads 默认接收 NaN/Infinity；工具参数必须是严格 JSON。
                json.dumps(arguments, allow_nan=False)
                parsed.append(ToolCall(call["id"], call["name"], arguments))
            if len({call.id for call in parsed}) != len(parsed):
                raise ProviderError("Duplicate tool call IDs")
            # 完整解析全部参数后才发出工具调用。Arc 仍需等待 done 才能执行。
            for parsed_call in parsed:
                yield ProviderEvent("tool_call", tool_call=parsed_call)
            reason: StopReason
            if finish == "tool_calls":
                reason = "tool_use"
            else:
                reason = finish
            yield ProviderEvent("done", stop_reason=reason, usage=usage)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Model transport error ({type(exc).__name__}); no automatic retry") from None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError(f"Malformed model stream ({type(exc).__name__}); no tools executed") from None


@dataclass(frozen=True)
class ProviderRequest:
    """记录测试 Provider 收到的一次完整请求。"""

    messages: tuple[Message, ...]
    system_prompt: str
    tools: tuple[ToolSpec, ...]


class FakeProvider:
    """按预设脚本产生事件且不访问网络的测试 Provider。"""

    name = "fake"
    model = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderEvent] | Exception]):
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self, messages: Sequence[Message], *, system_prompt: str, tools: Sequence[ToolSpec]
    ) -> AsyncGenerator[ProviderEvent, None]:
        """记录请求并发送下一组预设事件。"""

        index = len(self.requests)
        self.requests.append(ProviderRequest(tuple(messages), system_prompt, tuple(tools)))
        if index >= len(self.responses):
            raise ProviderError("FakeProvider script exhausted")
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        for event in response:
            await asyncio.sleep(0)
            yield event

    async def aclose(self) -> None:
        """保持与真实 Provider 一致的异步关闭接口。"""

        pass
