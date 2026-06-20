"""Agent orchestration loop with explicit state machine.

Architecture:
  - AgentLoop: the core RECEIVE→CLASSIFY→GATHER_CONTEXT→PLAN→EXECUTE→OBSERVE→VERIFY→REPLAN→FINALIZE loop
  - States: queued, running, waiting, completed, failed, cancelled
  - Explicit transitions, cancellation, timeout, retry
  - Checkpoint after critical steps for resume
  - Tích hợp vào LangGraph pipeline hiện tại, không thay thế
"""

from __future__ import annotations

import json
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .debug_log import write_debug_event
from .storage.models import (
    AgentRun,
    AgentStep,
    Message,
    ModelUsageRecord,
    Plan,
    RunStatus,
    StepStatus,
    _new_id,
    _now,
)
from .storage.run_repo import RunRepository, SQLiteRunRepository

# ---------------------------------------------------------------------------
# Agent Loop State
# ---------------------------------------------------------------------------


class LoopPhase(str, Enum):
    """Phases of the agent orchestration loop."""

    RECEIVE = "receive"
    CLASSIFY = "classify"
    GATHER_CONTEXT = "gather_context"
    ANALYSIS = "analysis"
    PLAN = "plan"
    WAIT_APPROVAL = "wait_approval"
    EXECUTE = "execute"
    OBSERVE = "observe"
    VERIFY = "verify"
    REPLAN = "replan"
    FINALIZE = "finalize"


@dataclass
class LoopConfig:
    """Configuration for the agent loop."""

    max_iterations: int = 10
    max_replan_attempts: int = 3
    max_tool_rounds: int = 10
    timeout_seconds: float = 1800.0  # 30 minutes
    checkpoint_after_phases: list[str] = field(
        default_factory=lambda: ["plan", "execute", "verify"]
    )

    @classmethod
    def from_env(cls) -> "LoopConfig":
        import os
        return cls(
            max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "10")),
            max_replan_attempts=max(0, int(os.getenv("AGENT_MAX_REPLAN_ATTEMPTS", "3"))),
            max_tool_rounds=int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "10")),
            timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "1800")),
        )


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------


