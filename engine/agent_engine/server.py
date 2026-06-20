from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import telemetry
from .autonomy import autonomy_status, run_idle_discovery, select_next_task
from .broker import SQLiteAgentBroker
from .debug_log import write_debug_event
from .deterministic_workflow import DEFAULT_WORKFLOW
from .durable_execution import DurableExecutionStore
from .graph import classify_execution, request_cancel, run_pipeline
from .project_doctor import run_doctor
from .state_store import control_plane_path, migrate_legacy_tables


# Lane assignment for the pipeline view. Source of truth for which lane each
# node belongs to; the renderer mirrors this to lay out the DAG.
_NODE_LANES: dict[str, str] = {
    "preflight": "intake",
    "codegraph_context": "intake",
    "repo_intelligence": "intake",
    "intake_user_intent": "intake",
    "intake_ambiguity": "intake",
    "intake_repo_context": "intake",
    "intake_synthesizer": "intake",
    "planning_minimal": "planning",
    "planning_robust": "planning",
    "planning_test_first": "planning",
    "critique_risk": "planning",
    "critique_test_coverage": "planning",
    "critique_security_regression": "planning",
    "plan_arbiter": "planning",
    "planner_task_graph": "governance",
    "researcher_context_agent": "governance",
    "governance_service": "governance",
    "human_gate": "governance",
    "environment_gate": "governance",
    "read_only_reporter": "governance",
    "load_context_files": "execution",
    "openhands_worker": "execution",
    "tester_agent": "execution",
    "security_reviewer_agent": "review",
    "code_reviewer_agent": "review",
    "doctor_feedback": "review",
    "release_deploy_agent": "review",
    "reviewer_decision": "review",
    "execution_gate": "review",
    "reporter": "release",
    "finalize_workspace": "release",
    "reporter_end": "release",
}


def _topology_payload() -> dict[str, Any]:
    wf = DEFAULT_WORKFLOW
    raw = wf.raw
    nodes = [
        {"id": name, "lane": _NODE_LANES.get(name, "other"), "label": name.replace("_", " ").title()}
        for name in wf.nodes
    ]
    edges: list[dict[str, Any]] = []
    for edge in raw.get("edges") or []:
        src, dst = str(edge[0]), str(edge[1])
        edges.append({"from": src, "to": dst, "kind": "direct"})
    for src, targets in (raw.get("fanOut") or {}).items():
        for dst in targets or []:
            edges.append({"from": str(src), "to": str(dst), "kind": "fanout"})
    for join in raw.get("joins") or []:
        for src in join.get("sources") or []:
            edges.append({"from": str(src), "to": str(join.get("target")), "kind": "join"})
    for node, cfg in (raw.get("routes") or {}).items():
        if not isinstance(cfg, dict):
            continue
        edges.append({"from": str(node), "to": str(cfg.get("default")), "kind": "route", "label": "default"})
        for case in cfg.get("cases") or []:
            edges.append({
                "from": str(node),
                "to": str(case.get("target")),
                "kind": "route",
                "label": str(case.get("when") or ""),
            })
    # De-duplicate (src,dst,kind,label)
    seen: set[tuple[str, str, str, str]] = set()
    unique_edges: list[dict[str, Any]] = []
    for edge in edges:
        key = (edge["from"], edge["to"], edge.get("kind", ""), str(edge.get("label", "")))
        if key in seen:
            continue
        seen.add(key)
        unique_edges.append(edge)
    return {
        "workflow": {"name": wf.name, "version": wf.version},
        "lanes": [
            {"id": "intake", "title": "Intake"},
            {"id": "planning", "title": "Planning"},
            {"id": "governance", "title": "Governance"},
            {"id": "execution", "title": "Execution"},
            {"id": "review", "title": "Review"},
            {"id": "release", "title": "Release"},
        ],
        "nodes": nodes,
        "edges": unique_edges,
        "contextRoutes": {k: list(v) for k, v in wf.context_routes.items()},
    }

_WRITE_LOCK = threading.Lock()
_AUTONOMY_LOCK = threading.Lock()


def _state_dir_path() -> Path:
    return Path(os.getenv("AGENT_ENGINE_STATE_DIR") or str(Path.cwd() / ".agent-state"))


