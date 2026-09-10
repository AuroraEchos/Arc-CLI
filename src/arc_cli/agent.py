"""实现 Arc 的消息状态与模型—工具循环。

本模块只负责编排模型调用和工具执行，不依赖终端、HTTP 实现或会话文件格式。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from arc_cli.context import project_context
from arc_cli.hooks import Hooks
from arc_cli.tools import ToolContext, ToolRegistry, ToolResult
from arc_cli.types import Event, Message, Provider, ToolCall


class Arc:
    """驱动模型推理、工具调用和运行时事件的核心 Agent。"""

    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        *,
        cwd: Path,
        system_prompt: str = "You are a coding assistant.",
        messages: Sequence[Message] = (),
        hooks: Hooks | None = None,
        max_turns: int = 20,
        transform_context: Callable[[list[Message]], Awaitable[list[Message]]] | None = None,
    ):
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.provider = provider
        self.tools = tools
        self.cwd = cwd.resolve()
        self.system_prompt = system_prompt
        self.messages = deepcopy(list(messages))
        self.hooks = hooks or Hooks()
        self.max_turns = max_turns
        self.transform_context = transform_context
        self.is_running = False
        self._steering: deque[str] = deque()
        self._follow_ups: deque[str] = deque()

    def steer(self, text: str) -> None:
        """在下一次模型调用前注入，不打断当前工具。"""
        if text.strip():
            self._steering.append(text)

    def follow_up(self, text: str) -> None:
        """在 Arc 原本要结束时继续一个新问题。"""
        if text.strip():
            self._follow_ups.append(text)

    def clear_queues(self) -> None:
        """清空尚未消费的 steering 与 follow-up 指令。"""

        self._steering.clear()
        self._follow_ups.clear()

    def _append(self, message: Message) -> Event:
        """保存消息并创建对应的 ``message_end`` 事件。"""

        self.messages.append(message)
        return Event("message_end", {"message": message.to_dict()})

    async def _execute(self, call: ToolCall) -> AsyncGenerator[Event, None]:
        """执行单个工具调用，并按顺序发送进度与完成事件。"""

        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=32)

        async def emit(text: str) -> None:
            await queue.put(text)

        context = ToolContext(self.cwd, emit)

        async def invoke() -> ToolResult:
            try:
                self.tools.validate(call)
                if self.hooks.before_tool:
                    reason = await self.hooks.before_tool(deepcopy(call), context)
                    if reason:
                        return ToolResult(f"Blocked by hook: {reason}", True)
                result = await self.tools.execute(call, context)
                if self.hooks.after_tool:
                    result = await self.hooks.after_tool(deepcopy(call), result, context)
                return result
            except Exception as exc:
                return ToolResult(f"Tool/hook error: {type(exc).__name__}: {exc}", True)
            finally:
                # On cancellation the consumer is no longer draining a full queue.
                current = asyncio.current_task()
                if current is not None and not current.cancelling():
                    await queue.put(None)

        task = asyncio.create_task(invoke())
        try:
            while (delta := await queue.get()) is not None:
                yield Event("tool_execution_update", {"call_id": call.id, "delta": delta})
            result = await task
            # 先提交状态，再通知 UI；UI 在任何 yield 后退出都不会丢掉已完成结果。
            message = Message(
                "tool", result.content, tool_call_id=call.id, name=call.name, is_error=result.is_error
            )
            ended = self._append(message)
            yield Event(
                "tool_execution_end",
                {"call_id": call.id, "content": result.content, "is_error": result.is_error},
            )
            yield ended
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
        """运行一次任务并以异步事件流报告状态。

        一次任务可能包含多个模型轮次。提前停止消费时，调用方应关闭异步生成器，
        以便 Arc 完成取消清理并补齐未完成的工具结果。
        """
        if self.is_running:
            raise RuntimeError("Arc already running; use steer/follow_up")
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        self.is_running = True
        partial: list[str] = []
        streaming = False
        pending: tuple[ToolCall, ...] = ()
        batch_start = len(self.messages)
        try:
            yield Event("agent_start")
            yield self._append(Message("user", prompt))
            for turn in range(1, self.max_turns + 1):
                while self._steering:
                    yield self._append(Message("user", self._steering.popleft()))
                yield Event("turn_start", {"turn": turn})
                context = deepcopy(self.messages)
                if self.transform_context:
                    context = await self.transform_context(context)
                context = project_context(context)
                partial = []
                calls: list[ToolCall] = []
                streaming = True
                yield Event("message_start", {"role": "assistant"})
                terminal = None
                async with aclosing(
                    self.provider.stream(context, system_prompt=self.system_prompt, tools=self.tools.specs())
                ) as stream:
                    async for event in stream:
                        if terminal is not None:
                            raise ValueError("Provider emitted data after done")
                        if event.kind == "text_delta":
                            partial.append(event.text)
                            yield Event("message_update", {"delta": event.text})
                        elif event.kind == "tool_call" and event.tool_call:
                            calls.append(event.tool_call)
                        elif event.kind == "done":
                            terminal = event
                if terminal is None:
                    raise ValueError("Provider stream ended without done")
                if terminal.stop_reason not in ("stop", "tool_use", "length"):
                    raise ValueError("Provider did not complete successfully")
                if len({call.id for call in calls}) != len(calls):
                    raise ValueError("Duplicate tool call IDs")
                if terminal.stop_reason == "tool_use" and not calls:
                    raise ValueError("Provider requested tools without tool calls")
                if calls and terminal.stop_reason == "stop":
                    raise ValueError("Unexpected tool calls on a stop response")
                message = Message(
                    "assistant",
                    "".join(partial),
                    tuple(calls),
                    stop_reason=terminal.stop_reason,
                    usage=terminal.usage,
                )
                streaming = False
                pending = message.tool_calls
                batch_start = len(self.messages)
                yield self._append(message)
                for call in pending:
                    yield Event("tool_execution_start", {"tool_call": asdict(call)})
                    if terminal.stop_reason == "length":
                        result = "Not executed: model output was truncated. Reissue complete arguments."
                        ended = self._append(
                            Message("tool", result, tool_call_id=call.id, name=call.name, is_error=True)
                        )
                        yield Event(
                            "tool_execution_end", {"call_id": call.id, "content": result, "is_error": True}
                        )
                        yield ended
                    else:
                        async with aclosing(self._execute(call)) as execution:
                            async for execution_event in execution:
                                yield execution_event
                pending = ()
                yield Event("turn_end", {"turn": turn, "stop_reason": terminal.stop_reason})
                if terminal.stop_reason == "length" and not calls:
                    yield Event("error", {"message": "Model output reached its token limit"})
                    yield Event("agent_end", {"status": "error"})
                    return
                if calls or self._steering:
                    continue
                if self._follow_ups:
                    self._steering.append(self._follow_ups.popleft())
                    continue
                yield Event("agent_end", {"status": "complete"})
                return
            yield Event("error", {"message": f"Reached max_turns={self.max_turns}"})
            yield Event("agent_end", {"status": "max_turns"})
        except Exception as exc:
            if streaming:
                streaming = False
                yield self._append(Message("assistant", "".join(partial), stop_reason="error"))
            yield Event("error", {"message": str(exc)})
            yield Event("agent_end", {"status": "error"})
        finally:
            if streaming:
                self.messages.append(Message("assistant", "".join(partial), stop_reason="aborted"))
            # 取消发生在工具调用中间时，必须补齐未完成工具的结果；恢复绝不重放副作用。
            completed = {m.tool_call_id for m in self.messages[batch_start:] if m.role == "tool"}
            for call in pending:
                if call.id not in completed:
                    self.messages.append(
                        Message(
                            "tool",
                            "Interrupted. Side effects may have occurred; inspect state before retrying.",
                            tool_call_id=call.id,
                            name=call.name,
                            is_error=True,
                        )
                    )
            self.is_running = False
            self.clear_queues()