@dataclass
class AgentLoop:
    """The core agent orchestration loop.

    Manages the lifecycle of an agent run through explicit phases.
    Each phase produces state that feeds into the next phase.
    Checkpoints are saved for resume-ability.
    """

    config: LoopConfig = field(default_factory=LoopConfig)
    repo: RunRepository | None = None
    emit: Callable[[str, str], None] | None = None

    # Callbacks — set by graph.py to integrate with LangGraph pipeline
    on_classify: Callable[[str], dict[str, Any]] | None = None
    on_gather_context: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    on_analyze: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    on_plan: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    on_execute: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None
    on_verify: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    on_replan: Callable[[dict[str, Any], str], dict[str, Any]] | None = None

    # Repo intelligence analysis state
    analysis_confidence: float = 0.0
    analysis_quality_gate: dict[str, Any] | None = None

    # Internal state
    _cancelled: bool = False
    _start_time: float = 0.0
    _current_phase: str = ""

    def cancel(self) -> None:
        """Cancel the current run."""
        self._cancelled = True
        if self.emit:
            self.emit("cancelled", "Agent run cancelled by user")

    def _check_cancelled(self) -> None:
        if self._cancelled:
            self._cancelled = False
            raise RuntimeError("Agent run was cancelled")

    def _check_timeout(self) -> None:
        if self._start_time > 0 and (time.monotonic() - self._start_time) > self.config.timeout_seconds:
            raise TimeoutError(f"Agent run timed out after {self.config.timeout_seconds}s")

    def _emit(self, stage: str, detail: str) -> None:
        if self.emit:
            self.emit(stage, detail)

    def _save_checkpoint(self, run: AgentRun, phase: str) -> None:
        """Save a checkpoint for resume."""
        if not self.repo or phase not in self.config.checkpoint_after_phases:
            return
        try:
            self.repo.save_checkpoint(
                run.id,
                phase,
                {
                    "run_id": run.id,
                    "phase": phase,
                    "status": run.status,
                    "step_count": len(run.steps),
                    "plan": run.plan.to_dict() if run.plan else None,
                    "timestamp_iso": _now(),
                },
            )
        except Exception as exc:
            write_debug_event("loop.checkpoint_error", {"phase": phase, "error": str(exc)})

    def run(self, task: str, *, conversation_id: str = "", session_id: str = "", workspace_path: str = "") -> AgentRun:
        """Execute the full agent loop on a task.

        Args:
            task: The user's task description
            conversation_id: Parent conversation
            session_id: Session identifier
            workspace_path: Path to the workspace

        Returns:
            AgentRun with results

        Raises:
            RuntimeError: if cancelled
            TimeoutError: if timed out
        """
        self._cancelled = False
        self._start_time = time.monotonic()

        run = AgentRun(
            id=_new_id(),
            conversation_id=conversation_id,
            session_id=session_id,
            task=task,
            status=RunStatus.QUEUED.value,
            prompt_version="5.0.0",
            workspace_path=workspace_path,
        )

        # Persist run if repo available
        if self.repo:
            run = self.repo.create_run(run)

        self._emit("running", f"Agent run {run.id} started")

        try:
            # --- Phase 1: RECEIVE ---
            self._current_phase = LoopPhase.RECEIVE.value
            run.status = RunStatus.RUNNING.value
            if self.repo:
                self.repo.update_run_status(run.id, RunStatus.RUNNING.value)

            self._emit("receive", "Receiving task")
            run.add_message(Message(role="user", content=task, conversation_id=conversation_id))
            self._emit("receive", f"Task: {task[:120]}")

            # --- Phase 2: CLASSIFY ---
            self._current_phase = LoopPhase.CLASSIFY.value
            self._check_cancelled()
            self._check_timeout()

            self._emit("classify", "Classifying task intent")
            classification: dict[str, Any] = {"taskIntent": {"mode": "modify", "requiresWorker": True, "readOnly": False, "riskClass": "medium"}}
            if self.on_classify:
                try:
                    classification = self.on_classify(task)
                except Exception as exc:
                    write_debug_event("loop.classify_error", {"error": str(exc)})

            step_classify = AgentStep(
                id=_new_id(),
                run_id=run.id,
                name="classify",
                role="orchestrator",
                status=StepStatus.COMPLETED.value,
                output_data=classification,
            )
            run.add_step(step_classify)
            if self.repo:
                self.repo.create_step(step_classify)

            intent = classification.get("taskIntent", {})
            self._emit("classify", f"Mode={intent.get('mode')}, risk={intent.get('riskClass')}, readOnly={intent.get('readOnly')}")

            # If read-only, skip execute/verify
            is_read_only = intent.get("readOnly", False) or not intent.get("requiresWorker", True)

            # --- Phase 3: GATHER_CONTEXT ---
            self._current_phase = LoopPhase.GATHER_CONTEXT.value
            self._check_cancelled()
            self._check_timeout()

            self._emit("gather_context", "Gathering workspace context")
            context: dict[str, Any] = {"workspacePath": workspace_path}
            if self.on_gather_context:
                try:
                    context = self.on_gather_context(task, classification)
                except Exception as exc:
                    write_debug_event("loop.context_error", {"error": str(exc)})

            step_context = AgentStep(
                id=_new_id(),
                run_id=run.id,
                name="gather_context",
                role="researcher",
                status=StepStatus.COMPLETED.value,
                output_data=context,
            )
            run.add_step(step_context)
            if self.repo:
                self.repo.create_step(step_context)

            # --- Phase 3b: ANALYSIS (repo intelligence) ---
            self._current_phase = LoopPhase.ANALYSIS.value
            self._check_cancelled()
            self._check_timeout()

            self._emit("analysis", "Running repository intelligence analysis")
            analysis_output: dict[str, Any] = {}
            if self.on_analyze:
                try:
                    analysis_output = self.on_analyze(task, {**classification, **context})
                except Exception as exc:
                    write_debug_event("loop.analysis_error", {"error": str(exc)})
                    analysis_output = {"error": str(exc)}

            self.analysis_confidence = float(analysis_output.get("analysisConfidence", 0.0))
            self.analysis_quality_gate = analysis_output.get("analysisQualityGate")
            self._emit("analysis", f"Confidence={self.analysis_confidence:.2f}; "
                       f"gate_passed={self.analysis_quality_gate.get('passed') if self.analysis_quality_gate else 'N/A'}")

            step_analysis = AgentStep(
                id=_new_id(),
                run_id=run.id,
                name="analysis",
                role="researcher",
                status=StepStatus.COMPLETED.value,
                output_data=analysis_output,
            )
            run.add_step(step_analysis)
            if self.repo:
                self.repo.create_step(step_analysis)

            # --- Phase 4: PLAN ---
            self._current_phase = LoopPhase.PLAN.value
            self._check_cancelled()
            self._check_timeout()

            self._emit("plan", "Generating plan")
            plan_output: dict[str, Any] = {}
            if self.on_plan:
                try:
                    plan_output = self.on_plan(task, {**classification, **context})
                except Exception as exc:
                    write_debug_event("loop.plan_error", {"error": str(exc)})
                    plan_output = {"error": str(exc)}

            run.plan = Plan(
                id=_new_id(),
                run_id=run.id,
                prompt_version="5.0.0",
                problem_statement=task,
                items=[],
            )
            if self.repo:
                self.repo.save_plan(run.plan)

            step_plan = AgentStep(
                id=_new_id(),
                run_id=run.id,
                name="plan",
                role="planner",
                status=StepStatus.COMPLETED.value,
                output_data=plan_output,
            )
            run.add_step(step_plan)
            if self.repo:
                self.repo.create_step(step_plan)

            self._save_checkpoint(run, "plan")

            # --- Phase 4b: WAIT_APPROVAL if high risk ---
            risk_class = plan_output.get("riskClass", classification.get("taskIntent", {}).get("riskClass", "medium"))
            needs_approval = risk_class == "high" or plan_output.get("needsApproval", False)

            if needs_approval:
                self._current_phase = LoopPhase.WAIT_APPROVAL.value
                run.status = RunStatus.PENDING_APPROVAL.value
                if self.repo:
                    self.repo.update_run_status(run.id, RunStatus.PENDING_APPROVAL.value)
                self._emit("wait_approval", f"High risk ({risk_class}): waiting for human approval")
                self._save_checkpoint(run, "wait_approval")
                return run  # Return early — caller handles re-entry with approval

            # --- Phase 5: EXECUTE ---
            if not is_read_only:
                self._current_phase = LoopPhase.EXECUTE.value
                self._check_cancelled()
                self._check_timeout()

                self._emit("execute", "Executing plan")
                execute_output: dict[str, Any] = {}
                if self.on_execute:
                    try:
                        execute_output = self.on_execute(plan_output, context)
                    except Exception as exc:
                        write_debug_event("loop.execute_error", {"error": str(exc)})
                        execute_output = {"error": str(exc), "changedFiles": []}

                step_execute = AgentStep(
                    id=_new_id(),
                    run_id=run.id,
                    name="execute",
                    role="coder",
                    status=StepStatus.FAILED.value if execute_output.get("error") else StepStatus.COMPLETED.value,
                    output_data=execute_output,
                    error=execute_output.get("error"),
                )
                run.add_step(step_execute)
                if self.repo:
                    self.repo.create_step(step_execute)

                self._save_checkpoint(run, "execute")

                # --- Phase 6: OBSERVE ---
                self._current_phase = LoopPhase.OBSERVE.value
                self._check_cancelled()
                self._check_timeout()

                changed_files = execute_output.get("changedFiles", [])
                self._emit("observe", f"Changed {len(changed_files)} file(s)")
                for f in changed_files[:5]:
                    self._emit("observe", f"  {f.get('status')}: {f.get('path')}")

                # --- Phase 7: VERIFY ---
                self._current_phase = LoopPhase.VERIFY.value
                self._check_cancelled()
                self._check_timeout()

                self._emit("verify", "Verifying results")
                verify_output: dict[str, Any] = {"passed": True, "blockers": [], "warnings": []}
                if self.on_verify:
                    try:
                        verify_output = self.on_verify(execute_output)
                    except Exception as exc:
                        write_debug_event("loop.verify_error", {"error": str(exc)})
                        verify_output = {"passed": False, "blockers": [str(exc)]}

                step_verify = AgentStep(
                    id=_new_id(),
                    run_id=run.id,
                    name="verify",
                    role="tester",
                    status=StepStatus.COMPLETED.value,
                    output_data=verify_output,
                )
                run.add_step(step_verify)
                if self.repo:
                    self.repo.create_step(step_verify)

                self._save_checkpoint(run, "verify")

                # --- Phase 8: REPLAN if verification fails ---
                replan_count = 0
                while (not verify_output.get("passed")) and replan_count < self.config.max_replan_attempts:
                    self._check_cancelled()
                    self._check_timeout()
                    replan_count += 1

                    self._current_phase = LoopPhase.REPLAN.value
                    blockers = verify_output.get("blockers", [])
                    self._emit("replan", f"Verification failed (attempt {replan_count}/{self.config.max_replan_attempts}). Replanning...")
                    self._emit("replan", f"Blockers: {'; '.join(str(b) for b in blockers[:3])}")

                    if self.on_replan:
                        try:
                            plan_output = self.on_replan(execute_output, "; ".join(str(b) for b in blockers))
                        except Exception as exc:
                            write_debug_event("loop.replan_error", {"error": str(exc)})
                            break

                    step_replan = AgentStep(
                        id=_new_id(),
                        run_id=run.id,
                        name=f"replan_{replan_count}",
                        role="planner",
                        status=StepStatus.COMPLETED.value,
                        output_data=plan_output,
                    )
                    run.add_step(step_replan)
                    if self.repo:
                        self.repo.create_step(step_replan)

                    # Re-execute
                    self._current_phase = LoopPhase.EXECUTE.value
                    self._emit("execute", f"Re-executing after replan (attempt {replan_count})")
                    if self.on_execute:
                        try:
                            execute_output = self.on_execute(plan_output, context)
                        except Exception as exc:
                            execute_output = {"error": str(exc), "changedFiles": []}

                    step_rexecute = AgentStep(
                        id=_new_id(),
                        run_id=run.id,
                        name=f"execute_{replan_count}",
                        role="coder",
                        status=StepStatus.FAILED.value if execute_output.get("error") else StepStatus.COMPLETED.value,
                        output_data=execute_output,
                        error=execute_output.get("error"),
                    )
                    run.add_step(step_rexecute)
                    if self.repo:
                        self.repo.create_step(step_rexecute)

                    # Re-verify
                    if self.on_verify:
                        try:
                            verify_output = self.on_verify(execute_output)
                        except Exception as exc:
                            verify_output = {"passed": False, "blockers": [str(exc)]}

                if replan_count >= self.config.max_replan_attempts and not verify_output.get("passed"):
                    self._emit("replan", f"Replan budget exhausted ({self.config.max_replan_attempts} attempts). Stopping.")

            # --- Phase 9: FINALIZE ---
            self._current_phase = LoopPhase.FINALIZE.value
            self._emit("finalize", "Finalizing run")

            if is_read_only:
                result = {
                    "text": plan_output.get("summary", "Read-only task completed."),
                    "changedFiles": [],
                    "review": {"passed": True},
                }
            elif execute_output.get("error"):
                run.mark_failed(execute_output["error"])
                result = {
                    "text": f"Execution failed: {execute_output['error']}",
                    "changedFiles": execute_output.get("changedFiles", []),
                    "review": verify_output,
                    "error": execute_output["error"],
                }
            elif not verify_output.get("passed"):
                run.mark_failed("Verification failed after replan attempts")
                result = {
                    "text": f"Verification failed: {'; '.join(str(b) for b in verify_output.get('blockers', []))}",
                    "changedFiles": execute_output.get("changedFiles", []),
                    "review": verify_output,
                    "partial": True,
                }
            else:
                run.mark_completed()
                result = {
                    "text": execute_output.get("summary", "Task completed successfully."),
                    "changedFiles": execute_output.get("changedFiles", []),
                    "review": verify_output,
                }

            self._emit("done", "Agent run completed")
            run.result = result

            if self.repo:
                self.repo.update_run_status(run.id, run.status, error=run.error)

            # Cleanup phase tracking
            self._current_phase = ""
            self._start_time = 0.0

            return run

        except RuntimeError as exc:
            # Cancellation
            if "cancelled" in str(exc).lower():
                run.mark_cancelled()
            else:
                run.mark_failed(str(exc))
            if self.repo:
                self.repo.update_run_status(run.id, run.status, error=run.error)
            raise

        except TimeoutError:
            run.mark_failed("Agent run timed out")
            if self.repo:
                self.repo.update_run_status(run.id, run.status, error=run.error)
            raise

        except Exception as exc:
            run.mark_failed(str(exc))
            if self.repo:
                self.repo.update_run_status(run.id, run.status, error=run.error)
            write_debug_event("loop.fatal", {
                "phase": self._current_phase,
                "error": str(exc),
                "traceback": traceback.format_exc()[-3000:],
            })
            raise


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_agent_loop(
    *,
    db_path: str = "",
    emit: Callable[[str, str], None] | None = None,
    config: LoopConfig | None = None,
) -> AgentLoop:
    """Create an AgentLoop with optional storage and callbacks."""
    loop = AgentLoop(config=config or LoopConfig(), emit=emit)

    if db_path:
        loop.repo = SQLiteRunRepository(db_path)

    return loop
