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

**Pipeline:** 34-node LangGraph state machine declared in `engine/agent_engine/workflows/default.yaml` (YAML topology + Jinja2 sandbox for route conditions). LLM is used for semantic analysis/planning/review but NEVER chooses the next node, retry budget, or branch condition — those are deterministic.

**Key pipeline insertions:**
- `repo_intelligence` (between codegraph_context and intake fan-out): runs `RepoIntelligenceAgent.analyze()` producing a `ContextPack`.
- `doctor_feedback` (between code_reviewer_agent and release_deploy_agent): runs the Project Doctor's autonomous scan→plan→patch→verify loop. On success, reviewer_decision downgrades hygiene blockers (syntax, deps, lockfile, gitignore) to warnings — the reviewer can then clear the review even after prior test failures.
- `reviewer_decision` → `openhands_worker` rework loop is the only cycle in the DAG.

**Control plane:** Single SQLite DB `agent-state.sqlite` (WAL mode, `agent_state/` directory). All lifecycle authority lives here — no separate broker/checkpoint/execution files after migration. ADR: `docs/architecture/0001-local-first-control-plane.md`.

**Two lanes:** `write` (process-wide `_WRITE_LOCK`; git worktree or direct workspace) and `read_only` (no lock, no worktree). `classify_execution()` determines lane from keyword signals.

**Direct workspace mode (default):** coder edits, installs, and verification all run in the opened folder. Dependency sync after coder runs is inferred from lock files (pnpm/yarn/npm/bun). Set `directWorkspaceMode: false` for isolated git-worktree-per-execution.

## Key modules (importable under `agent_engine`)

| Module | Purpose | Key export |
|---|---|---|
| `graph.py` | LangGraph nodes, state machine, `run_pipeline()` entry point, `traced_node` wrapper with enriched emit (agentRole, durationMs, I/O summary, token delta) | `PipelineState`, `classify_execution` |
| `openhands_worker.py` | OpenHands SDK adapter: `Conversation`, `Agent`, `LLM`, `Tool` | `run_openhands_worker` |
| `workspace.py` | File I/O, sandbox, codegraph, setup commands, verification commands; command allowlist gated by `AGENT_BYPASS_SAFE_COMMANDS` | `workspace` module used directly by graph nodes |
| `claude_adapter.py` | Anthropic SDK wrapper: generate, stream, tool-loop, retry, cancel; accepts optional `emit` callable for per-LLM-call events | `ClaudeProvider`, `ClaudeConfig` |
| `tool_registry.py` | Unified tool interface + registry; 5 built-in tools (file_read/write/list, command_run, search_content); accepts module-level `set_emit()` for tool-call events | `ToolRegistry`, `Tool` |
| `agent_loop.py` | RECEIVE→CLASSIFY→ANALYSIS→PLAN→EXECUTE→VERIFY→REPLAN→FINALIZE | `AgentLoop`, `LoopConfig` |
| `verifier.py` | Post-exec checks: tool errors, file changes, build, tests, scope | `Verifier`, `Verdict` |
| `storage/` | Domain models (`models.py`) + repository interface + SQLite impl (`run_repo.py`) | `SQLiteRunRepository`, `AgentRun`, `Plan` |
| `repo_intelligence/` | Pre-Planner analysis: graph retrieval, source verification, architecture reconstruction, ContextPack | `RepoIntelligenceAgent`, `ContextPack` |
| `deterministic_workflow.py` | YAML validator + Jinja2 sandbox + `DEFAULT_WORKFLOW` singleton; `route()` resolves conditional edges first-match-wins | `DeterministicWorkflow` |
| `durable_execution.py` | Lease/heartbeat/retry/idempotency, `DurableExecutionStore` | `checkpoint_step`, `execution_context` |
| `telemetry.py` | OpenTelemetry tracing/metrics, `start_span()` contextmanager; `now_ms()`, `record_token_usage()`, `get_token_usage_delta()` for per-node token accounting | `configure_telemetry` |
| `project_doctor/` | Autonomous scan→plan→patch→verify pipeline (scanner, planner, patcher, verifier, orchestrator). Uses `claude-agent-sdk` with fallback to `anthropic` SDK | `run_doctor`, `Doctor`, `ScanReport`, `FixReport` |
| `agent_sdk_provider.py` | Thin adapter over `claude-agent-sdk.query()` — Read/Edit/Bash agent with live token streaming into `emit()` | `ClaudeAgentSDKProvider`, `maybe_build_provider` |
| `codebase_memory.py` | Gateway to the `codebase-memory-mcp` binary (knowledge graph: search, trace, architecture). Falls back gracefully when binary is missing | `search_graph`, `trace_path`, `get_architecture` |
| `autonomy.py` | L4 idle scanner: discovers findings via static analysis of the workspace (TODO/FIXME/HACK markers, unsafe patterns, missing tests, large files), ranks by priority×impact/effort, generates long-horizon initiative plans + L5 skill proposals, persists to ACT-R memory store. **Auto Loop scheduler**: `select_next_task()` picks the highest-priority finding or rotates through an enhancement-idea pool so the loop never starves. | `discover_autonomous_findings`, `build_long_horizon_plan`, `select_next_task`, `run_idle_discovery`, `autonomy_status` |
| `skill_registry/` | Two-tier skill selection: deterministic eligibility filter + evidence-based ranker | `SkillRouter` |
| `planning_council/` | Multi-plan synthesis across parallel plan/critique nodes | — |
| `llm_client.py` | Legacy OpenAI-compatible HTTP client (pre-Anthropic SDK) | `ChatClient` |
| `prompt_templates.py` | Structured JSON schemas for every LLM-facing node (planner, coder, reviewer, etc.) + system prompts | `PLAN_SCHEMAS`, `CODER_SCHEMAS`, `REVIEWER_SCHEMAS` |
| `run.py` | Standalone CLI pipeline runner (no Electron/HTTP needed) | `main` |

