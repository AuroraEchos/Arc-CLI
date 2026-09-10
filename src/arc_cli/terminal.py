"""提供 Arc CLI 克制、可组合的终端用户界面。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from arc_cli import __version__
from arc_cli.runtime import ArcSession
from arc_cli.types import Event, JsonObject, Message

RESET = "\x1b[0m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
DIM = "\x1b[2m"

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

BANNER = """ █████╗  ██████╗   ██████╗
██╔══██╗ ██╔══██╗ ██╔════╝
███████║ ██████╔╝ ██║
██╔══██║ ██╔══██╗ ██║
██║  ██║ ██║  ██║ ╚██████╗
╚═╝  ╚═╝ ╚═╝  ╚═╝  ╚═════╝"""


def safe_terminal(text: str) -> str:
    """转义可能影响终端控制状态的不可打印字符。"""

    return "".join(
        char if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159) else f"\\x{ord(char):02x}"
        for char in text
    )


def display_path(path: Path | None, cwd: Path) -> str:
    """优先以相对于工作目录的形式展示路径。"""

    if path is None:
        return "(memory)"
    try:
        return str(path.relative_to(cwd))
    except ValueError:
        return str(path)


def _one_line(value: object, width: int) -> str:
    """将任意值压缩为适合事件标题的一行文本。"""

    text = " ".join(safe_terminal(str(value)).split())
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


@dataclass(frozen=True)
class ToolSnapshot:
    """保存最近一次工具调用及其完整结果。"""

    call_id: str
    name: str
    arguments: JsonObject
    content: str
    is_error: bool


class Renderer:
    """将运行时事件渲染为安静的终端 UI 或 JSONL。"""

    def __init__(
        self,
        mode: str,
        *,
        color: bool = True,
        interactive: bool = True,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ):
        """配置输出模式、颜色策略和目标流。"""

        self.mode = mode
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        term = os.environ.get("TERM", "")
        self.color = color and "NO_COLOR" not in os.environ and self.stdout.isatty() and term != "dumb"
        self.dynamic = interactive and self.stderr.isatty() and term != "dumb"
        self.interactive = interactive
        self.assistant_started = False
        self._message_buffer = ""
        self.state = "idle"
        self.turn: int | None = None
        self.pending_tool: str | None = None
        self.last_tool: ToolSnapshot | None = None
        self._calls: dict[str, tuple[str, JsonObject]] = {}
        self._spinner_task: asyncio.Task[None] | None = None
        self._spinner_label: str | None = None
        self._spinner_frame = 0
        self._invalidate: Callable[[], None] | None = None

    def bind_prompt(self, invalidate: Callable[[], None]) -> None:
        """将活动状态绑定到 prompt_toolkit 的重绘机制。"""

        self._invalidate = invalidate

    def bind_streams(self, stdout: TextIO, stderr: TextIO) -> None:
        """切换到 prompt_toolkit 提供的安全输出代理。"""

        self.stdout = stdout
        self.stderr = stderr

    def bottom_toolbar(self) -> str:
        """返回 prompt_toolkit 底部活动状态栏的格式化内容。"""

        if self._spinner_label is None:
            return ""
        frame = SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)]
        return f"{frame} {self._spinner_label}"

    def input_prompt(self) -> str:
        """仅在当前任务真正结束后显示下一条输入提示。"""

        return "arc ▸ " if self.state == "idle" else ""

    def paint(self, text: str, style: str) -> str:
        """在启用颜色时为文本应用单个 ANSI 样式。"""

        return f"{style}{text}{RESET}" if self.color else text

    def start_activity(self, label: str) -> None:
        """在交互式 TTY 的同一行启动轻量状态动画。"""

        self.stop_activity()
        self._spinner_label = label
        self._spinner_frame = 0
        if not self.dynamic:
            return
        self._redraw_activity()
        self._spinner_task = asyncio.create_task(self._animate(label))

    def stop_activity(self) -> None:
        """停止状态动画并清除其占用的终端行。"""

        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None
        had_activity = self._spinner_label is not None
        if self.dynamic and had_activity and self._invalidate is None:
            self.stderr.write("\r\x1b[2K")
            self.stderr.flush()
        self._spinner_label = None
        if had_activity and self._invalidate is not None:
            self._invalidate()

    async def _animate(self, label: str) -> None:
        """周期性刷新 spinner，不产生新的终端行。"""

        try:
            while True:
                await asyncio.sleep(0.08)
                if self._spinner_label != label:
                    return
                self._spinner_frame += 1
                self._redraw_activity()
        except asyncio.CancelledError:
            return

    def _redraw_activity(self) -> None:
        """通过 prompt 重绘或直接 TTY 写入刷新活动状态。"""

        if self._invalidate is not None:
            self._invalidate()
            return
        if self._spinner_label is None:
            return
        frame = SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)]
        label = self._spinner_label
        self.stderr.write(f"\r\x1b[2K{self.paint(frame, CYAN)} {self.paint(label, DIM)}")
        self.stderr.flush()

    def emit(self, event: Event) -> None:
        """更新终端状态并渲染单个 Arc 运行时事件。"""

        if self.mode == "json":
            print(
                json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False),
                file=self.stdout,
                flush=True,
            )
            return
        if event.type == "agent_start":
            self.state = "running"
        elif event.type == "turn_start":
            self.turn = int(event.data["turn"])
            self.pending_tool = None
            self.start_activity("thinking")
        elif event.type == "message_start":
            self.assistant_started = False
            self._message_buffer = ""
        elif event.type == "message_update":
            self._message_buffer += safe_terminal(str(event.data["delta"]))
            self._flush_complete_message_lines()
        elif event.type == "message_end" and event.data["message"]["role"] == "assistant":
            self._flush_message_tail()
            self.stop_activity()
        elif event.type == "tool_execution_start":
            self.stop_activity()
            call = event.data["tool_call"]
            call_id = str(call["id"])
            name = str(call["name"])
            arguments = dict(call["arguments"])
            self._calls[call_id] = (name, arguments)
            self.pending_tool = name
            self.start_activity(f"running {name}")
        elif event.type == "tool_execution_update":
            # 工具的完整输出保留在最终结果中；默认界面只显示动态状态，避免刷屏。
            return
        elif event.type == "tool_execution_end":
            self.stop_activity()
            call_id = str(event.data["call_id"])
            name, arguments = self._calls.pop(call_id, (self.pending_tool or "tool", {}))
            snapshot = ToolSnapshot(
                call_id,
                name,
                arguments,
                str(event.data["content"]),
                bool(event.data["is_error"]),
            )
            self.last_tool = snapshot
            self.pending_tool = None
            print(self._tool_block(snapshot), file=self.stderr, flush=True)
        elif event.type == "error":
            self.stop_activity()
            print(self.paint(f"error │ {safe_terminal(str(event.data['message']))}", RED), file=self.stderr)
        elif event.type == "agent_end":
            self.stop_activity()
            self.state = "idle"
            self.pending_tool = None

    def _flush_complete_message_lines(self) -> None:
        """仅向输出代理提交完整行，避免 prompt 重绘覆盖流式片段。"""

        while "\n" in self._message_buffer:
            line, self._message_buffer = self._message_buffer.split("\n", 1)
            self._write_message_line(line)

    def _flush_message_tail(self) -> None:
        """在消息结束时提交最后一个没有换行符的片段。"""

        if self._message_buffer:
            self._write_message_line(self._message_buffer)
        self._message_buffer = ""

    def _write_message_line(self, line: str) -> None:
        """原子地写入一行助手文本，并为交互界面保留轻量身份标记。"""

        if not self.interactive:
            print(line, file=self.stdout, flush=True)
            self.assistant_started = True
            return
        prefix = self.paint("arc", CYAN) + " │ " if not self.assistant_started else "    │ "
        print(prefix + line, file=self.stdout, flush=True)
        self.assistant_started = True

    def _tool_block(self, snapshot: ToolSnapshot) -> str:
        """生成工具完成事件的紧凑三行块。"""

        width = max(48, min(100, shutil.get_terminal_size((88, 24)).columns))
        title = self._tool_title(snapshot, width - 12)
        top = self.paint(f"╭─ {snapshot.name} · {title}", CYAN)
        detail = self._tool_detail(snapshot, width - 4)
        body = "\n".join(f"│ {line}" for line in detail)
        status = "error" if snapshot.is_error else "done"
        bottom = self.paint(f"╰─ {status}", RED if snapshot.is_error else GREEN)
        return "\n".join(part for part in (top, body, bottom) if part)

    @staticmethod
    def _tool_title(snapshot: ToolSnapshot, width: int) -> str:
        if snapshot.name == "bash":
            value = snapshot.arguments.get("command", "command")
        elif snapshot.name in ("read", "write", "edit"):
            value = snapshot.arguments.get("path", "file")
        else:
            value = json.dumps(snapshot.arguments, ensure_ascii=False, sort_keys=True)
        return _one_line(value, width)

    @staticmethod
    def _tool_detail(snapshot: ToolSnapshot, width: int) -> list[str]:
        """提取比原始工具输出更适合默认显示的摘要。"""

        content = safe_terminal(snapshot.content)
        if snapshot.name == "read":
            numbered = [line for line in content.splitlines() if re.match(r"^\d+: ", line)]
            read_detail = [f"returned {len(numbered)} numbered lines"]
            more = re.search(r"\[More lines: use offset=(\d+)\]", content)
            if more:
                read_detail.append(f"more available from line {more.group(1)}")
            return read_detail
        if snapshot.name in ("write", "edit"):
            return ["file updated"] if not snapshot.is_error else [_one_line(content, width)]
        lines = [line for line in content.splitlines() if line.strip()]
        exit_line = next((line for line in reversed(lines) if line.startswith("[Exit code:")), None)
        payload = [line for line in lines if not line.startswith("[Exit code:")]
        detail: list[str] = []
        if exit_line:
            detail.append(exit_line.strip("[]"))
        if len(payload) <= 3:
            detail.extend(_one_line(line, width) for line in payload)
        elif payload:
            detail.extend(_one_line(line, width) for line in payload[:2])
            detail.append(f"… {len(payload) - 2} more lines; use /last-tool")
        return detail or ["no output"]

    def show_banner(self, session: ArcSession) -> None:
        """展示品牌标识和当前运行配置。"""

        print(self.paint(BANNER, CYAN), file=self.stdout)
        print(file=self.stdout)
        print(f"{self.paint('Arc CLI', CYAN)} {__version__}", file=self.stdout)
        print(self.paint("terminal-native agent runtime", DIM), file=self.stdout)
        print(file=self.stdout)
        self._metadata("model", session.agent.provider.model)
        self._metadata("cwd", str(session.agent.cwd))
        self._metadata("session", display_path(session.store.path, session.agent.cwd))
        self._metadata("tools", " ".join(session.agent.tools.names()) or "(none)")
        print(file=self.stdout)
        print(self.paint("History is not context. Side effects are not memories.", DIM), file=self.stdout)
        self._metadata("note", "tools use local user permissions; cwd is not a sandbox")
        print(file=self.stdout, flush=True)

    def show_status(self, session: ArcSession, *, busy: bool) -> None:
        """展示当前模型、会话分支和运行状态。"""

        print(self.paint("Arc CLI", CYAN), file=self.stdout)
        print(self.paint("─" * 44, DIM), file=self.stdout)
        self._metadata("model", session.agent.provider.model)
        self._metadata("cwd", str(session.agent.cwd))
        self._metadata("session", display_path(session.store.path, session.agent.cwd))
        self._metadata("branch", session.store.leaf[:8] if session.store.leaf else "root")
        self._metadata("messages", str(len(session.agent.messages)))
        self._metadata("tools", " ".join(session.agent.tools.names()) or "(none)")
        self._metadata("state", "running" if busy else self.state)
        if busy and self.turn is not None:
            self._metadata("turn", f"{self.turn} / {session.agent.max_turns}")
        if busy and self.pending_tool:
            self._metadata("pending", self.pending_tool)

    def show_history(self, messages: list[Message]) -> None:
        """以分层摘要形式展示当前分支消息。"""

        calls: dict[str, tuple[str, JsonObject]] = {}
        for index, message in enumerate(messages, 1):
            for call in message.tool_calls:
                calls[call.id] = (call.name, call.arguments)
            label: str = message.role
            detail = message.content
            if message.role == "tool":
                name, arguments = calls.get(message.tool_call_id or "", (message.name or "tool", {}))
                label = f"tool:{name}" + (" error" if message.is_error else "")
                subject = arguments.get("command") or arguments.get("path")
                suffix = f" · {_one_line(subject, 72)}" if subject else ""
                detail = f"{len(message.content):,} chars{suffix}"
            print(f"[{index}] {self.paint(label, CYAN if message.role == 'tool' else DIM)}", file=self.stdout)
            for line in (detail or "(empty)").splitlines()[:8]:
                print(f"    {_one_line(line, 100)}", file=self.stdout)
            if len((detail or "").splitlines()) > 8:
                print("    …", file=self.stdout)
            print(file=self.stdout)

    def show_last_tool(self, limit: int | None = None) -> None:
        """展示最近一次工具的完整结果或指定数量的开头行。"""

        if self.last_tool is None:
            print(self.paint("No tool result in this run.", DIM), file=self.stdout)
            return
        snapshot = self.last_tool
        print(self.paint(f"{snapshot.name} · {self._tool_title(snapshot, 88)}", CYAN), file=self.stdout)
        print(self.paint("─" * 44, DIM), file=self.stdout)
        lines = safe_terminal(snapshot.content).splitlines()
        selected = lines if limit is None else lines[:limit]
        print("\n".join(selected) or "(empty)", file=self.stdout)
        if limit is not None and len(lines) > limit:
            print(self.paint(f"… {len(lines) - limit} more lines", DIM), file=self.stdout)

    def clear(self) -> None:
        """清除交互式终端屏幕；非 TTY 输出保持可审计。"""

        if self.stdout.isatty():
            self.stdout.write("\x1b[2J\x1b[H")
            self.stdout.flush()

    def _metadata(self, key: str, value: str) -> None:
        print(f"{self.paint(key.ljust(9), DIM)}{safe_terminal(value)}", file=self.stdout)
