"""Arc CLI 公共包接口。

Arc CLI 是一个终端原生 Agent Runtime；核心运行时由 :class:`Arc` 提供。
"""

from arc_cli.agent import Arc

__version__ = "0.1.0"

__all__ = ["Arc", "__version__"]
