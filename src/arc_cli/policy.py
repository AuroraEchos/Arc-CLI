"""Runtime authorization for model-proposed tool effects."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from arc_cli.tools import Tool, ToolContext
from arc_cli.types import Effect, ToolCall

AuthorizationStatus = Literal["allowed", "blocked", "confirmation_required"]


@dataclass(frozen=True)
class AuthorizationRequest:
    """A validated model proposal presented to the Runtime policy."""

    call: ToolCall
    effects: tuple[Effect, ...]
    context: ToolContext


@dataclass(frozen=True)
class AuthorizationDecision:
    """The stable result of Runtime authorization."""

    status: AuthorizationStatus
    effects: tuple[Effect, ...]
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.status == "allowed"


ApprovalHandler = Callable[[AuthorizationRequest], Awaitable[bool]]


class ExecutionPolicy:
    """Authorize declared tool effects before an implementation is invoked.

    Read, write and local process effects preserve Arc's current developer
    workflow. External and destructive effects require explicit preauthorization
    or an approval handler. A policy decision cannot turn Bash into a sandbox;
    callers that do not trust arbitrary shell code should disable the Bash tool.
    """

    def __init__(
        self,
        *,
        allowed: Iterable[Effect] = ("read", "write", "process"),
        require_confirmation: Iterable[Effect] = ("external", "destructive"),
        approval: ApprovalHandler | None = None,
    ):
        self.allowed = frozenset(allowed)
        self.require_confirmation = frozenset(require_confirmation)
        self.approval = approval

    async def authorize(self, call: ToolCall, tool: Tool, context: ToolContext) -> AuthorizationDecision:
        """Return a decision without executing the proposed tool."""

        effects = tool.effects_for(call.arguments)
        unknown = set(effects) - self.allowed - self.require_confirmation
        if unknown:
            names = ", ".join(sorted(unknown))
            return AuthorizationDecision("blocked", effects, f"effects not permitted: {names}")
        pending = set(effects) & self.require_confirmation - self.allowed
        if not pending:
            return AuthorizationDecision("allowed", effects)
        names = ", ".join(sorted(pending))
        if self.approval is None:
            return AuthorizationDecision(
                "confirmation_required",
                effects,
                f"explicit authorization required for effects: {names}",
            )
        request = AuthorizationRequest(call, effects, context)
        if await self.approval(request):
            return AuthorizationDecision("allowed", effects, f"approved effects: {names}")
        return AuthorizationDecision("blocked", effects, f"authorization denied for effects: {names}")
