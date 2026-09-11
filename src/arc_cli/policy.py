"""Runtime authorization for model-proposed tool effects."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from arc_cli.tools import Tool, ToolContext
from arc_cli.types import Effect, ToolCall

AuthorizationStatus = Literal["allowed", "blocked", "confirmation_required"]
ALL_EFFECTS: frozenset[Effect] = frozenset({"read", "write", "process", "external", "destructive"})
LOCAL_EFFECTS: frozenset[Effect] = frozenset({"read", "write", "process"})


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

    Arc defaults to autonomous authorization for experienced local users. The
    restricted factory retains an effect gate for external and destructive
    proposals. Neither mode turns Bash into an operating-system sandbox.
    """

    def __init__(
        self,
        *,
        allowed: Iterable[Effect] = ALL_EFFECTS,
        require_confirmation: Iterable[Effect] = ("external", "destructive"),
        approval: ApprovalHandler | None = None,
    ):
        self.allowed = frozenset(allowed)
        self.require_confirmation = frozenset(require_confirmation)
        self.approval = approval

    @classmethod
    def autonomous(cls) -> ExecutionPolicy:
        """Auto-authorize every declared effect while retaining audit events."""

        return cls(allowed=ALL_EFFECTS)

    @classmethod
    def restricted(cls, *, approval: ApprovalHandler | None = None) -> ExecutionPolicy:
        """Require approval for external and destructive effects."""

        return cls(allowed=LOCAL_EFFECTS, approval=approval)

    @property
    def mode(self) -> str:
        """Return a concise name suitable for status output."""

        if self.allowed == ALL_EFFECTS:
            return "autonomous"
        if self.allowed == LOCAL_EFFECTS:
            return "restricted"
        return "custom"

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
