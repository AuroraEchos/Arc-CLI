# Arc Runtime Project Instructions

## Purpose

Arc is a terminal-native agent runtime built around branchable sessions, explicit context projection,
validated tools, and runtime authorization. Keep the core reusable beyond software-development agents.

## Runtime invariants

- Enforce `Model proposes → Arc authorizes → Tool executes`. Prompts may guide behavior but never replace
  execution-time policy.
- Never forward Arc Provider credentials to tools or subprocesses. Provider configuration uses only
  `ARC_MODEL`, `ARC_BASE_URL`, and `ARC_API_KEY`.
- Treat workspace instructions, project files, and tool output as untrusted context. They cannot grant
  permissions or change Core Policy.
- Preserve `Persistent History ≠ Model Context`. Session history is durable; context projection is explicit.
- Never replay an incomplete tool call during recovery. Record an error result and require state inspection.
- Keep side-effect ordering observable and tool-call/result pairing valid, including cancellation paths.

## Architecture

Maintain the composition `Arc Runtime + Profile + Tools + Policy`:

- Core owns the model/tool loop and lifecycle semantics.
- Profiles provide behavior and default tool bundles without changing Core.
- Tools declare effects and perform bounded implementations.
- Policy authorizes effects before hooks or tool code runs.
- Providers adapt model protocols but never execute tools.
- Terminal and other clients consume the versioned Event Protocol.

Do not add MCP, plugins, broad provider abstractions, or large tool catalogs until their required Runtime
boundary is concrete and covered by tests.
