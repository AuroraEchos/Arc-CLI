"""提供 Arc CLI 克制、可组合的终端用户界面。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import TextIO

from arc_cli import __version__
from arc_cli.runtime import ArcSession
from arc_cli.types import Event, JsonObject, Message, Usage

RESET = "\x1b[0m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
ITALIC = "\x1b[3m"
STRIKE = "\x1b[9m"
UNDERLINE = "\x1b[4m"

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

BANNER = """ █████╗  ██████╗   ██████╗
██╔══██╗ ██╔══██╗ ██╔════╝
███████║ ██████╔╝ ██║
██╔══██║ ██╔══██╗ ██║
██║  ██║ ██║  ██║ ╚██████╗
╚═╝  ╚═╝ ╚═╝  ╚═╝  ╚═════╝"""

INLINE_MARKDOWN = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold_star>\*\*[^*\n]+\*\*)"
    r"|(?P<bold_under>__[^_\n]+__)"
    r"|(?P<strike>~~[^~\n]+~~)"
    r"|(?P<link>\[[^\]\n]+\]\([^\s)\n]+\))"
    r"|(?P<italic_star>(?<!\*)\*[^*\n]+\*(?!\*))"
    r"|(?P<italic_under>(?<!_)_[^_\n]+_(?!_))"
)


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
    return _clip_cells(text, width)


def _display_width(text: str) -> int:
    """Approximate terminal cell width without depending on locale state."""

    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def _clip_cells(text: str, width: int, *, tail: bool = False) -> str:
    """Clip text by terminal cells, optionally retaining its newest tail."""

    if width < 2:
        return ""
    if _display_width(text) <= width:
        return text
    budget = width - 1
    selected = []
    used = 0
    characters = reversed(text) if tail else iter(text)
    for char in characters:
        cells = _display_width(char)
        if used + cells > budget:
            break
        selected.append(char)
        used += cells
    if tail:
        return "…" + "".join(reversed(selected))
    return "".join(selected) + "…"


class TerminalMarkdown:
    """Render common line-oriented Markdown without owning terminal layout."""

    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.in_fence = False

    def reset(self) -> None:
        self.in_fence = False

    @staticmethod
    def _style(text: str, style: str) -> str:
        return f"{style}{text}{RESET}"

    def _inline(self, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            token = match.group(0)
            kind = match.lastgroup
            if kind == "code":
                return self._style(token[1:-1], YELLOW)
            if kind in ("bold_star", "bold_under"):
                return self._style(token[2:-2], BOLD)
            if kind == "strike":
                return self._style(token[2:-2], STRIKE)
            if kind == "link":
                label, url = token[1:].split("](", 1)
                return self._style(label, UNDERLINE) + self._style(f" ({url[:-1]})", DIM)
            return self._style(token[1:-1], ITALIC)

        return INLINE_MARKDOWN.sub(replace, text)

    def render_line(self, line: str) -> str:
        """Render one complete line while retaining fenced-code state."""

        if not self.enabled:
            return line
        fence = re.match(r"^\s*```\s*([^`]*)$", line)
        if fence:
            if self.in_fence:
                self.in_fence = False
                return self._style("╰─", DIM)
            self.in_fence = True
            language = fence.group(1).strip()
            label = f"╭─ code · {language}" if language else "╭─ code"
            return self._style(label, DIM)
        if self.in_fence:
            return self._style("│ ", DIM) + line

        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            return self._style(self._inline(heading.group(1)), BOLD + CYAN)
        if re.match(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$", line):
            return self._style("─" * 48, DIM)

        quote = re.match(r"^(\s*)>\s?(.*)$", line)
        if quote:
            return quote.group(1) + self._style("│ ", DIM) + self._inline(quote.group(2))

        bullet = re.match(r"^(\s*)[-+*]\s+(.*)$", line)
        if bullet:
            body = bullet.group(2)
            checkbox = re.match(r"^\[([ xX])\]\s+(.*)$", body)
            marker = "•"
            if checkbox:
                marker = "☑" if checkbox.group(1).lower() == "x" else "☐"
                body = checkbox.group(2)
            return bullet.group(1) + self._style(marker, CYAN) + " " + self._inline(body)

        ordered = re.match(r"^(\s*)(\d+[.)])\s+(.*)$", line)
        if ordered:
            return (
                ordered.group(1) + self._style(ordered.group(2), CYAN) + " " + self._inline(ordered.group(3))
            )
        return self._inline(line)


@dataclass(frozen=True)
class ToolSnapshot:
    """保存最近一次工具调用及其完整结果。"""

    call_id: str
    name: str
    arguments: JsonObject
    content: str
    is_error: bool
    status: str = "complete"
    duration_ms: int = 0


class Renderer:
    """将运行时事件渲染为安静的终端 UI 或 JSONL。"""

    def __init__(
        self,
        mode: str,
        *,
        color: bool = True,
        interactive: bool = True,
        markdown: bool = True,
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
        self.markdown = TerminalMarkdown(enabled=markdown and interactive and self.color)
        self.assistant_started = False
        self._message_buffer = ""
        self._message_preview_task: asyncio.Task[None] | None = None
        self._message_ends_newline = False
        self.state = "idle"
        self.turn: int | None = None
        self.pending_tool: str | None = None
        self.last_tool: ToolSnapshot | None = None
        self._calls: dict[str, tuple[str, JsonObject]] = {}
        self._spinner_task: asyncio.Task[None] | None = None
        self._spinner_label: str | None = None
        self._spinner_frame = 0
        self._activity_started: float | None = None
        self._invalidate: Callable[[], None] | None = None
        self.last_usage: Usage | None = None
        self.task_input_tokens = 0
        self.task_output_tokens = 0

    def bind_prompt(self, invalidate: Callable[[], None]) -> None:
        """将活动状态绑定到 prompt_toolkit 的重绘机制。"""

        self._invalidate = invalidate

    def bind_streams(self, stdout: TextIO, stderr: TextIO) -> None:
        """切换到 prompt_toolkit 提供的安全输出代理。"""

        self.stdout = stdout
        self.stderr = stderr

    def bottom_toolbar(self) -> str:
        """返回 prompt_toolkit 底部活动状态栏的格式化内容。"""

        columns = max(20, shutil.get_terminal_size((88, 24)).columns)
        usage = self._usage_label()
        if self._spinner_label is None:
            return _clip_cells(usage, columns)
        frame = SPINNER_FRAMES[self._spinner_frame % len(SPINNER_FRAMES)]
        elapsed = monotonic() - self._activity_started if self._activity_started is not None else 0
        label = _clip_cells(self._spinner_label, max(8, min(54, columns - 14)))
        status = f"{frame} {label} · {elapsed:.1f}s"
        if self._message_buffer:
            width = columns - _display_width(status) - 3
            if width < 8:
                return _clip_cells(status, columns)
            preview = _clip_cells(" ".join(self._message_buffer.split()), width, tail=True)
            return f"{status} │ {preview}"
        if not usage:
            return _clip_cells(status, columns)
        available = columns - _display_width(status) - 3
        if available < 12:
            return _clip_cells(status, columns)
        return f"{status} │ {_clip_cells(usage, available)}"

    def _usage_label(self) -> str:
        """Return the latest request usage plus the running task total."""

        if self.last_usage is None:
            return ""
        latest_total = self.last_usage.input_tokens + self.last_usage.output_tokens
        task_total = self.task_input_tokens + self.task_output_tokens
        label = (
            f"tokens · {self.last_usage.input_tokens:,} in · "
            f"{self.last_usage.output_tokens:,} out · {latest_total:,} total"
        )
        if task_total != latest_total:
            label += f" · task {task_total:,}"
        return label

    def input_prompt(self) -> str:
        """仅在当前任务真正结束后显示下一条输入提示。"""

        marker = "›" if self.state == "idle" else "↳"
        return f"\n{self._input_rule()}\n\n{marker} "

    def finish_input(self) -> None:
        """Close the framed input area with vertical breathing room."""

        print(f"\n{self.paint(self._input_rule(), DIM)}\n", file=self.stdout, flush=True)

    @staticmethod
    def _input_rule() -> str:
        columns = shutil.get_terminal_size((88, 24)).columns
        return "─" * max(12, min(72, columns - 2))

    def paint(self, text: str, style: str) -> str:
        """在启用颜色时为文本应用单个 ANSI 样式。"""

        return f"{style}{text}{RESET}" if self.color else text

    def start_activity(self, label: str) -> None:
        """在交互式 TTY 的同一行启动轻量状态动画。"""

        self.stop_activity()
        self._spinner_label = label
        self._spinner_frame = 0
        self._activity_started = monotonic()
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
        self._activity_started = None
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
        label = self.bottom_toolbar()[2:]
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
            self.last_usage = None
            self.task_input_tokens = 0
            self.task_output_tokens = 0
        elif event.type == "turn_start":
            self.turn = int(event.data["turn"])
            self.pending_tool = None
            self.start_activity("thinking")
        elif event.type == "message_start":
            self.assistant_started = False
            self.markdown.reset()
            self._message_buffer = ""
            self._message_ends_newline = False
            self._cancel_message_preview()
        elif event.type == "message_update":
            delta = safe_terminal(str(event.data["delta"]))
            if not self.interactive:
                self._write_noninteractive_delta(delta)
            else:
                self._message_buffer += delta
                if self._spinner_label == "thinking":
                    self._spinner_label = "responding"
                self._flush_complete_message_lines()
                self._schedule_message_preview()
        elif event.type == "message_end" and event.data["message"]["role"] == "assistant":
            raw_usage = event.data["message"].get("usage")
            if isinstance(raw_usage, dict):
                usage = Usage(
                    int(raw_usage.get("input_tokens", 0)),
                    int(raw_usage.get("output_tokens", 0)),
                )
                self.last_usage = usage
                self.task_input_tokens += usage.input_tokens
                self.task_output_tokens += usage.output_tokens
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
            title = self._tool_title(ToolSnapshot(call_id, name, arguments, "", False), 54)
            self.start_activity(f"{name} · {title}")
        elif event.type == "tool_execution_authorization":
            # Authorization is reflected by the final status. Keep the active row quiet.
            return
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
                str(event.data.get("status", "error" if event.data["is_error"] else "complete")),
                int(event.data.get("duration_ms", 0)),
            )
            self.last_tool = snapshot
            self.pending_tool = None
            print(self._tool_line(snapshot), file=self.stderr, flush=True)
        elif event.type == "error":
            self.stop_activity()
            print(self.paint(f"error │ {safe_terminal(str(event.data['message']))}", RED), file=self.stderr)
        elif event.type == "agent_end":
            self.stop_activity()
            self.state = "idle"
            self.pending_tool = None

    def _flush_complete_message_lines(self) -> None:
        """Atomically submit complete lines while retaining a partial tail."""

        if "\n" not in self._message_buffer:
            return
        complete, self._message_buffer = self._message_buffer.rsplit("\n", 1)
        self._write_message_lines(complete.split("\n"))

    def _schedule_message_preview(self) -> None:
        """Throttle prompt-managed preview redraws for the incomplete tail."""

        if self._message_preview_task is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._message_preview_task = asyncio.create_task(self._preview_after_delay())

    async def _preview_after_delay(self) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(0.05)
            if self._invalidate is not None:
                self._invalidate()
        except asyncio.CancelledError:
            return
        finally:
            if self._message_preview_task is current:
                self._message_preview_task = None

    def _cancel_message_preview(self) -> None:
        if self._message_preview_task is not None:
            self._message_preview_task.cancel()
            self._message_preview_task = None

    def _flush_message_tail(self) -> None:
        """在消息结束时提交最后一个没有换行符的片段。"""

        self._cancel_message_preview()
        if not self.interactive:
            if self.assistant_started and not self._message_ends_newline:
                self.stdout.write("\n")
                self.stdout.flush()
            return
        if self._message_buffer:
            self._write_message_lines([self._message_buffer])
        self._message_buffer = ""

    def _write_noninteractive_delta(self, delta: str) -> None:
        """Stream directly when no prompt_toolkit application can repaint."""

        if not delta:
            return
        self.stdout.write(delta)
        self.stdout.flush()
        self.assistant_started = True
        self._message_ends_newline = delta.endswith("\n")

    def _write_message_lines(self, lines: list[str]) -> None:
        """Write complete interactive lines in one StdoutProxy transaction."""

        rendered = []
        for line in lines:
            prefix = self.paint("arc", CYAN) + " │ " if not self.assistant_started else "    │ "
            rendered.append(prefix + self.markdown.render_line(line))
            self.assistant_started = True
        self.stdout.write("\n".join(rendered) + "\n")
        self.stdout.flush()

    def _tool_line(self, snapshot: ToolSnapshot) -> str:
        """Render one quiet, durable completion summary."""

        width = max(48, min(100, shutil.get_terminal_size((88, 24)).columns))
        title = self._tool_title(snapshot, max(12, width // 2))
        detail = self._tool_detail(snapshot, max(12, width // 2))
        summary = detail[-1] if detail else ("error" if snapshot.is_error else "done")
        elapsed = f"{snapshot.duration_ms / 1000:.1f}s"
        if snapshot.status == "blocked":
            symbol, style = "!", YELLOW
        elif snapshot.is_error:
            symbol, style = "✗", RED
        else:
            symbol, style = "✓", GREEN
        return self.paint(f"{symbol} {snapshot.name}", style) + f" · {title} · {summary} · {elapsed}"

    @staticmethod
    def _tool_title(snapshot: ToolSnapshot, width: int) -> str:
        if snapshot.name == "bash":
            value = snapshot.arguments.get("command", "command")
        elif snapshot.name in ("read", "write", "edit"):
            value = snapshot.arguments.get("path", "file")
        elif snapshot.name == "apply_patch":
            operations = snapshot.arguments.get("operations", [])
            value = f"{len(operations)} file operations"
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
            return ["; ".join(read_detail)]
        if snapshot.name in ("write", "edit"):
            return ["file updated"] if not snapshot.is_error else [_one_line(content, width)]
        if snapshot.name == "apply_patch":
            if snapshot.is_error:
                return [_one_line(content, width)]
            operations = snapshot.arguments.get("operations", [])
            action_counts = {
                action: sum(operation.get("type") == action for operation in operations)
                for action in ("add", "update", "delete")
            }
            summary = ", ".join(f"{count} {action}" for action, count in action_counts.items() if count)
            return [summary or "patch applied"]
        lines = [line for line in content.splitlines() if line.strip()]
        exit_line = next((line for line in reversed(lines) if line.startswith("[Exit code:")), None)
        payload = [line for line in lines if not line.startswith("[Exit code:")]
        if snapshot.is_error and payload:
            return [_one_line(payload[-1], width)]
        test_summary = next(
            (
                line
                for line in reversed(payload)
                if re.search(r"\b(?:passed|failed|errors?|tests?)\b", line, re.IGNORECASE)
            ),
            None,
        )
        if test_summary:
            return [_one_line(test_summary, width)]
        if payload:
            return [_one_line(payload[-1], width)]
        if exit_line:
            return [exit_line.strip("[]")]
        return ["no output"]

    def show_banner(self, session: ArcSession) -> None:
        """Show the compact everyday startup identity."""

        cwd = display_path(session.agent.cwd, Path.cwd())
        line = f"ARC {__version__} · {session.agent.provider.model} · {cwd}"
        print(self.paint(line, CYAN), file=self.stdout)
        print(self.paint("› task   ↳ steer   /help commands", DIM), file=self.stdout)
        print(file=self.stdout, flush=True)

    def show_status(self, session: ArcSession, *, busy: bool) -> None:
        """展示当前模型、会话分支和运行状态。"""

        print(self.paint("ARC", CYAN), file=self.stdout)
        print(self.paint("─" * 44, DIM), file=self.stdout)
        self._metadata("model", session.agent.provider.model)
        self._metadata("cwd", str(session.agent.cwd))
        self._metadata("session", display_path(session.store.path, session.agent.cwd))
        self._metadata("branch", session.store.leaf[:8] if session.store.leaf else "root")
        self._metadata("messages", str(len(session.agent.messages)))
        self._metadata("tools", " ".join(session.agent.tools.names()) or "(none)")
        self._metadata("policy", session.agent.policy.mode)
        self._metadata("effects", " ".join(sorted(session.agent.policy.allowed)) or "(none)")
        thinking = getattr(session.agent.provider, "thinking", None)
        effort = getattr(session.agent.provider, "reasoning_effort", None)
        if thinking is not None:
            self._metadata("thinking", str(thinking))
            self._metadata("effort", str(effort or "default"))
        if self.last_usage is not None:
            self._metadata("usage", self._usage_label().removeprefix("tokens · "))
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