## Module dependency rules

- `storage/models.py` depends on NOTHING (zero imports from agent_engine)
- `project_doctor/models.py` depends on NOTHING (zero imports from agent_engine)
- `repo_intelligence/` can import `workspace`, `telemetry`, `debug_log`
- `graph.py` can import everything — it's the integration layer
- `claude_adapter.py` can import `telemetry`, `debug_log`, `storage/models`
- `agent_loop.py`, `project_doctor/scanner.py`, `project_doctor/patcher.py` can import `storage/`, `debug_log`
- Never import `graph` from other modules (circular)
- `prompt_templates.py` is pure strings + schemas — no runtime deps
- `agent_sdk_provider.py` depends only on `claude-agent-sdk` (no agent_engine deps)

## Env var overrides

| Variable | Purpose |
|---|---|
| `AGENT_ENGINE_STATE_DIR` | Override `.agent-state/` dir (default: cwd-relative) |
| `AGENT_BYPASS_SAFE_COMMANDS` | Set to `1` to disable command allowlists (set via Full Power panel, not manually) |
| `ANTHROPIC_API_KEY` | API key for Anthropic SDK + claude-agent-sdk |
| `ANTHROPIC_BASE_URL` | Override Anthropic API base URL (for LiteLLM / OpenRouter / custom proxy) |
| `OPENHANDS_SUPPRESS_BANNER` | Set to `1` to hide the OpenHands SDK banner |
| `AGENT_DISABLE_HW_ACCEL` | Set to `1` to disable GPU in Electron on broken drivers |
| `AGENT_NO_ELEVATE` | Set to `1` to skip Windows UAC relaunch (dev/debug only) |
| `AGENT_HEARTBEAT_SECONDS` | Heartbeat interval for durable execution leases (default 5s) |

## Electron + UI architecture

**Two stores, no cross-query:**
- **Engine SQLite** (`engine/agent_engine/storage/`): 9 tables (storage_runs/steps/tool_calls/usage/artifacts/messages/conversations/memories/checkpoints). The renderer CANNOT read these directly — all data flows through the NDJSON progress stream.
- **Electron UI SQLite** (`src/main/appDatabase.js`): 4 tables (sessions/messages/runs/approvals). Runs stored as opaque `payload_json` blobs + progressEvents arrays for replay.

**Event flow:** Python `server.py` `emit()` NDJSON → `backendService.js` normalize (allowlist of 24 fields including eventId, parentEventId, correlationId, agentRole, durationMs, model, tool, status, tokenUsage, I/O summary) → IPC `agent:progress` → `preload.js` passthrough → renderer `window.dispatchEvent('agent:progress')` → `flowView.js` `_applyProgress()` + `state.progress[]`.

**FlowView** (`src/renderer/flowView.js`, ~900 LOC): vanilla-JS SVG DAG with glassmorphism nodes, particle edges, phase groups, minimap. Two panels: **Global Live Activity** (when no node selected: running count, bottleneck, recent events, health badge) and **Agent Inspector** (7 subtabs: Overview, Activity, I/O, Messages, Tools, Health, Raw). Upstream/downstream chip navigation from topology edges. `lastEventFor` keeps per-node event history (cap 200/node); `allEvents` for the global tail (cap 500).

