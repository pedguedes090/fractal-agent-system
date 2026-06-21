"""Hierarchical Agent Swarm — 1 Root + ≤20 Lead + ≤200 Specialist (max depth 2).

Root Orchestrator analyzes the repository, spawns up to 20 Lead Agents per
domain, each Lead spawns up to 10 Specialist Agents. All communication flows
through the Durable A2A Broker. One agent crash cannot kill the pipeline.

ARCHITECTURE
  Root Orchestrator
    ├── Lead 1 (e.g. Frontend)
    │   ├── Specialist A
    │   └── Specialist B
    ├── Lead 2 (e.g. Backend)
    │   └── ...
    └── ... (≤20 Leads, each ≤10 Specialists, total ≤221)

CONSTRAINTS
  - max_depth = 2 (Root→Lead→Specialist; Specialist MUST NOT spawn further)
  - 221 is the capacity ceiling, NOT a mandatory quota
  - Spawn only when there is real independent work with clear acceptance criteria
  - Never let two agents edit the same file concurrently (file-ownership lease)
  - Global concurrency semaphore (default 20) to avoid RAM/token/file explosion
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import telemetry
from .debug_log import write_debug_event

# ---------------------------------------------------------------------------
# Utility helpers (inline, no external deps beyond stdlib)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_FileOwnership: dict[str, str] = {}  # relpath → agentId
_OwnershipLock = threading.Lock()


def claim_file(agent_id: str, path: str) -> bool:
    rel = str(path).replace("\\", "/")
    with _OwnershipLock:
        if rel in _FileOwnership and _FileOwnership[rel] != agent_id:
            return False
        _FileOwnership[rel] = agent_id
        return True


def release_file(agent_id: str, path: str) -> None:
    rel = str(path).replace("\\", "/")
    with _OwnershipLock:
        if _FileOwnership.get(rel) == agent_id:
            del _FileOwnership[rel]


def release_all_files(agent_id: str) -> None:
    with _OwnershipLock:
        for k, v in list(_FileOwnership.items()):
            if v == agent_id:
                del _FileOwnership[k]


# ---------------------------------------------------------------------------
# Domain types (independent data-only dataclasses — no agent_engine deps)
# ---------------------------------------------------------------------------


@dataclass
class AgentIdentity:
    agent_id: str
    parent_id: str | None  # None = Root
    depth: int  # 0=Root, 1=Lead, 2=Specialist
    role: str  # e.g. "frontend", "backend", "testing"
    name: str
    model: str = ""
    status: str = "queued"  # queued|running|waiting|testing|fixing|blocked|failed|completed

    def to_dict(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "parentId": self.parent_id,
            "depth": self.depth,
            "role": self.role,
            "name": self.name,
            "model": self.model,
            "status": self.status,
        }


@dataclass
class TaskEnvelope:
    task_id: str
    parent_task_id: str | None
    owner_agent_id: str
    objective: str
    allowed_files: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    priority: str = "medium"  # critical|high|medium|low
    retry_limit: int = 3
    max_turns: int = 40
    state: str = "submitted"  # submitted→dispatched→working→verifying→completed|failed→recovering→redispatched
    attempt: int = 0
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    idempotency_key: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raw = f"{self.task_id}:{self.parent_task_id or ''}:{self.objective[:80]}"
            self.idempotency_key = hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class A2AMessage:
    message_id: str
    correlation_id: str
    task_id: str
    sender_agent_id: str
    receiver_agent_id: str
    event_type: str  # TASK_CREATED|TASK_ASSIGNED|...|TASK_COMPLETED
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = ""
    retry_count: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message_id:
            self.message_id = f"msg-{uuid.uuid4().hex[:12]}"
        if not self.timestamp:
            self.timestamp = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "correlationId": self.correlation_id,
            "taskId": self.task_id,
            "senderAgentId": self.sender_agent_id,
            "receiverAgentId": self.receiver_agent_id,
            "eventType": self.event_type,
            "status": self.status,
            "payload": self.payload,
            "artifacts": self.artifacts,
            "diagnostics": self.diagnostics,
            "timestamp": self.timestamp,
            "retryCount": self.retry_count,
            "tokenUsage": self.token_usage,
        }


# ---------------------------------------------------------------------------
# Swarm capacity constants
# ---------------------------------------------------------------------------

MAX_LEADS = 20
MAX_SPECIALISTS_PER_LEAD = 10
MAX_TOTAL_AGENTS = 1 + MAX_LEADS + MAX_LEADS * MAX_SPECIALISTS_PER_LEAD  # 221
MAX_DEPTH = 2
DEFAULT_CONCURRENCY = int(os.environ.get("AGENT_SWARM_CONCURRENCY", "20"))

# Lead domain templates — adjusted dynamically based on actual project structure.
LEAD_DOMAINS = [
    "product_requirement",
    "architecture",
    "frontend",
    "backend",
    "database",
    "api",
    "authentication",
    "security",
    "ui_ux",
    "accessibility",
    "performance",
    "testing",
    "build_tooling",
    "devops",
    "observability",
    "code_quality",
    "integration",
    "regression",
    "release",
    "final_verification",
]


# ---------------------------------------------------------------------------
# Swarm Orchestrator
# ---------------------------------------------------------------------------


class SwarmOrchestrator:
    """Root orchestrator for the hierarchical agent swarm.

    Spawned once per pipeline run. Holds the agent tree, task queue, A2A
    message log, and global concurrency semaphore.
    """

    def __init__(
        self,
        execution_id: str,
        workspace_path: str,
        original_user_goal: str = "",
        max_concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.execution_id = execution_id
        self.workspace_path = workspace_path
        self.original_user_goal = original_user_goal
        self.max_concurrency = max_concurrency

        # Agent tree
        self.agents: dict[str, AgentIdentity] = {}
        self.root_id: str | None = None

        # Task ledger
        self.tasks: dict[str, TaskEnvelope] = {}
        self._task_lock = threading.Lock()

        # A2A message log (persistent — survives agent crashes)
        self.message_log: list[A2AMessage] = []
        self._msg_lock = threading.Lock()

        # Global concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

        # File ownership lease (global across all agents)
        self._file_leases: dict[str, str] = {}  # relpath → agentId
        self._lease_lock = threading.Lock()

        # Heartbeat registry
        self._heartbeats: dict[str, float] = {}  # agentId → last_beat_time
        self._heartbeat_ttl: float = float(os.environ.get("AGENT_HEARTBEAT_TTL", "15"))

        # Statistics
        self.stats: dict[str, int] = {
            "total_agents": 0,
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "agent_crashes": 0,
            "messages_sent": 0,
        }

    # ── Agent lifecycle ──────────────────────────────────────────────────

    def _make_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def spawn_root(self, model: str = "") -> AgentIdentity:
        agent = AgentIdentity(
            agent_id=self._make_id("root"),
            parent_id=None,
            depth=0,
            role="orchestrator",
            name="Root Orchestrator",
            model=model,
            status="running",
        )
        self.agents[agent.agent_id] = agent
        self.root_id = agent.agent_id
        return agent

    def spawn_lead(self, parent_id: str, role: str, model: str = "") -> AgentIdentity | None:
        if parent_id not in self.agents:
            return None
        if self.agents[parent_id].depth != 0:
            return None  # only Root can spawn Leads
        lead_count = sum(1 for a in self.agents.values() if a.depth == 1)
        if lead_count >= MAX_LEADS:
            return None
        agent = AgentIdentity(
            agent_id=self._make_id(f"lead-{role}"),
            parent_id=parent_id,
            depth=1,
            role=role,
            name=f"Lead {role.replace('_', ' ').title()}",
            model=model,
            status="queued",
        )
        self.agents[agent.agent_id] = agent
        self.stats["total_agents"] += 1
        return agent

    def spawn_specialist(self, parent_id: str, objective: str, model: str = "") -> AgentIdentity | None:
        if parent_id not in self.agents:
            return None
        parent = self.agents[parent_id]
        if parent.depth != 1:
            return None  # only Leads can spawn Specialists
        sibling_count = sum(1 for a in self.agents.values() if a.parent_id == parent_id)
        if sibling_count >= MAX_SPECIALISTS_PER_LEAD:
            return None
        agent = AgentIdentity(
            agent_id=self._make_id("spec"),
            parent_id=parent_id,
            depth=2,
            role=parent.role,
            name=f"{parent.name} · Spec {sibling_count + 1}",
            model=model,
            status="queued",
        )
        self.agents[agent.agent_id] = agent
        self.stats["total_agents"] += 1
        return agent

    def register_agent(self, parent_id: str | None, role: str, name: str, model: str = "", depth: int = 1) -> AgentIdentity | None:
        """Generic registration: auto-derives depth from parent."""
        if parent_id and parent_id not in self.agents:
            return None
        if parent_id:
            parent_depth = self.agents[parent_id].depth
            depth = parent_depth + 1
            if depth > MAX_DEPTH:
                return None
        else:
            depth = 0
        agent = AgentIdentity(
            agent_id=self._make_id(role.split("_")[0]),
            parent_id=parent_id,
            depth=depth,
            role=role,
            name=name,
            model=model,
        )
        self.agents[agent.agent_id] = agent
        self.stats["total_agents"] += 1
        return agent

    # ── Task lifecycle ────────────────────────────────────────────────────

    def create_task(
        self,
        owner_agent_id: str,
        objective: str,
        *,
        parent_task_id: str | None = None,
        allowed_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        verification_commands: list[str] | None = None,
        priority: str = "medium",
        retry_limit: int = 3,
        max_turns: int = 40,
    ) -> TaskEnvelope:
        task = TaskEnvelope(
            task_id=self._make_id("task"),
            parent_task_id=parent_task_id,
            owner_agent_id=owner_agent_id,
            objective=objective,
            allowed_files=list(allowed_files or []),
            acceptance_criteria=list(acceptance_criteria or []),
            verification_commands=list(verification_commands or []),
            priority=priority,
            retry_limit=retry_limit,
            max_turns=max_turns,
            state="submitted",
        )
        with self._task_lock:
            self.tasks[task.task_id] = task
            self.stats["total_tasks"] += 1
        return task

    def dispatch_task(self, task_id: str) -> TaskEnvelope | None:
        with self._task_lock:
            task = self.tasks.get(task_id)
            if not task or task.state != "submitted":
                return None
            task.state = "dispatched"
            task.attempt = 0
        return task

    def start_task(self, task_id: str) -> TaskEnvelope | None:
        with self._task_lock:
            task = self.tasks.get(task_id)
            if not task or task.state not in ("dispatched", "recovering", "redispatched"):
                return None
            task.state = "working"
            task.attempt += 1
            task.lease_expires_at = str(time.time() + 300)
            task.heartbeat_at = str(time.time())
        return task

    def complete_task(self, task_id: str) -> TaskEnvelope | None:
        with self._task_lock:
            task = self.tasks.get(task_id)
            if not task or task.state not in ("working", "verifying"):
                return None
            task.state = "completed"
            self.stats["completed_tasks"] += 1
        return task

    def fail_task(self, task_id: str, failure: dict[str, Any]) -> TaskEnvelope | None:
        with self._task_lock:
            task = self.tasks.get(task_id)
            if not task:
                return None
            task.state = "failed"
            task.failure = failure
            self.stats["failed_tasks"] += 1
        return task

    def recover_task(self, task_id: str) -> TaskEnvelope | None:
        with self._task_lock:
            task = self.tasks.get(task_id)
            if not task or task.state != "failed":
                return None
            if task.attempt >= task.retry_limit:
                return None
            task.state = "recovering"
        return task

    def redispatch_task(self, task_id: str, new_owner: str) -> TaskEnvelope | None:
        with self._task_lock:
            task = self.tasks.get(task_id)
            if not task or task.state != "recovering":
                return None
            task.owner_agent_id = new_owner
            task.state = "redispatched"
        return task

    # ── A2A messaging ─────────────────────────────────────────────────────

    def send_message(
        self,
        sender_id: str,
        receiver_id: str,
        event_type: str,
        *,
        task_id: str = "",
        status: str = "ok",
        payload: dict[str, Any] | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> A2AMessage:
        msg = A2AMessage(
            message_id="",
            correlation_id=self.execution_id,
            task_id=task_id,
            sender_agent_id=sender_id,
            receiver_agent_id=receiver_id,
            event_type=event_type,
            status=status,
            payload=dict(payload or {}),
            diagnostics=list(diagnostics or []),
        )
        with self._msg_lock:
            self.message_log.append(msg)
            self.stats["messages_sent"] += 1
        write_debug_event("swarm.a2a", msg.to_dict())
        return msg

    # ── File ownership ────────────────────────────────────────────────────

    def claim_file(self, agent_id: str, path: str) -> bool:
        rel = str(path).replace("\\", "/")
        with self._lease_lock:
            if rel in self._file_leases and self._file_leases[rel] != agent_id:
                return False
            self._file_leases[rel] = agent_id
            return True

    def release_file(self, agent_id: str, path: str) -> None:
        rel = str(path).replace("\\", "/")
        with self._lease_lock:
            if self._file_leases.get(rel) == agent_id:
                del self._file_leases[rel]

    # ── Heartbeat ─────────────────────────────────────────────────────────

    def heartbeat(self, agent_id: str) -> None:
        with self._lease_lock:
            self._heartbeats[agent_id] = time.time()

    def check_heartbeats(self) -> list[str]:
        """Return list of agent IDs whose heartbeat has expired."""
        now = time.time()
        dead: list[str] = []
        with self._lease_lock:
            for aid, last in list(self._heartbeats.items()):
                if now - last > self._heartbeat_ttl:
                    dead.append(aid)
                    # Mark agent as crashed
                    if aid in self.agents:
                        self.agents[aid].status = "failed"
                    self.stats["agent_crashes"] += 1
                    # Release all files held by dead agent
                    for k, v in list(self._file_leases.items()):
                        if v == aid:
                            del self._file_leases[k]
        return dead

    # ── Concurrency ───────────────────────────────────────────────────────

    def acquire(self) -> bool:
        return self._semaphore.acquire(blocking=True)

    def release(self) -> None:
        try:
            self._semaphore.release()
        except ValueError:
            pass

    def running_count(self) -> int:
        return sum(1 for a in self.agents.values() if a.status == "running")

    # ── Completion gating ─────────────────────────────────────────────────

    def all_critical_clear(self) -> bool:
        """True when no Critical/High tasks remain pending/failed/blocked."""
        with self._task_lock:
            for t in self.tasks.values():
                if t.priority in ("critical", "high") and t.state not in ("completed",):
                    return False
        return True

    def completion_verdict(self) -> dict[str, Any]:
        """Return structured verdict usable by Final Verifier."""
        total = self.stats["total_tasks"]
        completed = self.stats["completed_tasks"]
        failed = self.stats["failed_tasks"]
        crashed = self.stats["agent_crashes"]
        return {
            "totalTasks": total,
            "completedTasks": completed,
            "failedTasks": failed,
            "agentCrashes": crashed,
            "allCriticalClear": self.all_critical_clear(),
            "pass": crashed == 0 and failed == 0 and completed > 0 and self.all_critical_clear(),
        }

    # ── Serialization (for UI streaming) ──────────────────────────────────

    def tree_dict(self) -> dict[str, Any]:
        """Return the full agent tree as a serializable dict for the 3D map."""

        def _subtree(agent_id: str) -> dict[str, Any]:
            agent = self.agents.get(agent_id)
            if not agent:
                return {}
            children = [c for c in self.agents.values() if c.parent_id == agent_id]
            return {
                **agent.to_dict(),
                "heartbeatAlive": (
                    time.time() - self._heartbeats.get(agent_id, 0) < self._heartbeat_ttl
                    if agent_id in self._heartbeats
                    else False
                ),
                "children": [_subtree(c.agent_id) for c in children],
            }

        return _subtree(self.root_id or "")

    def status_dict(self) -> dict[str, Any]:
        return {
            "executionId": self.execution_id,
            "totalAgents": len(self.agents),
            "agentCounts": {
                "queued": sum(1 for a in self.agents.values() if a.status == "queued"),
                "running": sum(1 for a in self.agents.values() if a.status == "running"),
                "completed": sum(1 for a in self.agents.values() if a.status == "completed"),
                "failed": sum(1 for a in self.agents.values() if a.status == "failed"),
                "blocked": sum(1 for a in self.agents.values() if a.status == "blocked"),
            },
            "taskCounts": {
                "submitted": sum(1 for t in self.tasks.values() if t.state == "submitted"),
                "dispatched": sum(1 for t in self.tasks.values() if t.state == "dispatched"),
                "working": sum(1 for t in self.tasks.values() if t.state == "working"),
                "verifying": sum(1 for t in self.tasks.values() if t.state == "verifying"),
                "completed": sum(1 for t in self.tasks.values() if t.state == "completed"),
                "failed": sum(1 for t in self.tasks.values() if t.state == "failed"),
                "recovering": sum(1 for t in self.tasks.values() if t.state == "recovering"),
            },
            "stats": dict(self.stats),
            "completionVerdict": self.completion_verdict(),
        }
