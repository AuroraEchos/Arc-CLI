"""定义工具执行边界上的轻量扩展钩子。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from arc_cli.tools import ToolContext, ToolResult
from arc_cli.types import ToolCall


@dataclass
class Hooks:
    """保存工具执行前后的可选异步回调。"""

    before_tool: Callable[[ToolCall, ToolContext], Awaitable[str | None]] | None = None
    after_tool: Callable[[ToolCall, ToolResult, ToolContext], Awaitable[ToolResult]] | None = None
