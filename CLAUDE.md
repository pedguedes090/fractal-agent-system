# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick reference

```powershell
# First-time setup
python -m uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e .
corepack enable; pnpm install

# Daily cycle
.venv\Scripts\python.exe -m pytest tests/ -v          # all tests
.venv\Scripts\python.exe -m pytest tests/test_X.py -v  # single file
pnpm run check                                          # full CI gate: JS syntax + compileall + eval + full test suite
pnpm start                                              # launch Electron app

# Single node classification smoke test (no LLM needed)
.venv\Scripts\python.exe -c "from agent_engine.graph import classify_execution; print(classify_execution('sửa bug auth'))"
```

## Electron / GPU gotcha

Do **not** add `app.commandLine.appendSwitch("in-process-gpu")` or `"disable-gpu-sandbox"` in `src/main/main.js`. On Windows 11 with hardware acceleration that combo lets the renderer paint the DOM but then crashes the GPU process before the framebuffer is presented — the window opens black even though `[app] init complete — dashboard rendered` shows in the log, accompanied by `Network service crashed`, `Gpu Cache Creation failed`, and `Unable to move the cache: Access is denied`. The cache errors are benign. If a machine genuinely needs a GPU fallback, run with `AGENT_DISABLE_HW_ACCEL=1` (handled at the top of `main.js`) instead of forcing in-process GPU.

## Architecture

**Two-process desktop app:** Electron UI (`src/`) spawns a Python backend (`engine/agent_engine/server.py`) that communicates via HTTP NDJSON on `127.0.0.1:<random>`.

**Pipeline:** 31-node LangGraph state machine declared in `engine/agent_engine/workflows/default.yaml` (YAML topology + Jinja2 sandbox for route conditions). LLM is used for semantic analysis/planning/review but NEVER chooses the next node, retry budget, or branch condition — those are deterministic.

**New node inserted between codegraph_context and intake fan-out:** `repo_intelligence` runs `RepoIntelligenceAgent.analyze()` producing a `ContextPack` with source-verified evidence, architecture reconstruction, and impact analysis. Planning nodes now receive `repoIntelligence` via contextRoutes.

**Control plane:** Single SQLite DB `agent-state.sqlite` (WAL mode, `agent_state/` directory). All lifecycle authority lives here — no separate broker/checkpoint/execution files after migration. ADR: `docs/architecture/0001-local-first-control-plane.md`.

**Two lanes:** `write` (process-wide `_WRITE_LOCK`; git worktree or direct workspace) and `read_only` (no lock, no worktree). `classify_execution()` determines lane from keyword signals.

**Direct workspace mode (default):** coder edits, installs, and verification all run in the opened folder. Dependency sync after coder runs is inferred from lock files (pnpm/yarn/npm/bun). Set `directWorkspaceMode: false` for isolated git-worktree-per-execution.

## Key modules (importable under `agent_engine`)

| Module | Purpose | Key export |
|---|---|---|
| `graph.py` | LangGraph nodes, state machine, `run_pipeline()` entry point | `PipelineState`, `classify_execution` |
| `openhands_worker.py` | OpenHands SDK adapter: `Conversation`, `Agent`, `LLM`, `Tool` | `run_openhands_worker` |
| `workspace.py` | File I/O, sandbox, codegraph, setup commands, verification commands | `workspace` module used directly by graph nodes |
| `claude_adapter.py` | Anthropic SDK wrapper: generate, stream, tool-loop, retry, cancel | `ClaudeProvider`, `ClaudeConfig` |
| `tool_registry.py` | Unified tool interface + registry; 5 built-in tools (file_read/write/list, command_run, search_content) | `ToolRegistry`, `Tool` |
| `agent_loop.py` | RECEIVE→CLASSIFY→ANALYSIS→PLAN→EXECUTE→VERIFY→REPLAN→FINALIZE | `AgentLoop`, `LoopConfig` |
| `verifier.py` | Post-exec checks: tool errors, file changes, build, tests, scope | `Verifier`, `Verdict` |
| `storage/` | Domain models (`models.py`) + repository interface + SQLite impl (`run_repo.py`) | `SQLiteRunRepository`, `AgentRun`, `Plan` |
| `repo_intelligence/` | Pre-Planner analysis: graph retrieval, source verification, architecture reconstruction, ContextPack | `RepoIntelligenceAgent`, `ContextPack` |
| `deterministic_workflow.py` | YAML validator + Jinja2 sandbox + `DEFAULT_WORKFLOW` singleton | `DeterministicWorkflow` |
| `durable_execution.py` | Lease/heartbeat/retry/idempotency, `DurableExecutionStore` | `checkpoint_step`, `execution_context` |
| `telemetry.py` | OpenTelemetry tracing/metrics, `start_span()` contextmanager | `configure_telemetry` |
| `llm_client.py` | Legacy OpenAI-compatible HTTP client (pre-Anthropic SDK) | `ChatClient` |

## Module dependency rules

- `storage/models.py` depends on NOTHING (zero imports from agent_engine)
- `repo_intelligence/` can import `workspace`, `telemetry`, `debug_log`
- `graph.py` can import everything — it's the integration layer
- `claude_adapter.py` can import `telemetry`, `debug_log`, `storage/models`
- `agent_loop.py` can import `storage/`, `debug_log`
- Never import `graph` from other modules (circular)
- `prompt_templates.py` is pure strings + schemas — no runtime deps

## Skills (workflow methodology)

14 skills in `skills/` adapted from obra/superpowers. Follow the workflow: brainstorming → writing-plans → TDD → subagent-driven-development → code-review → verification-before-completion → finishing-a-development-branch.

## Python conventions

- `from __future__ import annotations` in every file
- Type hints everywhere; `dict[str, Any]` for JSON-like shapes
- `@dataclass` for domain models, each with `to_dict()` / `from_dict()`
- `write_debug_event("category.action", {...})` for structured logs
- `telemetry.start_span("name", {...}) as span:` for tracing
- Env vars for all configurable values, reasonable defaults
- No hardcoded paths, secrets, or model names
