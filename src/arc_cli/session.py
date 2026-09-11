"""实现基于追加式 JSONL 和 parent 指针的可分支会话存储。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from arc_cli.types import JsonObject, Message, ToolCall


class SessionStore:
    """持久化会话树，并维护当前分支的叶节点。"""

    def __init__(self, cwd: Path, path: Path | None = None):
        self.cwd = cwd.resolve()
        self.path = path.resolve() if path else None
        self.id = uuid4().hex
        self.entries: dict[str, JsonObject] = {}
        self.leaf: str | None = None
        self._file: TextIO | None = None

    @classmethod
    def create(cls, cwd: Path, path: Path | None = None) -> SessionStore:
        """创建内存会话或具有排他写入锁的新会话文件。"""

        store = cls(cwd, path)
        if store.path:
            store.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(store.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            store._file = os.fdopen(descriptor, "w+", encoding="utf-8", newline="\n")
            try:
                store._lock()
                store._write(
                    {
                        "type": "session",
                        "version": 1,
                        "id": store.id,
                        "cwd": str(store.cwd),
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
            except BaseException:
                store.close()
                raise
        return store

    @classmethod
    def open(cls, path: Path) -> SessionStore:
        """打开、校验并锁定已有会话文件。"""

        store = cls(Path.cwd(), path)
        store._file = path.open("r+", encoding="utf-8")
        try:
            store._lock()
            lines = store._file.readlines()
            if not lines:
                raise ValueError("Empty session")
            header = json.loads(lines[0])
            if not isinstance(header, dict) or header.get("type") != "session" or header.get("version") != 1:
                raise ValueError("Unsupported session format")
            if not isinstance(header.get("cwd"), str) or not isinstance(header.get("id"), str):
                raise ValueError("Invalid session header")
            store.cwd = Path(header["cwd"]).resolve()
            store.id = header["id"]
            for number, line in enumerate(lines[1:], 2):
                try:
                    entry = json.loads(line)
                    store._apply(entry)
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f"Invalid session line {number}; file left unchanged") from exc
            store._file.seek(0, os.SEEK_END)
            if not lines[-1].endswith("\n"):
                store._file.write("\n")
                store._file.flush()
            return store
        except BaseException:
            store.close()
            raise

    def _lock(self) -> None:
        assert self._file is not None
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Session is already open by another writer") from exc

    def _write(self, entry: JsonObject) -> None:
        if self.path is not None and self._file is None:
            raise ValueError("Session is closed")
        if self._file:
            self._file.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())

    def _apply(self, entry: JsonObject) -> None:
        if not isinstance(entry, dict):
            raise ValueError("Invalid session entry")
        if entry.get("type") == "cursor":
            leaf = entry.get("leaf")
            if leaf is not None and (not isinstance(leaf, str) or leaf not in self.entries):
                raise ValueError("Unknown cursor")
            self.leaf = leaf
            return
        if entry.get("type") not in ("message", "checkpoint"):
            raise ValueError("Unknown entry type")
        identifier, parent = entry.get("id"), entry.get("parent")
        if not isinstance(identifier, str) or not identifier or identifier in self.entries:
            raise ValueError("Invalid/duplicate entry id")
        if parent is not None and (not isinstance(parent, str) or parent not in self.entries):
            raise ValueError("Unknown parent")
        if entry["type"] == "message":
            if not isinstance(entry.get("message"), dict):
                raise ValueError("Invalid message entry")
            Message.from_dict(entry["message"])
        else:
            messages = entry.get("messages")
            if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
                raise ValueError("Invalid checkpoint")
            for message in messages:
                Message.from_dict(message)
        self.entries[identifier] = entry
        self.leaf = identifier

    def append(self, message: Message) -> str:
        """在当前叶节点之后追加消息，并返回新节点 ID。"""

        return self._append_entry({"type": "message", "message": message.to_dict()})

    def checkpoint(self, messages: Sequence[Message]) -> str:
        """将一组已压缩消息追加为当前分支的检查点。"""

        return self._append_entry({"type": "checkpoint", "messages": [m.to_dict() for m in messages]})

    def _append_entry(self, data: JsonObject) -> str:
        identifier = uuid4().hex
        entry = json.loads(json.dumps({**data, "id": identifier, "parent": self.leaf}, allow_nan=False))
        # Validate before touching the file; detach nested mutable arguments.
        if entry["type"] == "message":
            Message.from_dict(entry["message"])
        else:
            for message in entry["messages"]:
                Message.from_dict(message)
        self._write(entry)
        self._apply(entry)
        return identifier

    def branch(self, prefix: str) -> None:
        """将当前叶节点切换为唯一匹配的节点或根节点。"""

        if prefix == "root":
            leaf = None
        else:
            matches = [key for key in self.entries if key.startswith(prefix)]
            if len(matches) != 1:
                raise ValueError("Entry prefix must match exactly one node")
            leaf = matches[0]
        self._write({"type": "cursor", "leaf": leaf})
        self.leaf = leaf

    def messages(self) -> list[Message]:
        """重建当前分支的消息，并修复未完成的工具调用。"""

        path = []
        cursor = self.leaf
        while cursor:
            entry = self.entries[cursor]
            path.append(entry)
            cursor = entry["parent"]
        result = []
        for entry in reversed(path):
            if entry["type"] == "checkpoint":
                result = [Message.from_dict(m) for m in entry["messages"]]
            else:
                result.append(Message.from_dict(entry["message"]))
        return repair_incomplete_tools(result)

    def tree_lines(self) -> list[str]:
        """返回适合终端显示的会话树文本行。"""

        children: dict[str | None, list[str]] = {}
        for identifier, entry in self.entries.items():
            children.setdefault(entry["parent"], []).append(identifier)

        lines: list[str] = []

        def visit(identifier: str, prefix: str, connector: str) -> None:
            entry = self.entries[identifier]
            if entry["type"] == "message":
                message = entry["message"]
                role = str(message["role"])
                content = " ".join(str(message.get("content", "")).split())
                if not content and message.get("tool_calls"):
                    names = [str(call.get("name", "tool")) for call in message["tool_calls"]]
                    content = "calls " + ", ".join(names)
                label = f"{role:<10} {content[:64]}"
            else:
                label = "checkpoint compressed context"
            marker = "●" if identifier == self.leaf else "○"
            current = "  ← current" if identifier == self.leaf else ""
            lines.append(f"{prefix}{connector}{marker} {identifier[:8]}  {label}{current}".rstrip())

            descendants = children.get(identifier, [])
            child_prefix = prefix + ("   " if connector == "└─" else "│  " if connector else "")
            for index, child in enumerate(descendants):
                branch = "└─" if index == len(descendants) - 1 else "├─"
                visit(child, child_prefix, branch)

        roots = children.get(None, [])
        for index, root in enumerate(roots):
            connector = "" if len(roots) == 1 else ("└─" if index == len(roots) - 1 else "├─")
            visit(root, "", connector)
        return lines

    def close(self) -> None:
        """关闭会话文件并释放排他锁。"""

        if self._file:
            self._file.close()
            self._file = None


def repair_incomplete_tools(messages: Sequence[Message]) -> list[Message]:
    """分支可落在 assistant/tool 批次中间；补错误结果，绝不自动重放工具。"""
    result: list[Message] = []
    pending: dict[str, ToolCall] = {}
    for message in messages:
        if message.role != "tool":
            for call in pending.values():
                result.append(
                    Message(
                        "tool",
                        "Interrupted; inspect side effects before retrying.",
                        tool_call_id=call.id,
                        name=call.name,
                        is_error=True,
                    )
                )
            pending = {call.id: call for call in message.tool_calls}
        elif message.tool_call_id in pending:
            pending.pop(message.tool_call_id)
        else:
            continue
        result.append(message)
    for call in pending.values():
        result.append(
            Message(
                "tool",
                "Interrupted; inspect side effects before retrying.",
                tool_call_id=call.id,
                name=call.name,
                is_error=True,
            )
        )
    return result


def arc_state_directory(environ: Mapping[str, str] | None = None) -> Path:
    """返回符合 XDG 规范的 Arc 用户状态目录。"""

    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_STATE_HOME")
    state_home = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    if not state_home.is_absolute():
        state_home = Path.home() / ".local" / "state"
    return state_home / "arc"


def workspace_state_directory(cwd: Path, *, state_directory: Path | None = None) -> Path:
    """将规范化工作目录稳定映射到 Arc 状态目录中的隔离空间。"""

    workspace_key = hashlib.sha256(os.fsencode(str(cwd.resolve()))).hexdigest()
    root = state_directory or arc_state_directory()
    return root / "workspaces" / workspace_key


def input_history_path(cwd: Path, *, state_directory: Path | None = None) -> Path:
    """返回工作目录对应的全局交互输入历史路径。"""

    return workspace_state_directory(cwd, state_directory=state_directory) / "input-history"


def new_session_path(cwd: Path, *, state_directory: Path | None = None) -> Path:
    """在全局 Arc 状态目录中为工作目录生成新会话路径。"""

    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8] + ".jsonl"
    return workspace_state_directory(cwd, state_directory=state_directory) / "sessions" / name


def latest_session_path(cwd: Path, *, state_directory: Path | None = None) -> Path:
    """返回全局状态目录中当前工作区最后修改的 Arc 会话文件。"""

    session_directory = workspace_state_directory(cwd, state_directory=state_directory) / "sessions"
    paths = list(session_directory.glob("*.jsonl"))
    if not paths:
        raise ValueError("No saved sessions in this working directory")
    return max(paths, key=lambda path: path.stat().st_mtime_ns)
