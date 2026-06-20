from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import telemetry

_LOCK = threading.Lock()

# Cache the resolved log directory per (state_dir, date) so we don't pay
# two mkdir/stat syscalls on every event. On Windows with antivirus the
# repeated stat() calls dominated test runtime — see CLAUDE.md.
_CACHED_DIR_KEY: tuple[str, str] | None = None
_CACHED_DIR: Path | None = None


def _state_dir() -> Path:
    value = os.getenv("AGENT_ENGINE_STATE_DIR")
    root = Path(value).expanduser() if value else Path.cwd() / ".agent-state"
    return root


def debug_log_path(now: datetime | None = None) -> Path:
    global _CACHED_DIR_KEY, _CACHED_DIR
    current = now or datetime.now(timezone.utc)
    date_key = current.strftime("%Y%m%d")
    state_key = os.getenv("AGENT_ENGINE_STATE_DIR") or ""
    key = (state_key, date_key)
    log_dir = _CACHED_DIR
    if key != _CACHED_DIR_KEY or log_dir is None:
        log_dir = _state_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _CACHED_DIR_KEY = key
        _CACHED_DIR = log_dir
    return log_dir / f"agent-debug-{date_key}.jsonl"


def _compact(value: Any, max_chars: int = 12000) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)[:max_chars]
    if len(text) <= max_chars:
        return value
    return {"truncated": True, "preview": text[:max_chars]}


def write_debug_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    try:
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "eventType": event_type,
            "correlationId": telemetry.get_correlation_id(),
            "payload": _compact(payload or {}),
        }
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        with _LOCK:
            with debug_log_path().open("a", encoding="utf-8") as file:
                file.write(line)
    except Exception:
        return
