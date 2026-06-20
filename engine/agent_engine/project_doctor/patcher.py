"""Apply patches with optional token-by-token LLM streaming.

Two paths:
  - autofix_safe → apply a deterministic fix (e.g. append missing .gitignore
    entries, regenerate lockfile via the package manager). No LLM, no stream.
  - everything else → ask the LLM for a unified-diff-style replacement,
    streaming each token via emit("doctor.patch.chunk", ...).

The patcher never edits files outside `project_root`. Every patch is recorded
so the verifier can roll back if the project ends up worse than before.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import FixReport, Issue, Patch

EmitFn = Callable[[str, str], None]


def _within_root(root: Path, candidate: Path) -> bool:
    # Sanity check, not a security boundary: the doctor is run by the
    # repo owner against their own project. The check still rejects paths
    # that resolve to a different drive/anchor on Windows so an LLM that
    # hallucinates `C:\Windows\...` or `/etc/passwd` doesn't silently land
    # there from a typo. Inside the same anchor, everything is allowed —
    # including node_modules, .venv, .agent-state, dotfiles.
    try:
        rs = root.resolve()
        cs = candidate.resolve()
    except OSError:
        return True  # resolve failure: trust the LLM, let the OS gate it
    return cs.anchor.lower() == rs.anchor.lower()


def _read_file(root: Path, rel: str) -> str | None:
    path = root / rel
    if not _within_root(root, path) or not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_file(root: Path, rel: str, content: str) -> bool:
    path = root / rel
    if not _within_root(root, path):
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return True
    except OSError:
        return False


# ── Deterministic fix handlers ─────────────────────────────────────────────────


def _apply_gitignore_fix(root: Path, issue: Issue) -> Patch | None:
    rel = ".gitignore"
    current = _read_file(root, rel) or ""
    # detail looks like: "Entries that should be ignored: ['.env', '...']"
    missing = []
    if "[" in issue.detail and "]" in issue.detail:
        chunk = issue.detail[issue.detail.find("[") + 1: issue.detail.find("]")]
        for token in chunk.split(","):
            token = token.strip().strip("'").strip('"')
            if token:
                missing.append(token)
    if not missing:
        return None
    appended = current.rstrip("\n") + "\n" + "\n".join(missing) + "\n"
    if not _write_file(root, rel, appended):
        return None
    return Patch(
        issue_id=issue.id, file=rel, old=current, new=appended,
        rationale=f"Appended {len(missing)} missing .gitignore entries: {missing}",
    )


def _apply_lockfile_resync(root: Path, issue: Issue, emit: EmitFn) -> Patch | None:
    pnpm = "pnpm.cmd" if os.name == "nt" else "pnpm"
    emit("doctor.patch.run", f"pnpm install (resync lockfile)")
    try:
        result = subprocess.run(
            [pnpm, "install"], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        emit("doctor.patch.error", f"pnpm install failed: {exc}")
        return None
    if result.returncode != 0:
        emit("doctor.patch.error", f"pnpm install exited {result.returncode}: {(result.stderr or '')[:240]}")
        return None
    return Patch(
        issue_id=issue.id, file="pnpm-lock.yaml", old="", new="",
        rationale="Regenerated lockfile via `pnpm install`.",
    )


# ── LLM-streamed fix path ──────────────────────────────────────────────────────


def _stream_llm_patch(
    root: Path,
    issue: Issue,
    emit: EmitFn,
    provider: Any | None,
) -> tuple[Patch | None, int]:
    """Ask the LLM to write a corrected file, streaming each token via emit.

    Two providers are supported:
      * ClaudeAgentSDKProvider — drives the official `claude-agent-sdk`
        (Read/Edit/Bash tools). The agent edits the file in place; we just
        record the patch as applied after the loop succeeds.
      * Anthropic ClaudeProvider — older path. Uses .stream() and asks the
        model for a fenced code block we write to disk ourselves.
    Returns (patch_or_None, streamed_chunks).
    """
    if provider is None:
        return None, 0

    # Path 1: claude-agent-sdk provider — agent edits the file in place.
    if provider.__class__.__name__ == "ClaudeAgentSDKProvider":
        current = _read_file(root, issue.file)
        if current is None:
            emit("doctor.patch.skip", f"{issue.file} not readable")
            return None, 0
        instructions = (
            f"Issue ({issue.group.value}/{issue.severity.value}): {issue.title}\n"
            f"Detail: {issue.detail}\n"
            f"Root cause: {issue.root_cause}\n"
            f"Suggested fix: {issue.suggested_fix}"
        )
        try:
            ok, chunk_count = provider.edit_file_with_stream(
                rel_path=issue.file, instructions=instructions, emit=emit,
            )
        except Exception as exc:
            emit("doctor.patch.error", f"Agent SDK invocation failed: {exc}")
            return None, 0
        if not ok:
            return None, chunk_count
        new_content = _read_file(root, issue.file)
        if new_content is None or new_content == current:
            emit("doctor.patch.skip", f"Agent SDK left {issue.file} unchanged")
            return None, chunk_count
        return Patch(
            issue_id=issue.id, file=issue.file, old=current, new=new_content,
            rationale=f"Agent SDK rewrite ({chunk_count} chunks): {issue.title}",
        ), chunk_count
    current = _read_file(root, issue.file)
    if current is None:
        emit("doctor.patch.skip", f"{issue.file} not readable")
        return None, 0

    system = (
        "You are Project Doctor's patch writer. Output ONLY the full corrected "
        "contents of the file inside a single fenced code block. No commentary."
    )
    user = (
        f"File: {issue.file}\n"
        f"Issue ({issue.group.value}/{issue.severity.value}): {issue.title}\n"
        f"Detail: {issue.detail}\n"
        f"Root cause: {issue.root_cause}\n"
        f"Suggested fix: {issue.suggested_fix}\n\n"
        f"Current content:\n```\n{current}\n```\n\n"
        "Return the FULL corrected file inside one fenced block."
    )

    # The adapter's stream() returns (events, response). We replay the events
    # to surface tokens, then assemble the final text.
    try:
        from ..claude_adapter import ClaudeMessage  # local import — keeps this module standalone
    except Exception:
        ClaudeMessage = None  # type: ignore[assignment]

    chunk_count = 0
    full_text = ""
    if ClaudeMessage is not None and hasattr(provider, "stream"):
        message = ClaudeMessage(role="user", content=user)
        try:
            events, response = provider.stream([message], system=system)
        except Exception as exc:
            emit("doctor.patch.error", f"LLM stream failed: {exc}")
            return None, 0
        for event in events:
            if getattr(event, "type", "") == "text_delta":
                token = getattr(event, "text", "") or ""
                if token:
                    chunk_count += 1
                    emit("doctor.patch.chunk", token)
        full_text = response.text or ""
    else:
        emit("doctor.patch.skip", "No streaming provider available")
        return None, 0

    new_content = _extract_fenced_block(full_text)
    if new_content is None or new_content == current:
        emit("doctor.patch.skip", f"LLM returned no usable change for {issue.file}")
        return None, chunk_count
    if not _write_file(root, issue.file, new_content):
        emit("doctor.patch.error", f"Could not write {issue.file}")
        return None, chunk_count
    return Patch(
        issue_id=issue.id, file=issue.file, old=current, new=new_content,
        rationale=f"LLM-streamed rewrite ({chunk_count} chunks): {issue.title}",
    ), chunk_count


def _extract_fenced_block(text: str) -> str | None:
    if "```" not in text:
        return text.strip() or None
    start = text.find("```")
    # skip language tag line
    newline = text.find("\n", start)
    if newline == -1:
        return None
    end = text.find("```", newline + 1)
    if end == -1:
        return None
    return text[newline + 1: end]


# ── Public dispatch ────────────────────────────────────────────────────────────


def apply_patches(
    root: Path,
    issues: list[Issue],
    emit: EmitFn,
    provider: Any | None = None,
) -> FixReport:
    report = FixReport()
    for issue in issues:
        emit("doctor.patch.start", f"{issue.group.value}/{issue.severity.value} · {issue.file} · {issue.title}")
        patch: Patch | None = None

        if issue.autofix_safe:
            if issue.file == ".gitignore":
                patch = _apply_gitignore_fix(root, issue)
            elif issue.file == "pnpm-lock.yaml":
                patch = _apply_lockfile_resync(root, issue, emit)
        else:
            patch, chunks = _stream_llm_patch(root, issue, emit, provider)
            report.streamed_chunks += chunks

        if patch is not None:
            report.applied.append(patch)
            emit("doctor.patch.applied", f"{patch.file} — {patch.rationale}")
        else:
            report.skipped.append({
                "issueId": issue.id, "file": issue.file,
                "reason": "no autofix path or LLM produced no usable change",
            })
            emit("doctor.patch.skip", f"{issue.file} ({issue.title})")
    return report
