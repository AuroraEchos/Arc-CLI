# Developer Profile Instructions

## Working agreement

- Target Python 3.11+ and keep strict MyPy compatibility.
- Prefer focused, readable modules and standard-library solutions. Add dependencies only when they create a
  clear boundary or remove substantial complexity.
- Preserve unrelated working-tree changes. Do not inspect, print, commit, or overwrite `.env` secrets.
- Use `apply_patch` for source and documentation edits. Keep user-facing CLI text in English.
- Do not describe Bash risk classification as an operating-system sandbox.

## Repository map

- `agent.py`: Core model/tool loop and cancellation semantics.
- `policy.py`: execution-time effect authorization.
- `profiles.py`: Core/Profile/User/Workspace instruction projection.
- `tools.py`: tool registry, implementations, and subprocess environment isolation.
- `types.py`: messages and versioned Runtime Event Protocol.
- `session.py` and `runtime.py`: append-only session tree, recovery, and persistence boundaries.
- `terminal.py` and `cli.py`: terminal-native presentation and application assembly.

## Verification

Run these before handing off a change:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
```

Prioritize Runtime-semantic tests: streaming and tool cancellation, authorization, malformed calls, multiple
tool pairing, consumer failure after side effects, session recovery, branching, checkpoints, and reopen.
