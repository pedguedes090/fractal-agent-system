"""Run repository — repository interface with SQLite implementation.

Architecture:
  - RunRepository: abstract interface (Protocol)
  - SQLiteRunRepository: SQLite implementation using the control-plane DB
  - Designed so domain/orchestration layers never touch SQL directly
  - Indexes: conversation_id, run_id, status, created_at
  - Idempotency keys prevent duplicate runs/tool calls
  - Optimistic locking for concurrent status updates
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from ..state_store import configure_connection
from .models import (
    AgentRun,
    AgentStep,
    Artifact,
    Conversation,
    Memory,
    Message,
    ModelUsageRecord,
    Plan,
    RunStatus,
    ToolCall,
    _new_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse(text: str | None, fallback: Any = None) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


# ---------------------------------------------------------------------------
# Repository interface
# ---------------------------------------------------------------------------


class RunRepository(Protocol):
    """Interface for agent run storage. Implementation can be SQLite, Postgres, etc."""

    # --- Runs ---

    def create_run(self, run: AgentRun) -> AgentRun: ...

    def get_run(self, run_id: str) -> AgentRun | None: ...

    def list_runs(
        self,
        *,
        conversation_id: str = "",
        session_id: str = "",
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentRun]: ...

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        error: str | None = None,
    ) -> bool: ...

    def delete_run(self, run_id: str) -> bool: ...

    # --- Steps ---

    def create_step(self, step: AgentStep) -> AgentStep: ...

    def get_step(self, step_id: str) -> AgentStep | None: ...

    def list_steps(self, run_id: str) -> list[AgentStep]: ...

    def update_step(self, step: AgentStep) -> AgentStep: ...

    # --- Conversations ---

    def create_conversation(self, conversation: Conversation) -> Conversation: ...

    def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    def list_conversations(self, *, limit: int = 20, offset: int = 0) -> list[Conversation]: ...

    # --- Messages ---

    def create_message(self, message: Message) -> Message: ...

    def list_messages(self, conversation_id: str, *, limit: int = 100) -> list[Message]: ...

    # --- Plans ---

    def save_plan(self, plan: Plan) -> Plan: ...

    def get_plan(self, plan_id: str) -> Plan | None: ...

    def get_plan_by_run(self, run_id: str) -> Plan | None: ...

    # --- Tool calls ---

    def create_tool_call(self, call: ToolCall) -> ToolCall: ...

    def update_tool_call(self, call: ToolCall) -> ToolCall: ...

    def list_tool_calls(self, run_id: str) -> list[ToolCall]: ...

    def get_tool_call_by_idempotency(self, idempotency_key: str) -> ToolCall | None: ...

    # --- Artifacts ---

    def create_artifact(self, artifact: Artifact) -> Artifact: ...

    def list_artifacts(self, run_id: str) -> list[Artifact]: ...

    # --- Memories ---

    def save_memory(self, memory: Memory) -> Memory: ...

    def list_memories(self, run_id: str = "", *, kind: str = "", limit: int = 50) -> list[Memory]: ...

    # --- Usage ---

    def record_usage(self, usage: ModelUsageRecord) -> ModelUsageRecord: ...

    def list_usage(self, run_id: str) -> list[ModelUsageRecord]: ...

    # --- Checkpoints ---

    def save_checkpoint(self, run_id: str, step_name: str, data: dict[str, Any]) -> dict[str, Any]: ...

    def get_latest_checkpoint(self, run_id: str) -> dict[str, Any] | None: ...

    # --- Health ---

    def health_check(self) -> bool: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


class SQLiteRunRepository:
    """SQLite-based RunRepository implementation using control-plane DB."""

    MIGRATION_VERSION = 2

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        configure_connection(self.conn)
        self._migrate()

    def _migrate(self) -> None:
        """Create tables if not exist. Idempotent."""
        self.conn.executescript(
            """
            -- Conversations
            CREATE TABLE IF NOT EXISTS storage_conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                workspace_path TEXT NOT NULL DEFAULT '',
                messages_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conv_created ON storage_conversations(created_at);

            -- Messages
            CREATE TABLE IF NOT EXISTS storage_messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tool_call_id TEXT,
                name TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES storage_conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_msg_conv ON storage_messages(conversation_id, created_at);

            -- Runs
            CREATE TABLE IF NOT EXISTS storage_runs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                task TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                prompt_version TEXT NOT NULL DEFAULT '',
                workspace_path TEXT NOT NULL DEFAULT '',
                plan_json TEXT,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                lock_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_run_conv ON storage_runs(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_run_session ON storage_runs(session_id);
            CREATE INDEX IF NOT EXISTS idx_run_status ON storage_runs(status);
            CREATE INDEX IF NOT EXISTS idx_run_created ON storage_runs(created_at);

            -- Steps
            CREATE TABLE IF NOT EXISTS storage_steps (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                duration_ms REAL NOT NULL DEFAULT 0,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES storage_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_step_run ON storage_steps(run_id);

            -- Tool calls
            CREATE TABLE IF NOT EXISTS storage_tool_calls (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL,
                input_json TEXT NOT NULL DEFAULT '{}',
                output_json TEXT,
                error TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                duration_ms REAL NOT NULL DEFAULT 0,
                truncated INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES storage_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_tc_run ON storage_tool_calls(run_id);
            CREATE INDEX IF NOT EXISTS idx_tc_idempotency ON storage_tool_calls(idempotency_key);

            -- Artifacts
            CREATE TABLE IF NOT EXISTS storage_artifacts (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL DEFAULT 'file',
                name TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                checksum TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES storage_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_art_run ON storage_artifacts(run_id);

            -- Memories
            CREATE TABLE IF NOT EXISTS storage_memories (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'working',
                source TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.5,
                activation REAL NOT NULL DEFAULT 0.0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mem_run ON storage_memories(run_id);
            CREATE INDEX IF NOT EXISTS idx_mem_kind ON storage_memories(kind, created_at);

            -- Usage records
            CREATE TABLE IF NOT EXISTS storage_usage (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES storage_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_usage_run ON storage_usage(run_id);

            -- Checkpoints
            CREATE TABLE IF NOT EXISTS storage_checkpoints (
                run_id TEXT NOT NULL,
                step_name TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, step_name),
                FOREIGN KEY (run_id) REFERENCES storage_runs(id)
            );

            -- Migration tracking
            CREATE TABLE IF NOT EXISTS storage_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

        # Apply migrations
        cursor = self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM storage_migrations")
        current_version = cursor.fetchone()[0]

        if current_version < self.MIGRATION_VERSION:
            self.conn.execute(
                "INSERT OR REPLACE INTO storage_migrations (version, applied_at) VALUES (?, ?)",
                (self.MIGRATION_VERSION, _now()),
            )
            self.conn.commit()

    # --- Runs ---

    def create_run(self, run: AgentRun) -> AgentRun:
        if not run.id:
            run.id = _new_id()
        self.conn.execute(
            """
            INSERT INTO storage_runs (
                id, conversation_id, session_id, task, status, prompt_version,
                workspace_path, plan_json, result_json, error, metadata_json,
                lock_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                run.id,
                run.conversation_id,
                run.session_id,
                run.task,
                run.status,
                run.prompt_version,
                run.workspace_path,
                _json(run.plan.to_dict() if run.plan else None),
                _json(run.result),
                run.error,
                _json(run.metadata),
                run.created_at,
                run.updated_at,
            ),
        )
        self.conn.commit()
        return run

    def get_run(self, run_id: str) -> AgentRun | None:
        row = self.conn.execute("SELECT * FROM storage_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def list_runs(
        self,
        *,
        conversation_id: str = "",
        session_id: str = "",
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentRun]:
        query = "SELECT * FROM storage_runs WHERE 1=1"
        params: list[Any] = []
        if conversation_id:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Optimistic locking: only updates if expected_status matches current."""
        if expected_status:
            result = self.conn.execute(
                """
                UPDATE storage_runs
                SET status = ?, error = COALESCE(?, error),
                    updated_at = ?, completed_at = CASE WHEN ? IN ('completed','failed','cancelled') THEN ? ELSE completed_at END,
                    lock_version = lock_version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    status,
                    error,
                    _now(),
                    status,
                    _now(),
                    run_id,
                    expected_status,
                ),
            )
        else:
            result = self.conn.execute(
                """
                UPDATE storage_runs
                SET status = ?, error = COALESCE(?, error),
                    updated_at = ?, completed_at = CASE WHEN ? IN ('completed','failed','cancelled') THEN ? ELSE completed_at END,
                    lock_version = lock_version + 1
                WHERE id = ?
                """,
                (status, error, _now(), status, _now(), run_id),
            )
        self.conn.commit()
        return result.rowcount > 0

    def delete_run(self, run_id: str) -> bool:
        self.conn.execute("DELETE FROM storage_usage WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM storage_artifacts WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM storage_memories WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM storage_tool_calls WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM storage_steps WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM storage_checkpoints WHERE run_id = ?", (run_id,))
        result = self.conn.execute("DELETE FROM storage_runs WHERE id = ?", (run_id,))
        self.conn.commit()
        return result.rowcount > 0

    # --- Steps ---

    def create_step(self, step: AgentStep) -> AgentStep:
        if not step.id:
            step.id = _new_id()
        self.conn.execute(
            """
            INSERT INTO storage_steps (id, run_id, name, role, status, input_json, output_json, error, duration_ms, checkpoint_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (step.id, step.run_id, step.name, step.role, step.status,
             _json(step.input_data), _json(step.output_data), step.error,
             step.duration_ms, _json(step.checkpoint_data), step.created_at, step.updated_at),
        )
        self.conn.commit()
        return step

    def get_step(self, step_id: str) -> AgentStep | None:
        row = self.conn.execute("SELECT * FROM storage_steps WHERE id = ?", (step_id,)).fetchone()
        if not row:
            return None
        return self._row_to_step(row)

    def list_steps(self, run_id: str) -> list[AgentStep]:
        rows = self.conn.execute(
            "SELECT * FROM storage_steps WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [self._row_to_step(row) for row in rows]

    def update_step(self, step: AgentStep) -> AgentStep:
        step.updated_at = _now()
        self.conn.execute(
            """
            UPDATE storage_steps
            SET status = ?, output_json = ?, error = ?, duration_ms = ?, checkpoint_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (step.status, _json(step.output_data), step.error, step.duration_ms,
             _json(step.checkpoint_data), step.updated_at, step.id),
        )
        self.conn.commit()
        return step

    # --- Conversations ---

    def create_conversation(self, conversation: Conversation) -> Conversation:
        if not conversation.id:
            conversation.id = _new_id()
        self.conn.execute(
            """
            INSERT INTO storage_conversations (id, title, workspace_path, messages_json, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation.id, conversation.title, conversation.workspace_path,
             _json([m.to_dict() for m in conversation.messages]),
             _json(conversation.metadata), conversation.created_at, conversation.updated_at),
        )
        self.conn.commit()
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        row = self.conn.execute("SELECT * FROM storage_conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not row:
            return None
        return self._row_to_conversation(row)

    def list_conversations(self, *, limit: int = 20, offset: int = 0) -> list[Conversation]:
        rows = self.conn.execute(
            "SELECT * FROM storage_conversations ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_conversation(row) for row in rows]

    # --- Messages ---

    def create_message(self, message: Message) -> Message:
        if not message.id:
            message.id = _new_id()
        self.conn.execute(
            """
            INSERT INTO storage_messages (id, conversation_id, role, content, tool_call_id, name, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message.id, message.conversation_id, message.role, message.content,
             message.tool_call_id, message.name, _json(message.metadata), message.created_at),
        )
        self.conn.commit()
        return message

    def list_messages(self, conversation_id: str, *, limit: int = 100) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM storage_messages WHERE conversation_id = ? ORDER BY created_at ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [self._row_to_message(row) for row in rows]

    # --- Plans ---

    def save_plan(self, plan: Plan) -> Plan:
        if not plan.id:
            plan.id = _new_id()
        # Store plan in runs table or separate
        self.conn.execute(
            "UPDATE storage_runs SET plan_json = ? WHERE id = ?",
            (_json(plan.to_dict()), plan.run_id),
        )
        self.conn.commit()
        return plan

    def get_plan(self, plan_id: str) -> Plan | None:
        row = self.conn.execute(
            "SELECT plan_json FROM storage_runs WHERE plan_json LIKE ?", (f"%{plan_id}%",)
        ).fetchone()
        if not row or not row["plan_json"]:
            return None
        return Plan.from_dict(_parse(row["plan_json"], {}))

    def get_plan_by_run(self, run_id: str) -> Plan | None:
        row = self.conn.execute("SELECT plan_json FROM storage_runs WHERE id = ?", (run_id,)).fetchone()
        if not row or not row["plan_json"]:
            return None
        data = _parse(row["plan_json"])
        if not data:
            return None
        return Plan.from_dict(data)

    # --- Tool calls ---

    def create_tool_call(self, call: ToolCall) -> ToolCall:
        if not call.id:
            call.id = _new_id()
        if call.idempotency_key:
            existing = self.get_tool_call_by_idempotency(call.idempotency_key)
            if existing:
                return existing

        try:
            self.conn.execute(
                """
                INSERT INTO storage_tool_calls (id, run_id, step_id, tool_name, input_json, output_json, error, status, duration_ms, truncated, idempotency_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (call.id, call.run_id, call.step_id, call.tool_name, _json(call.input_params),
                 _json(call.output), call.error, call.status, call.duration_ms,
                 1 if call.truncated else 0, call.idempotency_key, call.created_at, call.updated_at),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # Idempotency key conflict — return existing
            if call.idempotency_key:
                existing = self.get_tool_call_by_idempotency(call.idempotency_key)
                if existing:
                    return existing
            raise
        return call

    def update_tool_call(self, call: ToolCall) -> ToolCall:
        call.updated_at = _now()
        self.conn.execute(
            """
            UPDATE storage_tool_calls
            SET output_json = ?, error = ?, status = ?, duration_ms = ?, truncated = ?, updated_at = ?
            WHERE id = ?
            """,
            (_json(call.output), call.error, call.status, call.duration_ms,
             1 if call.truncated else 0, call.updated_at, call.id),
        )
        self.conn.commit()
        return call

    def list_tool_calls(self, run_id: str) -> list[ToolCall]:
        rows = self.conn.execute(
            "SELECT * FROM storage_tool_calls WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    def get_tool_call_by_idempotency(self, idempotency_key: str) -> ToolCall | None:
        if not idempotency_key:
            return None
        row = self.conn.execute(
            "SELECT * FROM storage_tool_calls WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_tool_call(row)

    # --- Artifacts ---

    def create_artifact(self, artifact: Artifact) -> Artifact:
        if not artifact.id:
            artifact.id = _new_id()
        self.conn.execute(
            """
            INSERT INTO storage_artifacts (id, run_id, artifact_type, name, path, checksum, size_bytes, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact.id, artifact.run_id, artifact.artifact_type, artifact.name,
             artifact.path, artifact.checksum, artifact.size_bytes,
             _json(artifact.metadata), artifact.created_at),
        )
        self.conn.commit()
        return artifact

    def list_artifacts(self, run_id: str) -> list[Artifact]:
        rows = self.conn.execute(
            "SELECT * FROM storage_artifacts WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    # --- Memories ---

    def save_memory(self, memory: Memory) -> Memory:
        if not memory.id:
            memory.id = _new_id()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO storage_memories (id, run_id, kind, source, content, tags_json, importance, activation, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory.id, memory.run_id, memory.kind, memory.source, memory.content,
             _json(memory.tags), memory.importance, memory.activation,
             _json(memory.metadata), memory.created_at, memory.updated_at),
        )
        self.conn.commit()
        return memory

    def list_memories(self, run_id: str = "", *, kind: str = "", limit: int = 50) -> list[Memory]:
        query = "SELECT * FROM storage_memories WHERE 1=1"
        params: list[Any] = []
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY activation DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_memory(row) for row in rows]

    # --- Usage ---

    def record_usage(self, usage: ModelUsageRecord) -> ModelUsageRecord:
        if not usage.id:
            usage.id = _new_id()
        self.conn.execute(
            """
            INSERT INTO storage_usage (id, run_id, step_id, model, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (usage.id, usage.run_id, usage.step_id, usage.model,
             usage.input_tokens, usage.output_tokens,
             usage.cache_creation_input_tokens, usage.cache_read_input_tokens,
             usage.created_at),
        )
        self.conn.commit()
        return usage

    def list_usage(self, run_id: str) -> list[ModelUsageRecord]:
        rows = self.conn.execute(
            "SELECT * FROM storage_usage WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [self._row_to_usage(row) for row in rows]

    # --- Checkpoints ---

    def save_checkpoint(self, run_id: str, step_name: str, data: dict[str, Any]) -> dict[str, Any]:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO storage_checkpoints (run_id, step_name, checkpoint_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, step_name, _json(data), _now()),
        )
        self.conn.commit()
        return data

    def get_latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT checkpoint_json FROM storage_checkpoints WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        return _parse(row["checkpoint_json"])

    # --- Health ---

    def health_check(self) -> bool:
        try:
            self.conn.execute("SELECT 1 FROM storage_runs LIMIT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.conn.close()

    # --- Row mapping ---

    def _row_to_run(self, row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            id=row["id"],
            conversation_id=row["conversation_id"] or "",
            session_id=row["session_id"] or "",
            task=row["task"] or "",
            status=row["status"] or "queued",
            prompt_version=row["prompt_version"] or "",
            workspace_path=row["workspace_path"] or "",
            result=_parse(row["result_json"], {}),
            error=row["error"],
            metadata=_parse(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def _row_to_step(self, row: sqlite3.Row) -> AgentStep:
        return AgentStep(
            id=row["id"],
            run_id=row["run_id"],
            name=row["name"],
            role=row["role"],
            status=row["status"],
            input_data=_parse(row["input_json"], {}),
            output_data=_parse(row["output_json"], {}),
            error=row["error"],
            duration_ms=row["duration_ms"] or 0.0,
            checkpoint_data=_parse(row["checkpoint_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_tool_call(self, row: sqlite3.Row) -> ToolCall:
        return ToolCall(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"] or "",
            tool_name=row["tool_name"],
            input_params=_parse(row["input_json"], {}),
            output=_parse(row["output_json"]),
            error=row["error"],
            status=row["status"],
            duration_ms=row["duration_ms"] or 0.0,
            truncated=bool(row["truncated"]),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_artifact(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"],
            run_id=row["run_id"],
            artifact_type=row["artifact_type"],
            name=row["name"],
            path=row["path"],
            checksum=row["checksum"],
            size_bytes=row["size_bytes"],
            metadata=_parse(row["metadata_json"], {}),
            created_at=row["created_at"],
        )

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            source=row["source"],
            content=row["content"],
            tags=_parse(row["tags_json"], []),
            importance=row["importance"] or 0.5,
            activation=row["activation"] or 0.0,
            metadata=_parse(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_usage(self, row: sqlite3.Row) -> ModelUsageRecord:
        return ModelUsageRecord(
            id=row["id"],
            run_id=row["run_id"],
            step_id=row["step_id"] or "",
            model=row["model"],
            input_tokens=row["input_tokens"] or 0,
            output_tokens=row["output_tokens"] or 0,
            cache_creation_input_tokens=row["cache_creation_input_tokens"] or 0,
            cache_read_input_tokens=row["cache_read_input_tokens"] or 0,
            created_at=row["created_at"],
        )

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"],
            conversation_id=row["conversation_id"],
            role=row["role"],
            content=row["content"],
            tool_call_id=row["tool_call_id"],
            name=row["name"],
            metadata=_parse(row["metadata_json"], {}),
            created_at=row["created_at"],
        )

    def _row_to_conversation(self, row: sqlite3.Row) -> Conversation:
        messages_data = _parse(row["messages_json"], [])
        return Conversation(
            id=row["id"],
            title=row["title"],
            workspace_path=row["workspace_path"],
            messages=[Message.from_dict(m) for m in messages_data] if isinstance(messages_data, list) else [],
            metadata=_parse(row["metadata_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
