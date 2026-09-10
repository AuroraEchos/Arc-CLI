"""定义 Arc CLI 各层共享的数据结构、事件和 Provider 协议。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
JsonObject: TypeAlias = dict[str, Any]

Role = Literal["user", "assistant", "tool", "summary", "note"]
StopReason = Literal["stop", "tool_use", "length", "error", "aborted"]


@dataclass(frozen=True)
class Usage:
    """记录一次模型响应的输入与输出 token 数。"""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ToolCall:
    """表示模型请求的一次结构化工具调用。"""

    id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True)
class ToolSpec:
    """描述工具名称、用途和 JSON Schema 参数。"""

    name: str
    description: str
    parameters: JsonObject


@dataclass(frozen=True)
class Message:
    """表示会话历史中的一条统一消息。"""

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    stop_reason: StopReason | None = None
    usage: Usage | None = None
    is_error: bool = False

    def to_dict(self) -> JsonObject:
        """将消息及其嵌套数据转换为可序列化字典。"""
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: JsonObject) -> Message:
        """校验字典内容并重建消息对象。"""
        role, content = data.get("role"), data.get("content", "")
        if role not in ("user", "assistant", "tool", "summary", "note") or not isinstance(content, str):
            raise ValueError("Invalid message role/content")
        raw_calls = data.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise ValueError("Invalid tool_calls")
        calls = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                raise ValueError("Invalid tool call")
            if not all(isinstance(raw.get(key), str) and raw[key] for key in ("id", "name")):
                raise ValueError("Invalid tool call id/name")
            if not isinstance(raw.get("arguments"), dict):
                raise ValueError("Invalid tool arguments")
            calls.append(ToolCall(raw["id"], raw["name"], raw["arguments"]))
        reason = data.get("stop_reason")
        if reason not in (None, "stop", "tool_use", "length", "error", "aborted"):
            raise ValueError("Invalid stop reason")
        for key in ("tool_call_id", "name"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValueError(f"Invalid {key}")
        if type(data.get("is_error", False)) is not bool:
            raise ValueError("Invalid is_error")
        usage = data.get("usage")
        if usage is not None:
            if not isinstance(usage, dict) or any(
                type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0
                for key in ("input_tokens", "output_tokens")
            ):
                raise ValueError("Invalid usage")
            usage = Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        return cls(
            role,
            content,
            tuple(calls),
            data.get("tool_call_id"),
            data.get("name"),
            reason,
            usage,
            data.get("is_error", False),
        )


@dataclass(frozen=True)
class ProviderEvent:
    """表示 Provider 向 Arc 发送的流式响应事件。"""

    kind: Literal["text_delta", "tool_call", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    stop_reason: StopReason | None = None
    usage: Usage | None = None


@dataclass(frozen=True)
class Event:
    """表示 Arc 向终端或其他消费者发送的运行时事件。"""

    type: str
    data: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        """将事件类型和载荷展平为可序列化字典。"""

        return {"type": self.type, **self.data}


class Provider(Protocol):
    """定义 Arc 调用模型服务所需的最小异步接口。"""

    name: str
    model: str

    def stream(
        self, messages: Sequence[Message], *, system_prompt: str, tools: Sequence[ToolSpec]
    ) -> AsyncGenerator[ProviderEvent, None]: ...

    async def aclose(self) -> None: ...
