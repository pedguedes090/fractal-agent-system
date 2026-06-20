from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .debug_log import write_debug_event

_execution_id: ContextVar[str] = ContextVar("durable_execution_id", default="")
_database_path: ContextVar[str] = ContextVar("durable_database_path", default="")
_runtime_settings: ContextVar[dict[str, Any]] = ContextVar("runtime_settings", default={})

_SECRET_KEYS = {
    "api_key", "apikey", "api-key", "authorization",
    "cookie", "password", "secret", "token",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _is_secret_key(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized in {item.replace("-", "_") for item in _SECRET_KEYS} or normalized.endswith(
        ("_token", "_secret", "_password", "_api_key")
    )


def sanitize_payload(value: Any, *, max_string: int = 100_000, depth: int = 0) -> Any:
    if depth > 8:
        return "[max-depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + f"...[truncated {len(value) - max_string} chars]"
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                result["__truncated__"] = f"{len(value) - 200} keys"
                break
            result[str(key)] = "[redacted]" if _is_secret_key(key) else sanitize_payload(item, max_string=max_string, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [sanitize_payload(item, max_string=max_string, depth=depth + 1) for item in items[:200]]
        if len(items) > 200:
            result.append(f"[truncated {len(items) - 200} items]")
        return result
    if hasattr(value, "model_dump"):
        try:
            return sanitize_payload(value.model_dump(mode="json", exclude_none=True), max_string=max_string, depth=depth + 1)
        except Exception:
            pass
    return sanitize_payload(str(value), max_string=max_string, depth=depth + 1)


def _fingerprint(value: Any) -> str:
    payload = _json(sanitize_payload(value))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# JSON-file durable execution store — one file per execution.
# No SQLite, no WAL contention, no "disk full" from temp dir quotas.
# ---------------------------------------------------------------------------

def _executions_dir() -> Path:
    value = os.getenv("AGENT_ENGINE_STATE_DIR")
    root = Path(value).expanduser() if value else Path.cwd() / ".agent-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _execution_path(execution_id: str) -> Path:
    safe = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:32]
    return _executions_dir() / "executions" / f"{safe}.json"


# ── In-memory durable store (no disk I/O, no SQLite, no race) ─────────
_STORE: dict[str, dict[str, Any]] = {}
# RLock: prepare()/recover_incomplete() re-enter via get()/record_event() while holding the lock.
_STORE_LOCK = threading.RLock()

class DurableExecutionStore:
    """In-memory durable execution store. Lives for process lifetime."""

    def __init__(self, _db_path: Path | None = None) -> None:
        pass  # all state is class-level, shared across instances

    def close(self) -> None:
        pass

    # --- public API (identical contract) ---------------------------------------

    def prepare(self, *, execution_id: str, session_id: str, correlation_id: str,
                task: str, workspace_path: str, input_payload: dict[str, Any]) -> dict[str, Any]:
        request_fingerprint = _fingerprint({"task": task, "workspacePath": os.path.normpath(os.path.abspath(workspace_path))})
        now = _now()
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
            if data:
                if data.get("_fp") != request_fingerprint:
                    raise ValueError(f"Execution ID {execution_id} is already bound to a different task or workspace.")
                data["input_json"] = _json(sanitize_payload(input_payload))
                data["correlation_id"] = correlation_id
                data["updated_at"] = now
                return self.get(execution_id) or {}
            _STORE[execution_id] = {
                "id": execution_id, "session_id": session_id, "correlation_id": correlation_id,
                "_fp": request_fingerprint, "task": task,
                "workspace_path": os.path.normpath(os.path.abspath(workspace_path)),
                "input_json": _json(sanitize_payload(input_payload)),
                "result_json": None, "status": "queued", "attempt": 0,
                "lease_owner": None, "heartbeat_at": None, "last_error": None,
                "created_at": now, "updated_at": now, "completed_at": None, "steps": [],
            }
        return self.get(execution_id) or {}

    def acquire(self, execution_id: str) -> str:
        owner = str(uuid.uuid4()); now = _now()
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
            if not data: raise ValueError(f"Unknown execution: {execution_id}")
            data["status"] = "running"; data["attempt"] = data.get("attempt", 0) + 1
            data["lease_owner"] = owner; data["heartbeat_at"] = now
            data["last_error"] = None; data["updated_at"] = now
        self.record_event(execution_id, "supervisor", "lease_acquired", {"leaseOwner": owner})
        return owner

    def heartbeat(self, execution_id: str, owner: str) -> bool:
        now = _now()
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
            if not data or data.get("lease_owner") != owner or data.get("status") != "running":
                return False
            data["heartbeat_at"] = now; data["updated_at"] = now
            return True

    def mark_recoverable(self, execution_id: str, error: BaseException | str) -> None:
        now = _now(); message = str(error)[:4000]
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
            if data:
                data["status"] = "recoverable"; data["lease_owner"] = None
                data["last_error"] = message; data["updated_at"] = now
        self.record_event(execution_id, "supervisor", "execution_recoverable", {"error": message})

    def complete(self, execution_id: str, result: dict[str, Any], status: str = "completed") -> None:
        now = _now()
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
            if data:
                data["status"] = status; data["result_json"] = _json(sanitize_payload(result))
                data["lease_owner"] = None; data["last_error"] = None
                data["heartbeat_at"] = now; data["updated_at"] = now; data["completed_at"] = now
        self.record_event(execution_id, "supervisor", "execution_completed", {"status": status})

    def get(self, execution_id: str) -> dict[str, Any] | None:
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
        if not data: return None
        return {"id": data["id"], "sessionId": data["session_id"], "correlationId": data["correlation_id"],
                "task": data["task"], "workspacePath": data["workspace_path"],
                "input": _parse(data.get("input_json"), {}), "result": _parse(data.get("result_json"), None),
                "status": data["status"], "attempt": data.get("attempt", 0),
                "leaseOwner": data.get("lease_owner"), "heartbeatAt": data.get("heartbeat_at"),
                "lastError": data.get("last_error"), "createdAt": data.get("created_at"),
                "updatedAt": data.get("updated_at"), "completedAt": data.get("completed_at")}

    def recover_incomplete(self) -> int:
        now = _now(); count = 0
        with _STORE_LOCK:
            for eid, data in list(_STORE.items()):
                if data.get("status") == "running":
                    data["status"] = "recoverable"; data["lease_owner"] = None
                    data["last_error"] = "Backend stopped before the execution completed."
                    data["updated_at"] = now; count += 1
        for eid in list(_STORE.keys()):
            d = _STORE.get(eid)
            if d and d.get("status") == "recoverable" and d.get("updated_at") == now:
                self.record_event(eid, "supervisor", "startup_recovered", {"status": "recoverable"})
        return count

    def start_step(self, execution_id: str, kind: str, name: str, input_payload: Any,
                   *, idempotency_key: str | None = None) -> str:
        step_id = str(uuid.uuid4()); now = _now()
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
            steps = (data.get("steps") or []) if data else []
            steps.append({"id": step_id, "execution_id": execution_id, "sequence": len(steps) + 1,
                         "kind": kind, "name": name, "idempotency_key": idempotency_key,
                         "status": "running", "input_json": _json(sanitize_payload(input_payload)),
                         "output_json": None, "error": None, "started_at": now, "completed_at": None})
            if data:
                data["steps"] = steps; data["heartbeat_at"] = now; data["updated_at"] = now
        return step_id

    def finish_step(self, step_id: str, output: Any = None, *, error: BaseException | str | None = None) -> None:
        now = _now(); status = "failed" if error is not None else "completed"
        message = str(error)[:4000] if error is not None else None
        with _STORE_LOCK:
            for data in _STORE.values():
                for s in data.get("steps") or []:
                    if s.get("id") == step_id:
                        s["status"] = status
                        s["output_json"] = _json(sanitize_payload(output)) if output is not None else None
                        s["error"] = message; s["completed_at"] = now
                        data["updated_at"] = now; return

    def cached_step(self, execution_id: str, idempotency_key: str) -> Any:
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
        if data:
            for s in reversed(data.get("steps") or []):
                if s.get("idempotency_key") == idempotency_key and s.get("status") == "completed":
                    return _parse(s.get("output_json"), None)
        return None

    def record_event(self, execution_id: str, kind: str, name: str, payload: Any) -> None:
        step_id = self.start_step(execution_id, kind, name, payload)
        self.finish_step(step_id, payload)

    def steps(self, execution_id: str) -> list[dict[str, Any]]:
        with _STORE_LOCK:
            data = _STORE.get(execution_id)
        result: list[dict[str, Any]] = []
        for s in (data.get("steps") or []) if data else []:
            result.append({"id": s["id"], "executionId": s["execution_id"], "sequence": s["sequence"],
                          "kind": s["kind"], "name": s["name"],
                          "idempotencyKey": s.get("idempotency_key"), "status": s["status"],
                          "input": _parse(s.get("input_json"), {}),
                          "output": _parse(s.get("output_json"), None),
                          "error": s.get("error"), "startedAt": s.get("started_at"),
                          "completedAt": s.get("completed_at")})
        return result


# ---------------------------------------------------------------------------
# Heartbeat / context / checkpoint (unchanged contract, just uses new store)
# ---------------------------------------------------------------------------

class ExecutionHeartbeat:
    def __init__(self, db_path: Path, execution_id: str, owner: str, interval_seconds: float | None = None) -> None:
        self.execution_id = execution_id
        self.owner = owner
        self.interval_seconds = interval_seconds or max(1.0, float(os.getenv("AGENT_HEARTBEAT_SECONDS", "5")))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"execution-heartbeat-{execution_id[:8]}", daemon=True)

    def __enter__(self) -> "ExecutionHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)
        return False

    def _run(self) -> None:
        store = DurableExecutionStore()
        try:
            while not self._stop.wait(self.interval_seconds):
                if not store.heartbeat(self.execution_id, self.owner):
                    return
        finally:
            store.close()


@contextmanager
def execution_context(
    *,
    execution_id: str,
    database_path: Path,
    runtime_settings: dict[str, Any] | None = None,
) -> Iterator[None]:
    exec_token = _execution_id.set(execution_id)
    db_token = _database_path.set(str(database_path))
    settings_token = _runtime_settings.set(dict(runtime_settings or {}))
    try:
        yield
    finally:
        _runtime_settings.reset(settings_token)
        _database_path.reset(db_token)
        _execution_id.reset(exec_token)


def current_execution_id() -> str:
    return _execution_id.get()


def runtime_settings() -> dict[str, Any]:
    return dict(_runtime_settings.get())


def _active_store() -> tuple[DurableExecutionStore | None, str]:
    execution_id = current_execution_id()
    if not execution_id:
        return None, ""
    return DurableExecutionStore(), execution_id


class StepCheckpoint:
    def __init__(self, step_id: str | None = None) -> None:
        self.step_id = step_id
        self.output: Any = None

    def set_output(self, output: Any) -> Any:
        self.output = output
        return output


@contextmanager
def checkpoint_step(kind: str, name: str, input_payload: Any = None) -> Iterator[StepCheckpoint]:
    store, execution_id = _active_store()
    checkpoint = StepCheckpoint()
    if not store:
        yield checkpoint
        return
    try:
        checkpoint.step_id = store.start_step(execution_id, kind, name, input_payload)
        yield checkpoint
    except Exception as exc:
        store.finish_step(checkpoint.step_id, checkpoint.output, error=exc)
        raise
    else:
        store.finish_step(checkpoint.step_id, checkpoint.output)
    finally:
        store.close()


def record_checkpoint(kind: str, name: str, payload: Any = None) -> None:
    store, execution_id = _active_store()
    if not store:
        return
    try:
        store.record_event(execution_id, kind, name, payload)
    finally:
        store.close()


def cached_tool_call(kind: str, name: str, input_payload: Any, callback: Callable[[], Any]) -> tuple[Any, bool]:
    store, execution_id = _active_store()
    if not store:
        return callback(), False
    idempotency_key = f"{kind}:{name}:{_fingerprint(input_payload)}"
    try:
        cached = store.cached_step(execution_id, idempotency_key)
        if cached is not None:
            store.record_event(execution_id, kind, f"{name}.cache_hit", {"idempotencyKey": idempotency_key})
            return cached, True
        step_id = store.start_step(execution_id, kind, name, input_payload, idempotency_key=idempotency_key)
        try:
            output = callback()
        except Exception as exc:
            store.finish_step(step_id, error=exc)
            raise
        store.finish_step(step_id, output)
        return output, False
    finally:
        store.close()


def durable_state_dir() -> Path:
    value = os.getenv("AGENT_ENGINE_STATE_DIR")
    root = Path(value).expanduser() if value else Path.cwd() / ".agent-state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def execution_artifact_dir(execution_id: str | None = None) -> Path:
    value = str(execution_id or current_execution_id() or "unscoped")
    safe_id = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    path = durable_state_dir() / "executions" / safe_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_transient_error(error: BaseException | str) -> bool:
    value = str(error).lower()
    # Never retry MCP-related failures — they're persistent (server absent,
    # handshake failure, binary not responding) and retrying just wastes time.
    mcp_signals = ("mcp tool listing timed out", "parse error", "mcp connection failure")
    if any(pattern.lower() in value for pattern in mcp_signals):
        return False
    transient_signals = (
        "429", "500", "502", "503", "504",
        "connection", "temporarily unavailable", "timed out", "timeout",
        "rate limit", "remote end closed", "network", "broken pipe",
    )
    return any(signal in value for signal in transient_signals)


def log_resume(execution_id: str, detail: str) -> None:
    write_debug_event("durable.resume", {"executionId": execution_id, "detail": detail})
