"""Recovery Supervisor — handles agent crash→recover→redispatch flow.

Distinguishes infrastructure_error (agent code crash, NameError, OOM) from
product_failure (build/test fail, TS6133, eslint). Only the former triggers
agent restart+task reassignment; the latter creates a corrective CodeFixTask.

Integrates with SwarmOrchestrator and A2A Durable Broker to persist failure
evidence, release leases, and route recovery tasks.
"""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any

from .debug_log import write_debug_event

# Infrastructure error signatures — these are agent bugs, not product bugs.
_INFRA_SIGNALS = {
    "NameError", "AttributeError", "ImportError", "ModuleNotFoundError",
    "KeyError", "IndexError", "MemoryError", "RecursionError",
    "NotImplementedError", "TypeError: 'NoneType'", "ConnectionError",
    "asyncio.run() cannot be called", "event loop is already running",
    "cannot import name", "has no attribute",
}


def classify_failure(error: str | Exception) -> dict[str, Any]:
    """Return {type: infrastructure|product, retryable: bool, reason: str}."""
    msg = str(error)
    is_infra = any(sig in msg for sig in _INFRA_SIGNALS)
    # Also check traceback for infrastructure patterns
    if not is_infra and isinstance(error, Exception):
        tb = "".join(traceback.format_exception_only(type(error), error))
        is_infra = any(sig in tb for sig in _INFRA_SIGNALS)
    return {
        "type": "infrastructure" if is_infra else "product",
        "retryable": is_infra,  # product failures need code fix, not agent restart
        "reason": msg[:500],
    }


class RecoverySupervisor:
    """Watches agent heartbeats, handles crash→recover flow.

    Called from SwarmOrchestrator when an agent fails. Determines whether
    to restart the agent (infra error) or create a corrective task (product
    failure), then routes accordingly via the A2A broker.
    """

    def __init__(self, swarm: Any) -> None:
        self.swarm = swarm  # SwarmOrchestrator instance
        self.recovery_log: list[dict[str, Any]] = []
        self.max_recovery_attempts = 3

    def handle_agent_failure(
        self,
        agent_id: str,
        task_id: str,
        error: str | Exception,
        output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Main recovery entry point. Called on agent crash or task failure.

        Returns a recovery action dict that the orchestrator can execute.
        """
        classification = classify_failure(error)
        agent = self.swarm.agents.get(agent_id)
        agent_role = agent.role if agent else "unknown"
        agent_status = agent.status if agent else "failed"

        action: dict[str, Any] = {
            "agentId": agent_id,
            "taskId": task_id,
            "classification": classification,
            "timestamp": time.time(),
            "action": "noop",
        }

        write_debug_event("recovery.failure", {
            "agentId": agent_id, "taskId": task_id,
            "type": classification["type"], "error": classification["reason"],
        })

        if classification["type"] == "infrastructure":
            action.update(self._handle_infra_failure(agent_id, task_id, error, agent_role))
        else:
            action.update(self._handle_product_failure(agent_id, task_id, error, output))

        self.recovery_log.append(action)
        self.swarm.send_message(
            agent_id, "supervisor", "TASK_REQUEUED" if action.get("reassigned")
            else "TASK_FAILED", task_id=task_id, payload=action,
        )
        return action

    def _handle_infra_failure(
        self, agent_id: str, task_id: str, error: str | Exception, role: str,
    ) -> dict[str, Any]:
        """Agent itself crashed (NameError, OOM, etc). Release lease, find fallback, redispatch."""
        # Release all files held by the crashed agent.
        for path, owner in list(self.swarm._file_leases.items()):
            if owner == agent_id:
                del self.swarm._file_leases[path]

        # Mark agent failed.
        if agent_id in self.swarm.agents:
            self.swarm.agents[agent_id].status = "failed"
            self.swarm.stats["agent_crashes"] += 1

        # Find a sibling agent with the same role as fallback.
        task = self.swarm.tasks.get(task_id)
        fallback_id = None
        if task:
            parent_id = self.swarm.agents.get(agent_id).parent_id if agent_id in self.swarm.agents else None
            for aid, a in self.swarm.agents.items():
                if a.parent_id == parent_id and a.role == role and a.status not in ("failed", "blocked") and aid != agent_id:
                    fallback_id = aid
                    break
            # If no sibling, try any agent with the same role.
            if not fallback_id:
                for aid, a in self.swarm.agents.items():
                    if a.role == role and a.status not in ("failed", "blocked") and aid != agent_id:
                        fallback_id = aid
                        break

        if fallback_id and task and task.attempt < task.retry_limit:
            # Redispatch to fallback.
            self.swarm.redispatch_task(task_id, fallback_id)
            return {
                "action": "redispatch",
                "fallbackAgentId": fallback_id,
                "reassigned": True,
                "reason": f"Agent {agent_id} crashed ({role}): {str(error)[:200]}",
            }

        return {
            "action": "fail_permanent",
            "reassigned": False,
            "reason": f"No fallback available for {agent_id} ({role}) after crash",
        }

    def _handle_product_failure(
        self, agent_id: str, task_id: str, error: str | Exception, output: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Product code failure (build/test fail). Create a corrective CodeFixTask
        for the agent that owns the failing files, rather than restarting the agent."""
        # Extract file paths from the error/output for targeted fix.
        import re
        err_text = str(error)
        if output:
            err_text += " " + str(output.get("stderr", "")) + " " + str(output.get("stdout", ""))
        files = re.findall(r'([^\s:,]+\.(?:py|ts|tsx|js|jsx|vue|css|html))[:(\d]', err_text)
        files = sorted(set(f.replace("\\", "/") for f in files))[:20]

        # Find the coder/tester agent that owns those files.
        fix_agent = None
        for f in files:
            for aid, a in self.swarm.agents.items():
                if a.role in ("frontend", "backend", "api", "coder") and a.status not in ("failed", "blocked"):
                    fix_agent = aid
                    break
            if fix_agent:
                break
        if not fix_agent:
            fix_agent = agent_id  # same agent re-fixes

        return {
            "action": "create_corrective_task",
            "correctiveAgentId": fix_agent,
            "failingFiles": files,
            "reassigned": False,
            "reason": f"Product failure: {str(error)[:300]}",
        }

    # ── Periodic heartbeat check ──

    def check_and_recover(self) -> list[dict[str, Any]]:
        """Check all agent heartbeats; recover any dead ones. Returns recovery actions."""
        dead = self.swarm.check_heartbeats()
        actions = []
        for agent_id in dead:
            # Find the active task for this agent.
            active_task_id = None
            for tid, t in self.swarm.tasks.items():
                if t.owner_agent_id == agent_id and t.state in ("working", "verifying"):
                    active_task_id = tid
                    break
            action = self.handle_agent_failure(
                agent_id, active_task_id or f"recovery-{agent_id}",
                f"Heartbeat expired (TTL={self.swarm._heartbeat_ttl}s)",
            )
            actions.append(action)
        return actions
