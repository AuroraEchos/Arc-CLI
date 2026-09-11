"""定义工具注册表、参数校验和 Arc CLI 的内置工具。"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import signal
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from jsonschema import Draft202012Validator, ValidationError

from arc_cli.types import Effect, JsonObject, ToolCall, ToolSpec

MAX_FILE_BYTES = 1024 * 1024
MAX_OUTPUT = 24_000

# Provider settings are Runtime state, never tool input. The broader pattern also
# prevents accidentally forwarding unrelated ambient credentials by default.
PROVIDER_ENV_NAMES = frozenset(
    {
        "ARC_API_KEY",
        "ARC_BASE_URL",
        "ARC_MODEL",
    }
)
SENSITIVE_ENV_PATTERN: re.Pattern[str] = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY)(?:$|_)",
    flags=re.IGNORECASE,
)


def sanitized_subprocess_env(
    source: Mapping[str, str] | None = None,
    *,
    allow_sensitive: Sequence[str] = (),
    deny: Sequence[str] = (),
) -> dict[str, str]:
    """Build a subprocess environment without Runtime/provider credentials.

    Non-sensitive process settings are retained so local commands continue to
    work. Sensitive-looking names are denied unless explicitly allowlisted, but
    Provider names remain unconditionally blocked.
    """

    source = os.environ if source is None else source
    allowed = {name.upper() for name in allow_sensitive} - PROVIDER_ENV_NAMES
    denied = {name.upper() for name in deny} | PROVIDER_ENV_NAMES
    return {
        name: value
        for name, value in source.items()
        if name.upper() not in denied
        and (name.upper() in allowed or SENSITIVE_ENV_PATTERN.search(name) is None)
    }


@dataclass(frozen=True)
class ToolResult:
    """保存工具输出及其错误状态。"""

    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ToolContext:
    """提供工具工作目录和流式进度回调。"""

    cwd: Path
    emit: Callable[[str], Awaitable[None]]
    env: Mapping[str, str] | None = None
    env_allow_sensitive: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tool:
    """将工具规范与异步执行函数绑定。"""

    spec: ToolSpec
    execute: Callable[[JsonObject, ToolContext], Awaitable[ToolResult]]
    effects: tuple[Effect, ...] = ("read",)
    resolve_effects: Callable[[JsonObject], tuple[Effect, ...]] | None = None

    def effects_for(self, arguments: JsonObject) -> tuple[Effect, ...]:
        """Return the declared effects for one validated invocation."""

        return self.resolve_effects(arguments) if self.resolve_effects else self.effects


class ToolRegistry:
    """注册工具，并在执行前校验模型提供的参数。"""

    def __init__(self, tools: Sequence[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """注册工具，并验证其名称唯一且参数 Schema 有效。"""
        if tool.spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.spec.name}")
        Draft202012Validator.check_schema(tool.spec.parameters)
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        """返回所有已注册工具的规范。"""
        return [tool.spec for tool in self._tools.values()]

    def names(self) -> list[str]:
        """按注册顺序返回工具名称。"""
        return list(self._tools)

    def validate(self, call: ToolCall) -> Tool:
        """验证工具调用名称和参数，并返回匹配的工具。"""
        tool = self._tools.get(call.name)
        if tool is None:
            raise ValueError(f"Unknown tool: {call.name}")
        try:
            Draft202012Validator(tool.spec.parameters).validate(call.arguments)
        except ValidationError as e:
            raise ValueError(f"Invalid arguments for tool {call.name}: {e.message}") from e
        return tool

    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
        """执行已注册工具，并将普通异常转换为错误结果。"""
        try:
            tool = self.validate(call)
            return await tool.execute(call.arguments, context)
        except ValueError as exc:
            return ToolResult(f"Invalid arguments: {exc}", True)
        except Exception as exc:
            # CancelledError 是 BaseException，必须交还 Arc 负责取消和清理。
            return ToolResult(f"{type(exc).__name__}: {exc}", True)


def _path(context: ToolContext, value: str) -> Path:
    """相对于工具工作目录解析路径；绝对路径保持其原始语义。"""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else context.cwd / path).resolve()


def _read_text(path: Path) -> str:
    """读取受大小限制的 UTF-8 文本，并拒绝明显的二进制文件。"""
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("File exceeds 1 MiB; use bash to inspect a smaller portion")
    if b"\x00" in data:
        raise ValueError("Binary files are not supported")
    return data.decode("utf-8")


def _atomic_write(path: Path, content: str) -> None:
    """通过同目录临时文件和原子替换写入 UTF-8 文本。"""
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Write exceeds 1 MiB")

    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=".arc-", dir=path.parent)

    written = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), permissions)
        os.replace(temporary, path)
        written = True
    finally:
        if not written:
            Path(temporary).unlink(missing_ok=True)


PatchAction = Literal["add", "update", "delete"]


@dataclass(frozen=True)
class _PreparedPatch:
    """One validated file transition ready to commit."""

    action: PatchAction
    path: Path
    original: str | None
    updated: str | None
    permissions: int | None
    edit_count: int = 0


def _validate_write_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("Patch result exceeds 1 MiB")


def _prepare_patch(arguments: JsonObject, context: ToolContext) -> list[_PreparedPatch]:
    """Validate every patch operation and calculate results without changing disk state."""

    prepared: list[_PreparedPatch] = []
    seen: set[Path] = set()
    for operation in cast(list[JsonObject], arguments["operations"]):
        action = cast(PatchAction, operation["type"])
        path = _path(context, operation["path"])
        if path in seen:
            raise ValueError(f"Duplicate patch path: {path}")
        seen.add(path)

        if action == "add":
            if path.exists():
                raise ValueError(f"Cannot add existing path: {path}")
            content = cast(str, operation["content"])
            _validate_write_size(content)
            prepared.append(_PreparedPatch(action, path, None, content, None))
            continue

        if not path.is_file():
            raise ValueError(f"Patch target is not a file: {path}")
        original = _read_text(path)
        permissions = path.stat().st_mode & 0o777
        if action == "delete":
            prepared.append(_PreparedPatch(action, path, original, None, permissions))
            continue

        updated = original
        edits = cast(list[JsonObject], operation["edits"])
        for index, replacement in enumerate(edits, 1):
            old_text = cast(str, replacement["old_text"])
            count = updated.count(old_text)
            if count == 0:
                raise ValueError(f"Update {path} edit {index}: old_text not found")
            if count > 1:
                raise ValueError(f"Update {path} edit {index}: old_text matches {count} times")
            updated = updated.replace(old_text, cast(str, replacement["new_text"]), 1)
        if updated == original:
            raise ValueError(f"Update does not change file: {path}")
        _validate_write_size(updated)
        prepared.append(_PreparedPatch(action, path, original, updated, permissions, len(edits)))
    return prepared


def _assert_patch_precondition(change: _PreparedPatch) -> None:
    """Refuse to overwrite a target that changed after preflight validation."""

    if change.original is None:
        if change.path.exists():
            raise ValueError(f"Patch target appeared after validation: {change.path}")
        return
    if not change.path.is_file() or _read_text(change.path) != change.original:
        raise ValueError(f"Patch target changed after validation: {change.path}")


def _commit_patch(change: _PreparedPatch) -> None:
    _assert_patch_precondition(change)
    if change.updated is None:
        change.path.unlink()
    else:
        _atomic_write(change.path, change.updated)


def _rollback_patch(change: _PreparedPatch) -> None:
    if change.original is None:
        change.path.unlink(missing_ok=True)
    else:
        _atomic_write(change.path, change.original)
        if change.permissions is not None:
            change.path.chmod(change.permissions)


async def apply_patch(arguments: JsonObject, context: ToolContext) -> ToolResult:
    """Apply a prevalidated multi-file text patch with best-effort rollback."""

    prepared = _prepare_patch(arguments, context)
    committed: list[_PreparedPatch] = []
    try:
        for change in prepared:
            _commit_patch(change)
            committed.append(change)
    except Exception as exc:
        rollback_errors: list[str] = []
        for change in reversed(committed):
            try:
                _rollback_patch(change)
            except Exception as rollback_error:
                rollback_errors.append(f"{change.path}: {rollback_error}")
        if rollback_errors:
            details = "; ".join(rollback_errors)
            raise RuntimeError(f"Patch failed ({exc}); rollback also failed: {details}") from exc
        raise

    summaries = []
    for change in prepared:
        detail = f" ({change.edit_count} edits)" if change.action == "update" else ""
        summaries.append(f"- {change.action} {change.path}{detail}")
    return ToolResult(f"Applied {len(prepared)} file operations:\n" + "\n".join(summaries))


def _apply_patch_effects(arguments: JsonObject) -> tuple[Effect, ...]:
    operations = cast(list[JsonObject], arguments["operations"])
    if any(operation["type"] == "delete" for operation in operations):
        return ("write", "destructive")
    return ("write",)


async def read(arguments: JsonObject, context: ToolContext) -> ToolResult:
    """读取 UTF-8 文件，并返回指定范围内带行号的文本。"""
    lines = _read_text(_path(context, arguments["path"])).splitlines()
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", 200)
    selected = lines[offset - 1 : offset - 1 + limit]
    result = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, offset))
    if offset - 1 + len(selected) < len(lines):
        result += f"\n[More lines: use offset={offset + len(selected)}]"
    if len(result) > MAX_OUTPUT:
        result = result[:MAX_OUTPUT] + "\n[Output truncated at 24000 characters]"
    return ToolResult(result or "[Empty file or offset beyond end]")


async def write(arguments: JsonObject, context: ToolContext) -> ToolResult:
    """创建或原子覆盖 UTF-8 文件。"""
    path = _path(context, arguments["path"])
    _atomic_write(path, arguments["content"])
    return ToolResult(f"Wrote {path}")


async def edit(arguments: JsonObject, context: ToolContext) -> ToolResult:
    """对文件执行可检测缺失和歧义的精确文本替换。"""
    path = _path(context, arguments["path"])
    original = _read_text(path)
    old = arguments["old_text"]
    count = original.count(old)
    if count == 0:
        raise ValueError("old_text not found; read the file again")
    if count > 1 and not arguments.get("replace_all", False):
        raise ValueError(f"old_text matches {count} times; supply more context or replace_all=true")
    updated = original.replace(old, arguments["new_text"], -1 if arguments.get("replace_all") else 1)
    _atomic_write(path, updated)
    return ToolResult(f"Edited {path}")


async def bash(arguments: JsonObject, context: ToolContext) -> ToolResult:
    """在工具工作目录运行 Bash，并限制时间和输出大小。"""
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        arguments["command"],
        cwd=context.cwd,
        env=sanitized_subprocess_env(
            context.env,
            allow_sensitive=context.env_allow_sensitive,
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    chunks: list[str] = []
    kept = 0
    truncated = False
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    async def collect() -> None:
        nonlocal kept, truncated
        assert process.stdout is not None
        while data := await process.stdout.read(4096):
            text = decoder.decode(data)
            available = MAX_OUTPUT - kept
            if len(text) > available:
                truncated = True
            text = text[:available]
            if text:
                kept += len(text)
                chunks.append(text)
                await context.emit(text)
        remainder = decoder.decode(b"", final=True)
        if remainder and kept < MAX_OUTPUT:
            chunks.append(remainder)
            await context.emit(remainder)
        await process.wait()

    timed_out = False
    try:
        async with asyncio.timeout(arguments.get("timeout", 120)):
            await collect()
    except TimeoutError:
        timed_out = True
    finally:
        # 取消/超时都杀整个进程组；也清理 shell 退出后遗留的后台进程。
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
    result = "".join(chunks)
    if truncated:
        result += "\n[Output truncated at 24000 characters]"
    if timed_out:
        return ToolResult(result + "\n[Command timed out; process group killed]", True)
    return ToolResult(result + f"\n[Exit code: {process.returncode}]", process.returncode != 0)


def _bash_effects(arguments: JsonObject) -> tuple[Effect, ...]:
    """Conservatively annotate common shell capabilities for policy decisions.

    This is intentionally a risk classifier, not a shell sandbox. ``process``
    remains broad authority because Bash can obscure any operation.
    """

    command = arguments["command"]
    effects: list[Effect] = ["process"]
    boundary = r"(?:^|[;&|()\n])\s*"
    prefixes = r"(?:(?:sudo|command|exec|nohup)\s+)*"
    external_patterns = (
        boundary + prefixes + r"(?:curl|wget|ssh|scp|sftp|nc|ncat|telnet|ftp)\b",
        boundary + prefixes + r"git\s+(?:clone|fetch|pull|push)\b",
        boundary + prefixes + r"(?:pip[0-9.]*|python[0-9.]*\s+-m\s+pip)\s+install\b",
        boundary + prefixes + r"npm\s+install\b",
        boundary + prefixes + r"uv\s+add\b",
    )
    destructive_patterns = (
        boundary + prefixes + r"(?:rm|rmdir|unlink|shred|mkfs(?:\.[a-z0-9]+)?|fdisk|shutdown|reboot)\b",
        boundary + prefixes + r"git\s+(?:reset\s+--hard|clean\b)",
        r"\bdrop\s+(?:database|table)\b",
    )
    if any(re.search(pattern, command, re.IGNORECASE) for pattern in external_patterns):
        effects.append("external")
    if any(re.search(pattern, command, re.IGNORECASE) for pattern in destructive_patterns):
        effects.append("destructive")
    return tuple(effects)


def create_builtin_tools() -> list[Tool]:
    """创建 Arc CLI 默认提供的文件与 shell 工具。"""
    path = {"type": "string", "minLength": 1}
    patch_operation = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "type": {"const": "add"},
                    "path": path,
                    "content": {"type": "string", "maxLength": MAX_FILE_BYTES},
                },
                "required": ["type", "path", "content"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "update"},
                    "path": path,
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string", "minLength": 1},
                                "new_text": {"type": "string", "maxLength": MAX_FILE_BYTES},
                            },
                            "required": ["old_text", "new_text"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["type", "path", "edits"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"type": {"const": "delete"}, "path": path},
                "required": ["type", "path"],
                "additionalProperties": False,
            },
        ]
    }
    definitions = [
        (
            "read",
            "Read UTF-8 text with numbered lines. offset is 1-based.",
            read,
            {
                "path": path,
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            },
            ["path"],
            ("read",),
            None,
        ),
        (
            "write",
            "Create or overwrite a UTF-8 file. Read existing files before changing them.",
            write,
            {"path": path, "content": {"type": "string"}},
            ["path", "content"],
            ("write",),
            None,
        ),
        (
            "edit",
            "Replace exact text; ambiguous matches fail unless replace_all is true.",
            edit,
            {
                "path": path,
                "old_text": {"type": "string", "minLength": 1},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            ["path", "old_text", "new_text"],
            ("write",),
            None,
        ),
        (
            "apply_patch",
            "Apply validated add, update, and delete operations across UTF-8 text files as one patch.",
            apply_patch,
            {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": patch_operation,
                }
            },
            ["operations"],
            ("write",),
            _apply_patch_effects,
        ),
        (
            "bash",
            "Run a shell command in cwd. No sandbox. Output is bounded; timeout in seconds.",
            bash,
            {
                "command": {"type": "string", "minLength": 1},
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
            },
            ["command"],
            ("process",),
            _bash_effects,
        ),
    ]
    return [
        Tool(
            ToolSpec(
                name,
                description,
                {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            ),
            execute,
            cast(tuple[Effect, ...], effects),
            resolver,
        )
        for name, description, execute, properties, required, effects, resolver in definitions
    ]