**Design system** (`src/renderer/styles.css`): professional dev-tool palette — `--bg: #F6F5F0`, `--surface: #FFFFFF`, `--accent: #176B63`. Status colors only on dots/badges. No gradients, no neon, no glow. Dark canvas only for the Flow Graph tab (`flow-canvas` background `#0A0F1C`).

**Theme** (`settingsStore` field `theme`): `auto` (follows `prefers-color-scheme` via live `change` listener) / `light` / `dark`. Stored in localStorage + settingsStore; applied via `[data-theme]` on `<html>`.

**Auto Loop** (`src/renderer/app.js` `autoLoopTick`): When toggled ON in the Dashboard "🤖 Auto Loop" card, the renderer polls `POST /v1/autonomy/next-task` to pick a single finding/idea, submits it via `runTask()`, waits for `agent:run-finished`, and repeats indefinitely. Persists completed IDs + iteration counter to localStorage. Safety: stops after 3 consecutive errors; refuses to start while elevated unless `fullPower.autoLoopAllowAdmin` is on; toggle state is NOT auto-restored on relaunch.

**Full Power** (`settingsStore.fullPower`): three independent flags — `bypassSafeCommands` (sets `AGENT_BYPASS_SAFE_COMMANDS=1` on backend spawn), `requireAdmin` (Windows UAC self-elevation via `Start-Process -Verb RunAs` with anti-loop guard), `autoLoopAllowAdmin` (explicit second consent for Auto Loop to run elevated). All off by default. `AGENT_NO_ELEVATE=1` skips UAC for dev.

**IPC bridge** (`src/main/main.js`): `agent:topology`, `agent:send`, `agent:cancel`, `agent:progress`, `agent:observability`, `agent:autonomy-status`, `agent:autonomy-scan`, `agent:autonomy-next-task`, `agent:full-power-status`, `doctor:run`, `doctor:event`.

## Skills (workflow methodology)

15 skills in `skills/` adapted from obra/superpowers. Key ones:
- brainstorming → writing-plans (specification before implementation)
- test-driven-development (red-green-refactor)
- subagent-driven-development (parallel agents for independent workstreams)
- verification-before-completion (run the project's own checks before declaring done)
- systematic-debugging (4-phase root cause before fix)
- project-doctor (autonomous scan→plan→patch→verify on the current workspace)

## Known issues / gotchas

- `test_phase2_approval_flow.py` has 3 `@pytest.mark.slow` tests that run 4 full rework loops with real git worktrees. They hang on Windows due to `ntpath.realpath` junction recursion — skip by default via `addopts = "-m 'not slow'"` in pyproject.toml.
- Temp dirs for test suite are redirected to `<repo>-pytest-tmp/` (repo parent) by `conftest.py` to avoid Windows %TEMP% orphans holding GBs of WAL files.
- `_STORE_LOCK` in `durable_execution.py` is `RLock` (not `Lock`) because `prepare()` calls `get()` while holding the lock.
- OpenHands Agent objects (`openhands_worker.py`) are Pydantic models with `frozen=True`; mutating `agent.mcp_config` is not allowed. The retry block creates a fresh `Agent()` instead.
- **Hanging run after reviewer_decision=blocked**: the default.yaml route `blocked → reporter_end` (no-op) skipped `reporter` (builds assistantText) and `finalize_workspace`, causing an empty response and FlowView banner stuck at "ĐANG CHẠY". Fixed in this revision by routing blocked/has_result/default through `reporter` instead. Regression test: `tests/test_deterministic_workflow.py` line 40-52.
- **UAC elevation on Windows**: `requireAdmin` stores a timestamp-based anti-loop guard (`elevation-attempt.txt`); if two launches happen within 30s the flag auto-disables to prevent infinite UAC prompts.
- **Dark mode is dev-tool pattern only**: the Flow Graph canvas uses a fixed dark background (`#0A0F1C` in `.flow-canvas`). The rest of the UI is light-surface. The theme dropdown in Settings only affects the main UI palette, not the canvas.

## Python conventions

- `from __future__ import annotations` in every file
- Type hints everywhere; `dict[str, Any]` for JSON-like shapes
- `@dataclass` for domain models, each with `to_dict()` / `from_dict()`
- `write_debug_event("category.action", {...})` for structured logs
- `telemetry.start_span("name", {...}) as span:` for tracing
- Env vars for all configurable values, reasonable defaults
- No hardcoded paths, secrets, or model names