def _recent_debug_events(limit: int = 80) -> list[dict[str, Any]]:
    log_dir = _state_dir_path() / "logs"
    files = sorted(log_dir.glob("agent-debug-*.jsonl"))
    if not files:
        return []
    lines: deque[str] = deque(maxlen=max(1, int(limit)))
    try:
        with files[-1].open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def _json_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _progress(stage: str, detail: str) -> dict[str, Any]:
    return {
        "type": "progress",
        "stage": stage,
        "detail": detail,
        "at": datetime.now(timezone.utc).isoformat(),
    }


class AgentRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "HeThongAgentBackend/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _serve_static(self, filename: str, content_type: str) -> None:
        renderer_dir = Path(__file__).resolve().parents[2] / "src" / "renderer"
        filepath = (renderer_dir / filename).resolve()
        if renderer_dir not in filepath.parents and filepath != renderer_dir:
            self._send_json(403, {"error": "forbidden"})
            return
        if not filepath.exists() or not filepath.is_file():
            self._send_json(404, {"error": "not_found"})
            return
        try:
            body = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self._send_json(500, {"error": "serve_error"})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        # Static file serving for web browser access
        if self.path == "/" or self.path == "/index.html":
            self._serve_static("index.html", "text/html; charset=utf-8")
            return
        if self.path.endswith(".css"):
            self._serve_static(self.path.lstrip("/"), "text/css; charset=utf-8")
            return
        if self.path.endswith(".js"):
            self._serve_static(self.path.lstrip("/"), "application/javascript; charset=utf-8")
            return
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/v1/topology":
            try:
                self._send_json(200, _topology_payload())
            except Exception as exc:
                self._send_json(500, {"error": "topology_error", "detail": str(exc)})
            return
        if self.path == "/v1/observability":
            state_dir = _state_dir_path()
            self._send_json(
                200,
                {
                    "ok": True,
                    "stateDir": str(state_dir),
                    "debugLogDir": str(state_dir / "logs"),
                    "runLockActive": _WRITE_LOCK.locked(),
                    "writeLockActive": _WRITE_LOCK.locked(),
                    "recentEvents": _recent_debug_events(),
                },
            )
            return
        if self.path == "/v1/autonomy/status":
            payload = autonomy_status(_state_dir_path())
            payload["runLockActive"] = _WRITE_LOCK.locked()
            payload["writeLockActive"] = _WRITE_LOCK.locked()
            payload["autonomyScanActive"] = _AUTONOMY_LOCK.locked()
            self._send_json(200, payload)
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/v1/autonomy/idle-scan":
            self._handle_autonomy_idle_scan()
            return
        if self.path == "/v1/autonomy/next-task":
            self._handle_autonomy_next_task()
            return
        if self.path == "/v1/runs/cancel":
            try:
                payload = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"error": f"invalid_json: {exc}"})
                return
            execution_id = str(payload.get("executionId") or "").strip()
            if not execution_id:
                self._send_json(400, {"error": "executionId required"})
                return
            cancelled_now = request_cancel(execution_id)
            write_debug_event("http.run_cancel", {
                "executionId": execution_id, "alreadyCancelled": not cancelled_now,
            })
            self._send_json(200, {"ok": True, "executionId": execution_id, "cancelled": cancelled_now})
            return
        if self.path == "/v1/doctor":
            self._handle_doctor()
            return
        if self.path != "/v1/runs":
            self._send_json(404, {"error": "not_found"})
            return

        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        payload["correlationId"] = correlation_id
        admission = classify_execution(str(payload.get("content") or payload.get("task") or ""))
        execution_class = str(admission["executionClass"])
        payload["executionClass"] = execution_class
        write_debug_event(
            "http.run_received",
            {
                "path": self.path,
                "sessionId": payload.get("sessionId"),
                "workspacePath": payload.get("workspacePath"),
                "taskPreview": str(payload.get("content") or payload.get("task") or "")[:500],
            },
        )

        # Compute the executionId now so we can echo it to the client up-front;
        # the renderer needs this to POST /v1/runs/cancel if user clicks Stop.
        import uuid as _uuid
        execution_id_for_stream = str(payload.get("executionId") or correlation_id or _uuid.uuid4())
        payload["executionId"] = execution_id_for_stream

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Correlation-Id", correlation_id)
        self.send_header("X-Execution-Id", execution_id_for_stream)
        self.end_headers()

        def write_line(message: dict[str, Any]) -> None:
            self.wfile.write(_json_line(message))
            self.wfile.flush()

        # First line so the client learns the executionId before any work starts.
        write_line({"type": "ready", "executionId": execution_id_for_stream, "correlationId": correlation_id,
                    "sessionId": payload.get("sessionId")})

        import uuid as _emit_uuid
        last_event_id_by_node: dict[str, str] = {}

        def emit(stage: str, detail: str, **fields: Any) -> None:
            message = _progress(stage, detail)
            message["executionId"] = execution_id_for_stream
            message["sessionId"] = payload.get("sessionId")
            message["correlationId"] = correlation_id
            event_id = _emit_uuid.uuid4().hex
            message["eventId"] = event_id
            # Stamp node field so the UI's log-filter-by-node, flowView status,
            # and execution-tab grouping work consistently.  When the stage
            # string IS a known node id (e.g. "openhands_worker"), that is
            # the canonical node.  Sub-stages get the explicit `node=` kwarg
            # from build_graph's traced_node wrapper.
            node_name = fields.pop("node", None) or _NODE_LANES.get(stage)
            if node_name:
                message["node"] = node_name
                message["parentEventId"] = last_event_id_by_node.get(node_name)
                last_event_id_by_node[node_name] = event_id
            # Default eventType for free-form sub-stage emits (lifecycle uses
            # explicit `event_type=` from traced_node).
            event_type = fields.pop("event_type", None)
            if event_type:
                message["eventType"] = event_type
            # Allowlisted enrichment kwargs — copy non-None values through.
            ALLOWED = (
                "agent_role", "from_agent", "to_agent",
                "duration_ms", "model", "tool", "status",
                "input_summary", "output_summary",
                "retry_count", "review_cycle",
                "token_usage", "warnings", "error", "route_label",
            )
            for key in ALLOWED:
                if key in fields and fields[key] is not None:
                    # camelCase for the wire — matches existing UI convention.
                    camel = "".join(
                        part if i == 0 else part.title()
                        for i, part in enumerate(key.split("_"))
                    )
                    message[camel] = fields[key]
            write_debug_event("progress", {"stage": stage, "detail": detail, "node": node_name})
            write_line(message)

        server_kind = telemetry.SpanKind.SERVER if telemetry.SpanKind else None
        with telemetry.start_span(
            "http.server.agent_run",
            {
                "http.method": "POST",
                "http.route": "/v1/runs",
                "session.id": payload.get("sessionId", ""),
                "correlation.id": correlation_id,
            },
            kind=server_kind,
            context=telemetry.extract_trace_context(headers),
        ) as span:
            lock_acquired = False
            try:
                if execution_class == "write":
                    if not _WRITE_LOCK.acquire(blocking=False):
                        write_debug_event("run.queued", {"correlationId": correlation_id, "executionClass": execution_class})
                        emit("queued", "Another write-capable run is active; waiting for the write lock")
                        _WRITE_LOCK.acquire()
                    lock_acquired = True
                    write_debug_event("run.running", {"correlationId": correlation_id, "executionClass": execution_class})
                    emit("running", "Write lane admitted")
                else:
                    write_debug_event("run.running", {"correlationId": correlation_id, "executionClass": execution_class})
                    emit("running", "Read-only lane admitted without the write lock")
                result = run_pipeline(payload, emit)
                if span:
                    span.set_attribute("run.id", result.get("id", ""))
                write_debug_event(
                    "run.result",
                    {
                        "runId": result.get("id"),
                        "changedFileCount": len(result.get("changedFiles") or []),
                        "reviewStatus": (result.get("review") or {}).get("status") if isinstance(result.get("review"), dict) else None,
                        "correlationId": correlation_id,
                    },
                )
                write_line({"type": "result", "result": result})
            except BrokenPipeError:
                return
            except Exception as exc:
                write_debug_event("run.error", {"error": str(exc), "correlationId": correlation_id})
                write_line({"type": "error", "error": str(exc)})
            finally:
                if lock_acquired:
                    _WRITE_LOCK.release()
                    write_debug_event("run.released", {"correlationId": correlation_id})

    def _handle_autonomy_idle_scan(self) -> None:
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return

        raw_workspace_path = str(payload.get("workspacePath") or "").strip()
        if not raw_workspace_path:
            self._send_json(400, {"error": "workspacePath is required"})
            return
        workspace_path = Path(raw_workspace_path).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            self._send_json(400, {"error": "workspacePath must be an existing directory"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        if not _AUTONOMY_LOCK.acquire(blocking=False):
            write_debug_event("autonomy.idle_scan_skipped", {"reason": "autonomy_scan_active", "correlationId": correlation_id})
            self._send_json(
                409,
                {
                    "ok": False,
                    "error": "autonomy_scan_active",
                    "runLockActive": _WRITE_LOCK.locked(),
                    "writeLockActive": _WRITE_LOCK.locked(),
                    "autonomyScanActive": True,
                },
            )
            return

        try:
            write_debug_event(
                "autonomy.idle_scan_requested",
                {"workspacePath": str(workspace_path.resolve()), "correlationId": correlation_id},
            )
            report = run_idle_discovery(workspace_path, _state_dir_path())
            self._send_json(
                200,
                {
                    "ok": True,
                    "correlationId": correlation_id,
                    "runLockActive": _WRITE_LOCK.locked(),
                    "writeLockActive": _WRITE_LOCK.locked(),
                    "autonomyScanActive": False,
                    "report": report,
                    "memory": report.get("memory"),
                },
            )
        except Exception as exc:
            write_debug_event("autonomy.idle_scan_error", {"error": str(exc), "correlationId": correlation_id})
            self._send_json(500, {"ok": False, "error": str(exc)})
        finally:
            _AUTONOMY_LOCK.release()

    def _handle_autonomy_next_task(self) -> None:
        """Pick the next autonomous task.

        Body: {
          workspacePath: str,
          completedIds?: list[str],   # finding/idea ids already attempted this loop
          ideaCursor?: int,           # rotation index into enhancement pool
          rescanIfStale?: bool        # run idle-scan first if no report cached
        }
        Returns: { ok, task: {id,kind,category,title,task,source,priorityScore}|null,
                   nextIdeaCursor, source: "cache"|"fresh_scan"|"none" }
        """
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return
        raw_workspace = str(payload.get("workspacePath") or "").strip()
        if not raw_workspace:
            self._send_json(400, {"error": "workspacePath is required"})
            return
        workspace_path = Path(raw_workspace).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            self._send_json(400, {"error": "workspacePath must be an existing directory"})
            return

        completed_ids = set(map(str, payload.get("completedIds") or []))
        idea_cursor = int(payload.get("ideaCursor") or 0)
        rescan_if_stale = bool(payload.get("rescanIfStale"))

        state_dir = _state_dir_path()
        report = (autonomy_status(state_dir) or {}).get("lastReport")
        source = "cache"
        if (not report or rescan_if_stale) and not _AUTONOMY_LOCK.locked():
            if _AUTONOMY_LOCK.acquire(blocking=False):
                try:
                    write_debug_event(
                        "autonomy.next_task_rescan",
                        {"workspacePath": str(workspace_path.resolve())},
                    )
                    report = run_idle_discovery(workspace_path, state_dir)
                    source = "fresh_scan"
                finally:
                    _AUTONOMY_LOCK.release()

        task = select_next_task(report, completed_ids, idea_cursor=idea_cursor)
        next_cursor = idea_cursor
        if task and task.get("kind") == "enhancement_idea":
            next_cursor = (idea_cursor + 1) % 8  # pool size; small constant ok here.
        write_debug_event(
            "autonomy.next_task",
            {
                "selected": (task or {}).get("id"),
                "kind": (task or {}).get("kind"),
                "completedCount": len(completed_ids),
            },
        )
        self._send_json(
            200,
            {
                "ok": True,
                "task": task,
                "source": source if task else ("none" if not task else source),
                "nextIdeaCursor": next_cursor,
                "runLockActive": _WRITE_LOCK.locked(),
                "autonomyScanActive": _AUTONOMY_LOCK.locked(),
            },
        )

    def _handle_doctor(self) -> None:
        """Stream scan→plan→patch→verify events as NDJSON.

        Body: { workspacePath: str, sessionId?: str, model?: str }
        Each line is { type: "progress", stage: str, detail: str, ... }
        Final line: { type: "doctor.result", ok: bool, result: {...} }
        """
        try:
            payload = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"invalid_json: {exc}"})
            return
        raw_workspace = str(payload.get("workspacePath") or "").strip()
        if not raw_workspace:
            self._send_json(400, {"error": "workspacePath is required"})
            return
        workspace_path = Path(raw_workspace).expanduser()
        if not workspace_path.exists() or not workspace_path.is_dir():
            self._send_json(400, {"error": "workspacePath must be an existing directory"})
            return

        headers = {key.lower(): value for key, value in self.headers.items()}
        correlation_id = telemetry.set_correlation_id(headers.get("x-correlation-id") or payload.get("correlationId"))
        session_id = payload.get("sessionId")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("X-Correlation-Id", correlation_id)
        self.end_headers()

        def write_line(message: dict[str, Any]) -> None:
            try:
                self.wfile.write(_json_line(message))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client disconnected mid-stream — let the orchestrator continue,
                # we just stop trying to write.
                pass

        write_line({"type": "ready", "correlationId": correlation_id, "sessionId": session_id, "route": "/v1/doctor"})

        def emit(stage: str, detail: str) -> None:
            message = _progress(stage, detail)
            message["sessionId"] = session_id
            write_debug_event("doctor", {"stage": stage, "detail": detail[:400]})
            write_line(message)

        # Optional LLM provider so the patch step can stream tokens.
        # Prefer claude-agent-sdk (Read/Edit/Bash tools). Fall back to the
        # lower-level anthropic SDK provider, which streams text only.
        provider: Any = None
        api_key = (payload.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        model = str(payload.get("model") or os.environ.get("ANTHROPIC_MODEL") or "claude-opus-4-8")
        try:
            from .project_doctor.agent_sdk_provider import maybe_build_provider
            provider = maybe_build_provider(cwd=workspace_path, model=model, api_key=api_key)
            if provider is not None:
                emit("doctor.provider", "claude-agent-sdk ready")
        except Exception as exc:
            emit("doctor.provider.unavailable", f"Agent SDK init failed: {exc}")
        if provider is None and api_key:
            try:
                from .claude_adapter import ClaudeConfig, ClaudeProvider
                provider = ClaudeProvider(ClaudeConfig(api_key=api_key, model=model))
                emit("doctor.provider", "anthropic-sdk fallback ready")
            except Exception as exc:
                emit("doctor.provider.unavailable", f"Anthropic SDK fallback failed: {exc}")

        try:
            result = run_doctor(workspace_path, provider=provider, emit=emit)
            write_line({"type": "doctor.result", "ok": result["ok"], "result": result, "sessionId": session_id})
        except Exception as exc:
            write_debug_event("doctor.error", {"error": str(exc), "correlationId": correlation_id})
            emit("doctor.error", f"Doctor pipeline crashed: {exc}")
            write_line({"type": "doctor.result", "ok": False, "error": str(exc), "sessionId": session_id})


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    telemetry.configure_telemetry()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    # Kick off codebase-memory-mcp download into .tools/ on first boot.
    # Non-blocking — server starts immediately; binary is used as soon as it lands.
    try:
        from . import codebase_memory as _cm
        _cm.ensure_local_binary_async()
    except Exception:
        pass

    server = ThreadingHTTPServer((args.host, args.port), AgentRequestHandler)
    try:
        state_dir = _state_dir_path()
        db_path = control_plane_path(state_dir)
        supervisor = DurableExecutionStore(db_path)
        migrate_legacy_tables(
            db_path,
            state_dir / "durable-executions.sqlite",
            ("durable_executions", "durable_steps"),
        )
        durable_recovered = supervisor.recover_incomplete()
        supervisor.close()
        broker = SQLiteAgentBroker(db_path)
        migrate_legacy_tables(
            db_path,
            state_dir / "agent-broker.sqlite",
            ("agent_runs", "agent_subtasks", "agent_events"),
        )
        recovered = broker.recover_incomplete_runs()
        broker.close()
        telemetry.record_crash_recoveries(recovered + durable_recovered)
    except Exception:
        pass
    print(json.dumps({"type": "ready", "host": args.host, "port": server.server_port}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
