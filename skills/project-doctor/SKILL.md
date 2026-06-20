# Project Doctor — He Thong Agent

Autonomous scan → plan → patch → verify pipeline for the current workspace.

## When to use

- Build/run failures whose root cause is not yet obvious.
- After a large merge or refactor, before shipping.
- Any time the user types "fix the project" / "ê fix đống này đi" / "check lại đi".

## What it does

1. **Scan** — deterministic checks: Python AST, `node --check`, secret patterns,
   `.gitignore` basics, lockfile drift. Produces line-anchored `Issue` records.
2. **Plan** — orders issues by group (critical → logic → security → perf → ui → hygiene)
   and severity (blocker → major → minor).
3. **Patch** — applies deterministic autofixes immediately (gitignore append,
   `pnpm install`). For everything else, streams an LLM patch token-by-token
   via `emit("doctor.patch.chunk", ...)` so the UI can render typewriter-style.
   Provider preference order:
   - **`claude-agent-sdk`** (default when installed). Uses the official Agent
     SDK with `Read`/`Edit`/`Bash`/`Glob`/`Grep` tools — the agent edits
     files in place. Same SDK that powers Claude Code.
   - **`anthropic` SDK fallback**. Used when the Agent SDK isn't installed or
     fails to initialize. Streams text and we apply the fenced block ourselves.
4. **Verify** — re-runs the project's own verification commands (`pnpm run check`,
   `pytest tests/`, `compileall`) and reports PASS/FAIL with the failing tails.

## How to invoke

### From the UI

Open the **Doctor** tab → click **Chạy chẩn đoán**. The renderer subscribes
to `/v1/doctor` (NDJSON) and shows every stage as it fires, including the
live patch stream.

### From the terminal

```bash
.venv/Scripts/python.exe scripts/doctor.py
.venv/Scripts/python.exe scripts/doctor.py --no-llm           # autofix only
.venv/Scripts/python.exe scripts/doctor.py --json > out.json  # CI-friendly
```

Patch chunks render inline (no timestamp) so you see Claude typing live.

## Scope of changes

The doctor is run by the repo owner against their own project. It writes
anywhere on the same drive as the workspace, including `node_modules/`,
`.venv/`, `.agent-state/`, dotfiles, and other directories normally treated
as off-limits. Bash is enabled so the agent can run linters, reinstall
dependencies, restart dev servers, etc. There is no confirmation gate —
**full autonomy, full diff visible in the Doctor tab**.

The only sanity check is that paths must resolve to the same drive anchor
as the workspace (Windows: `C:\` ≠ `D:\`). This is a typo guard for LLM
output like `/etc/passwd` or `C:\Windows\System32\...`, not a security
boundary; same-drive system files are reachable if the workspace is on
the system drive.

## What it still WILL NOT do

- Push to a remote, force-push, or amend published commits.
- Elevate OS privileges via `runas` / UAC.
- Rewrite a file when the LLM returns no usable change — it records the
  issue as `skipped` and moves on.

## Output contract

```json
{
  "scan":   { "issues": [...], "countsByGroup": {...} },
  "plan":   [ Issue, ... ],
  "fix":    { "applied": [Patch, ...], "skipped": [...], "streamedChunks": N },
  "verify": { "ok": true/false, "runs": [{ "command": ..., "ok": ..., "code": ... }] },
  "ok":     true/false
}
```

## Streaming stages

| stage | when |
|---|---|
| `doctor.start` | once, at entry |
| `doctor.scan` | scan finished |
| `doctor.scan.group` | per-group count |
| `doctor.plan` | plan length |
| `doctor.plan.item` | top-10 ordered issues |
| `doctor.patch.start` | per-issue, before fix |
| `doctor.patch.chunk` | every LLM token (typewriter) |
| `doctor.patch.applied` | per-issue, after fix |
| `doctor.patch.skip` | per-issue, skipped |
| `doctor.patch.run` | shell command for deterministic fixes |
| `doctor.patch.done` | summary |
| `doctor.verify.*` | each re-verify command |
| `doctor.done` | PASS/FAIL |
