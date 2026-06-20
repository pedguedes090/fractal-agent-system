from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


CONTROL_PLANE_DB_NAME = "agent-state.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_abs(path: str | Path) -> Path:
    # Path.resolve() invokes ntpath.realpath() on Windows, which can recurse
    # through junctions and hang on workspaces containing OneDrive/Dropbox or
    # nested git worktrees. abspath() + normpath() gives the same canonical
    # form for our use without touching the filesystem.
    return Path(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))


def control_plane_path(state_dir: str | Path) -> Path:
    root = _safe_abs(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / CONTROL_PLANE_DB_NAME


def configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA busy_timeout = 30000")
    deadline = time.monotonic() + 30.0
    while True:
        try:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA max_page_count = 2147483647")  # SQLite max — no hard cap
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA cache_size = -128000")  # 128MB cache
    conn.execute("PRAGMA temp_store = MEMORY")  # keep temp objects off disk


def migrate_legacy_tables(
    target_path: str | Path,
    legacy_path: str | Path,
    table_names: Iterable[str],
) -> bool:
    target = _safe_abs(target_path)
    legacy = _safe_abs(legacy_path)
    if target == legacy or not legacy.exists():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    migration_key = f"legacy:{legacy.name}"
    conn = sqlite3.connect(str(target), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        configure_connection(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_migrations (
              key TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL
            )
            """
        )
        conn.commit()

        conn.execute("ATTACH DATABASE ? AS legacy_state", (str(legacy),))
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM state_migrations WHERE key = ?", (migration_key,)).fetchone():
                conn.rollback()
                return False
            for table_name in table_names:
                if not _table_exists(conn, "main", table_name) or not _table_exists(conn, "legacy_state", table_name):
                    continue
                target_columns = _table_columns(conn, "main", table_name)
                legacy_columns = set(_table_columns(conn, "legacy_state", table_name))
                shared_columns = [column for column in target_columns if column in legacy_columns]
                if not shared_columns:
                    continue
                columns_sql = ", ".join(_quote_identifier(column) for column in shared_columns)
                table_sql = _quote_identifier(table_name)
                conn.execute(
                    f"INSERT OR IGNORE INTO main.{table_sql} ({columns_sql}) "
                    f"SELECT {columns_sql} FROM legacy_state.{table_sql}"
                )
            conn.execute(
                "INSERT INTO state_migrations (key, applied_at) VALUES (?, ?)",
                (migration_key, _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("DETACH DATABASE legacy_state")
        return True
    finally:
        conn.close()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, schema: str, table_name: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, schema: str, table_name: str) -> list[str]:
    table_sql = _quote_identifier(table_name)
    return [str(row["name"]) for row in conn.execute(f"PRAGMA {schema}.table_info({table_sql})").fetchall()]
