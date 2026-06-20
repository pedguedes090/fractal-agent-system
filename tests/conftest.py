"""Test fixtures — clean state before each test, keep temp dirs small.

By default every test runs with AGENT_TEST_INMEM=1 so langgraph uses
InMemorySaver instead of SqliteSaver. This prevents the ~1GB/min WAL
growth observed when running the full pipeline under SqliteSaver, and
avoids orphan tempdirs holding GBs of langgraph-checkpoints.sqlite-wal
when a test is killed (Windows file lock prevents auto-cleanup).

Opt out for an individual test that needs real persistence with the
@pytest.mark.real_sqlite marker.
"""

from __future__ import annotations

import atexit
import gc
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


_temp_files: list[str] = []
# All test tempdirs live under this single root NEXT TO the repo (not inside —
# tests that walk up looking for .git would otherwise find the project repo).
# One rmtree at session end reclaims everything; no Windows-junction recursion,
# no orphaned multi-GB WAL leftovers in the OS temp dir.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEST_TMP_ROOT = _REPO_ROOT.parent / f"{_REPO_ROOT.name}-pytest-tmp"


def _cleanup_temp_files() -> None:
    for path in _temp_files:
        try:
            if os.path.isfile(path):
                Path(path).unlink(missing_ok=True)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
    _temp_files.clear()


atexit.register(_cleanup_temp_files)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_sqlite: opt out of InMemorySaver — use a real SQLite checkpointer (slow, writes WAL).",
    )
    os.environ.setdefault("AGENT_TEST_INMEM", "1")
    # Quieter test boot — saves ~1-2s/process printing the OpenHands banner.
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    # Redirect every tempfile.* call to the in-repo root so cleanup is local.
    _TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(_TEST_TMP_ROOT)
    os.environ["TMPDIR"] = str(_TEST_TMP_ROOT)
    os.environ["TEMP"] = str(_TEST_TMP_ROOT)
    os.environ["TMP"] = str(_TEST_TMP_ROOT)


@pytest.fixture(autouse=True)
def _inmem_checkpointer(request: pytest.FixtureRequest):
    """Per-test override: tests marked real_sqlite get the SQLite checkpointer back."""
    if request.node.get_closest_marker("real_sqlite"):
        prev = os.environ.get("AGENT_TEST_INMEM")
        os.environ["AGENT_TEST_INMEM"] = "0"
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("AGENT_TEST_INMEM", None)
            else:
                os.environ["AGENT_TEST_INMEM"] = prev
    else:
        yield


@pytest.fixture(autouse=True)
def clean_agent_state():
    paths = [
        Path.cwd() / ".agent-state",
        Path(os.environ["AGENT_ENGINE_STATE_DIR"]) if os.environ.get("AGENT_ENGINE_STATE_DIR") else None,
    ]
    for p in paths:
        if p and p.exists():
            shutil.rmtree(p, ignore_errors=True)
    yield
    for p in paths:
        if p and p.exists():
            shutil.rmtree(p, ignore_errors=True)
    _cleanup_temp_files()


def _close_lingering_sqlite_connections() -> None:
    # SQLite handles held by abandoned objects keep WAL files locked on Windows,
    # which blocks rmtree of tempdirs and leaves multi-GB leftovers.
    gc.collect()
    for obj in list(gc.get_objects()):
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                obj.close()
            except Exception:
                pass


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _close_lingering_sqlite_connections()
    _cleanup_temp_files()
    # Single rmtree on the in-repo test tempdir — no %TEMP% globbing required.
    # Anything Windows still holds locked is silently ignored; the user can
    # delete .pytest-tmp/ manually if a hung process leaves files behind.
    if _TEST_TMP_ROOT.exists():
        shutil.rmtree(_TEST_TMP_ROOT, ignore_errors=True)
