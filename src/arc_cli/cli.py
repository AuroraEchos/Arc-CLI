"""实现 Arc CLI 的参数解析、交互循环与终端事件渲染。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import aclosing
from pathlib import Path

from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from arc_cli import __version__
from arc_cli.agent import DEFAULT_SYSTEM_PROMPT, Arc
from arc_cli.providers import OpenAIProvider
from arc_cli.runtime import ArcSession
from arc_cli.session import SessionStore, latest_session_path, new_session_path
from arc_cli.terminal import DIM, GREEN, RED, YELLOW, Renderer, safe_terminal
from arc_cli.tools import ToolRegistry, create_builtin_tools
from arc_cli.types import Event, Provider

COMMANDS = (
    "/abort",
    "/branch",
    "/clear",
    "/compact",
    "/follow-up",
    "/help",
    "/history",
    "/last-tool",
    "/model",
    "/new",
    "/quit",
    "/session",
    "/status",
    "/steer",
    "/tools",
    "/tree",
)

HELP = """普通输入开始任务；任务运行时的普通输入会在下一轮模型请求前补充指令。
/help              显示帮助
/abort             取消正在运行的任务（不会回滚已完成的修改）
/steer TEXT        下一轮注入指令
/follow-up TEXT    当前任务自然结束后再处理
/status            查看模型、会话、分支、工具与运行状态
/last-tool [N]     查看最近一次完整工具结果，或前 N 行
/history           查看当前分支的分层消息摘要
/tree              查看会话树，● 为当前节点
/branch ID         切换到节点 ID 前缀；不回滚磁盘文件
/new               从空上下文开始（旧历史仍保留在树中）
/compact [N]       用模型总结旧消息，保留最近 N 个用户轮次，默认 2
/session           查看会话文件
/tools             查看启用的工具
/model [NAME]      查看/切换模型（仅空闲时）
/clear             清除终端屏幕
/quit              退出；Ctrl-D 同效；Ctrl-C 取消当前任务
"""


def parser() -> argparse.ArgumentParser:
    """创建并返回 Arc CLI 参数解析器。"""

    result = argparse.ArgumentParser(description="Arc CLI: a terminal-native agent runtime")
    result.add_argument("prompt", nargs="*", help="initial prompt")
    result.add_argument("-p", "--print", dest="print_mode", action="store_true", help="run once and exit")
    result.add_argument("--mode", choices=("text", "json"), default="text", help="JSONL event output")
    result.add_argument("--model")
    result.add_argument("--base-url")
    result.add_argument("--cwd", type=Path, help="tool working directory (not a sandbox)")
    sessions = result.add_mutually_exclusive_group()
    sessions.add_argument("--session", type=Path, help="open existing or create a named JSONL session")
    sessions.add_argument("-c", "--continue", dest="continue_session", action="store_true")
    sessions.add_argument("--no-session", action="store_true", help="keep history in memory only")
    result.add_argument("--tools", default="read,write,edit,bash", help="comma-separated allowlist, or none")
    result.add_argument("--max-turns", type=int, default=20)
    result.add_argument("--max-output-tokens", type=int, default=4096)
    result.add_argument("--timeout", type=float, default=60, help="model network timeout in seconds")
    result.add_argument("--system", default="", help="additional system instructions")
    result.add_argument("--no-context", action="store_true", help="do not read cwd/AGENTS.md")
    result.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    result.add_argument("--version", action="version", version=f"Arc CLI {__version__}")
    return result


async def consume(session: ArcSession, prompt: str, renderer: Renderer) -> int:
    """消费一次 Arc 任务，并将最终状态映射为进程退出码。"""

    status = "error"
    try:
        async with aclosing(session.run(prompt)) as events:
            async for event in events:
                renderer.emit(event)
                if event.type == "agent_end":
                    status = event.data["status"]
    except asyncio.CancelledError:
        renderer.emit(Event("agent_end", {"status": "aborted"}))
        raise
    return 0 if status == "complete" else 1


async def interactive(session: ArcSession, initial: str, renderer: Renderer) -> int:
    """运行支持命令、steering 和 follow-up 的交互式终端会话。"""

    history: History = InMemoryHistory()
    if session.store.path is not None:
        history_path = session.agent.cwd / ".arc" / "input-history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_path))
    prompt_session: PromptSession[str] = PromptSession(
        history=history,
        completer=WordCompleter(COMMANDS, WORD=True),
        complete_while_typing=False,
        enable_history_search=True,
        bottom_toolbar=renderer.bottom_toolbar,
        style=Style.from_dict(
            {
                "bottom-toolbar": (
                    "noreverse bg:default #6c7086" if renderer.color else "noreverse bg:default"
                ),
            }
        ),
    )
    renderer.bind_prompt(prompt_session.app.invalidate)
    task: asyncio.Task[int] | None = None

    async def abort() -> None:
        nonlocal task
        if task:
            if not task.done():
                task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    print(safe_terminal(f"[error] {result}"), file=sys.stderr)
            task = None

    renderer.show_banner(session)
    with patch_stdout(raw=True):
        renderer.bind_streams(sys.stdout, sys.stdout)
        try:
            if initial:
                task = asyncio.create_task(consume(session, initial, renderer))
            while True:
                try:
                    text = (await prompt_session.prompt_async("arc ▸ ")).strip()
                except KeyboardInterrupt:
                    await abort()
                    print(renderer.paint("aborted", YELLOW))
                    continue
                except EOFError:
                    break
                if not text:
                    continue
                if task and task.done():
                    await abort()
                command, _, argument = text.partition(" ")
                busy = task is not None and not task.done()
                try:
                    if command in ("/quit", "/exit"):
                        break
                    if command == "/abort":
                        await abort()
                    elif command == "/help":
                        print(HELP)
                    elif command == "/status":
                        renderer.show_status(session, busy=busy)
                    elif command == "/last-tool":
                        if argument:
                            limit = int(argument)
                            if limit < 1:
                                raise ValueError("Line limit must be positive")
                        else:
                            limit = None
                        renderer.show_last_tool(limit)
                    elif command == "/clear":
                        renderer.clear()
                    elif command == "/session":
                        print(session.store.path or "(memory)")
                    elif command == "/tools":
                        print(", ".join(session.agent.tools.names()) or "(none)")
                    elif command == "/history":
                        renderer.show_history(session.agent.messages)
                    elif command == "/tree":
                        print(safe_terminal("\n".join(session.store.tree_lines())) or "(empty)")
                    elif command in ("/steer", "/follow-up"):
                        if not argument.strip():
                            raise ValueError("Provide text after the command")
                        if not busy:
                            task = asyncio.create_task(consume(session, argument, renderer))
                        elif command == "/steer":
                            session.agent.steer(argument)
                        else:
                            session.agent.follow_up(argument)
                    elif command in ("/new", "/branch", "/compact", "/model"):
                        if busy:
                            raise ValueError("Wait for completion or /abort first")
                        if command == "/new":
                            session.branch("root")
                        elif command == "/branch":
                            if not argument:
                                raise ValueError("Usage: /branch ID")
                            session.branch(argument)
                        elif command == "/compact":
                            renderer.start_activity("compacting")
                            try:
                                await session.compact(int(argument) if argument else 2)
                            finally:
                                renderer.stop_activity()
                            print(renderer.paint("compacted │ original history retained", GREEN))
                        else:
                            if argument:
                                session.agent.provider.model = argument
                            print(session.agent.provider.model)
                    elif command.startswith("/"):
                        raise ValueError("Unknown command; use /help")
                    elif busy:
                        session.agent.steer(text)
                        print(renderer.paint("queued │ next model turn", DIM))
                    else:
                        task = asyncio.create_task(consume(session, text, renderer))
                except (ValueError, OSError, RuntimeError) as exc:
                    print(renderer.paint(f"error │ {safe_terminal(str(exc))}", RED), file=sys.stderr)
        finally:
            await abort()
    return 0


async def run(args: argparse.Namespace) -> int:
    """根据命令行参数组装并运行 Arc CLI。"""

    cwd = (args.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    load_dotenv(cwd / ".env", override=True)
    model = args.model or os.environ.get("MODEL")
    base_url = args.base_url or os.environ.get("BASE_URL", "https://api.openai.com/v1")
    key = os.environ.get("API_KEY", "")
    names = [] if args.tools in ("", "none") else args.tools.split(",")
    builtins = create_builtin_tools()
    unknown = set(names) - {tool.spec.name for tool in builtins}
    if unknown:
        raise ValueError(f"Unknown tools: {', '.join(sorted(unknown))}")
    if args.max_turns < 1 or args.max_output_tokens < 1 or args.timeout <= 0:
        raise ValueError("Limits must be positive")
    prompt = " ".join(args.prompt)
    one_shot = args.print_mode or args.mode == "json" or not sys.stdin.isatty()
    if not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        prompt = "\n\n".join(part for part in (prompt, piped) if part)
    if one_shot and not prompt:
        raise ValueError("A prompt is required (argument or stdin)")
    if not model:
        raise ValueError("Set MODEL in .env or pass --model")
    if base_url.rstrip("/") == "https://api.openai.com/v1" and not key:
        raise ValueError("Set API_KEY in .env")
    provider: Provider = OpenAIProvider(
        model=model,
        api_key=key,
        base_url=base_url,
        timeout=args.timeout,
        max_output_tokens=args.max_output_tokens,
    )
    store = None
    try:
        path = latest_session_path(cwd) if args.continue_session else args.session
        if path and path.exists():
            store = SessionStore.open(path)
            if args.cwd and cwd != store.cwd:
                raise ValueError("--cwd differs from saved session cwd; start a new session instead")
            cwd = store.cwd
            if not cwd.is_dir():
                raise ValueError("Saved session cwd no longer exists")
        else:
            store = SessionStore.create(cwd, None if args.no_session else (path or new_session_path(cwd)))
        system = f"{DEFAULT_SYSTEM_PROMPT}\nWorking directory: {cwd}\n{args.system}"
        context_path = cwd / "AGENTS.md"
        if not args.no_context and context_path.is_file():
            if context_path.stat().st_size > 32_768:
                raise ValueError("AGENTS.md exceeds 32 KiB; use --no-context or shorten it")
            system += "\nProject instructions (AGENTS.md):\n" + context_path.read_text(encoding="utf-8")
        agent = Arc(
            provider,
            ToolRegistry([tool for tool in builtins if tool.spec.name in names]),
            cwd=cwd,
            system_prompt=system,
            max_turns=args.max_turns,
        )
        session = ArcSession(agent, store)
        renderer = Renderer(args.mode, color=not args.no_color, interactive=not one_shot)
        if one_shot:
            return await consume(session, prompt, renderer)
        return await interactive(session, prompt, renderer)
    finally:
        if store:
            store.close()
        await provider.aclose()


def main() -> None:
    """运行 Arc CLI，并将应用结果转换为进程退出状态。"""

    args = parser().parse_args()
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    except (ValueError, OSError, RuntimeError) as exc:
        if args.mode == "json":
            print(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False))
        else:
            print(safe_terminal(f"arc: {exc}"), file=sys.stderr)
        code = 2
    raise SystemExit(code)
