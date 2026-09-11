"""组合 Arc 核心循环与持久化会话存储。"""

from collections.abc import AsyncGenerator
from contextlib import aclosing

from arc_cli.agent import Arc
from arc_cli.context import compact_messages
from arc_cli.session import SessionStore
from arc_cli.types import Event


class ArcSession:
    """在 Arc 运行过程中持久化消息并管理会话分支。"""

    def __init__(self, agent: Arc, store: SessionStore):
        self.agent = agent
        self.store = store
        self.agent.messages = store.messages()
        self._saved = len(self.agent.messages)

    def _save(self) -> None:
        while self._saved < len(self.agent.messages):
            self.store.append(self.agent.messages[self._saved])
            self._saved += 1

    async def run(self, prompt: str) -> AsyncGenerator[Event, None]:
        """运行一次任务，并在事件边界与退出清理阶段持久化新消息。"""

        try:
            async with aclosing(self.agent.run(prompt)) as events:
                async for event in events:
                    event.validate()
                    self._save()
                    yield event
        finally:
            # Arc 的 finally 可能补齐取消结果，即使没有 message_end 也必须保存。
            self._save()

    def branch(self, prefix: str) -> None:
        """切换到唯一匹配的会话节点，并从存储重建 Arc 历史。"""

        if self.agent.is_running:
            raise ValueError("Wait for the agent or use /abort first")
        self.store.branch(prefix)
        self.agent.messages = self.store.messages()
        self._saved = len(self.agent.messages)
        self.agent.clear_queues()

    async def compact(self, keep_last_turns: int = 2) -> None:
        """压缩较早消息并将结果保存为新的会话检查点。"""

        if self.agent.is_running:
            raise ValueError("Wait for the agent or use /abort first")
        compacted = await compact_messages(
            self.agent.messages, self.agent.provider, keep_last_turns=keep_last_turns
        )
        self.store.checkpoint(compacted)
        self.agent.messages = compacted
        self._saved = len(compacted)
