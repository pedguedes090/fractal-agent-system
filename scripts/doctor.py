#!/usr/bin/env python3
"""Project Doctor CLI — same pipeline as the HTTP endpoint, prints to stdout.

Usage:
  python scripts/doctor.py [path] [--model claude-opus-4-7] [--no-llm] [--json]

  path        directory to scan (default: cwd)
  --model     override the Claude model id
  --no-llm    skip the LLM patch path entirely; only deterministic autofixes run
  --json      print the final result as JSON on stdout (events still stream to stderr)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Make `from agent_engine.project_doctor import ...` work whether you ran this
# from the repo root or from inside scripts/.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(_ROOT / "engine"))

from agent_engine.project_doctor import run_doctor  # noqa: E402


def _make_emit(stream_target):
    start = time.monotonic()

    def emit(stage: str, detail: str) -> None:
        elapsed = time.monotonic() - start
        # Patch chunks come at high rate — render them as raw text so the user
        # sees the LLM typing live, the way they would in a chat UI.
        if stage == "doctor.patch.chunk":
            stream_target.write(detail)
            stream_target.flush()
            return
        line = f"[{elapsed:6.2f}s] {stage:<24s} {detail}\n"
        stream_target.write(line)
        stream_target.flush()

    return emit


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Project Doctor on a workspace.")
    parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: cwd)")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7"))
    parser.add_argument("--no-llm", action="store_true", help="Skip the LLM patch path; deterministic autofixes only")
    parser.add_argument("--json", action="store_true", help="Print final result as JSON on stdout")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    provider = None
    if not args.no_llm:
        api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        # Try claude-agent-sdk first (Read/Edit/Bash agent loop), fall back
        # to the lower-level Anthropic SDK provider.
        try:
            from agent_engine.project_doctor.agent_sdk_provider import maybe_build_provider
            provider = maybe_build_provider(cwd=root, model=args.model, api_key=api_key or None)
        except Exception as exc:
            print(f"warning: Agent SDK disabled ({exc})", file=sys.stderr)
        if provider is None and api_key:
            try:
                from agent_engine.claude_adapter import ClaudeConfig, ClaudeProvider
                provider = ClaudeProvider(ClaudeConfig(api_key=api_key, model=args.model))
            except Exception as exc:
                print(f"warning: Anthropic SDK fallback disabled ({exc})", file=sys.stderr)

    # Events go to stderr so `--json` keeps stdout clean.
    emit = _make_emit(sys.stderr)
    result = run_doctor(root, provider=provider, emit=emit)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        scan = result.get("scan", {})
        fix = result.get("fix", {})
        verify_block = result.get("verify", {})
        print()
        print(f"Scan: {len(scan.get('issues') or [])} issue(s)")
        print(f"Fix:  {len(fix.get('applied') or [])} applied, "
              f"{len(fix.get('skipped') or [])} skipped, "
              f"{fix.get('streamedChunks', 0)} LLM chunks")
        print(f"Verify: {'PASS' if verify_block.get('ok') else 'FAIL'} "
              f"({len(verify_block.get('runs') or [])} command(s))")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
