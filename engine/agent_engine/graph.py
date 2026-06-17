from __future__ import annotations

import json
import operator
import uuid
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

try:
    from langgraph.checkpoint.memory import InMemorySaver
except Exception:  # pragma: no cover
    from langgraph.checkpoint.memory import MemorySaver as InMemorySaver

from .llm_client import ChatClient
from .openhands_worker import run_openhands_worker
from .workspace import codegraph_affected_tests, codegraph_context, get_snapshot, normalize_verification_commands, read_file, run_command, trusted_context


class PipelineState(TypedDict, total=False):
    task: str
    workspacePath: str
    settings: dict[str, Any]
    messages: list[dict[str, Any]]
    sessionId: str
    preflight: dict[str, Any]
    taskIntent: dict[str, Any]
    codegraphContext: dict[str, Any]
    trustedRepoContext: dict[str, Any]
    intakeFindings: Annotated[list[dict[str, Any]], operator.add]
    problem: dict[str, Any]
    candidatePlans: Annotated[list[dict[str, Any]], operator.add]
    critiqueFindings: Annotated[list[dict[str, Any]], operator.add]
    finalPlan: dict[str, Any]
    contextFiles: list[dict[str, Any]]
    retryCount: int
    workerAttempts: Annotated[list[dict[str, Any]], operator.add]
    reviewFindings: Annotated[list[dict[str, Any]], operator.add]
    latestReview: dict[str, Any]
    result: dict[str, Any]


def _client(state: PipelineState) -> ChatClient:
    settings = state["settings"]
    return ChatClient(settings["serverUrl"], settings["model"])


def _json(state: PipelineState, prompt: str, fallback: dict[str, Any]) -> dict[str, Any]:
    return _client(state).json(prompt, fallback)


CHANGE_SIGNALS = [
    "sửa",
    "fix",
    "tạo",
    "thêm",
    "xóa",
    "xoá",
    "cập nhật",
    "triển khai",
    "làm ra",
    "xây",
    "khởi tạo",
    "chỉnh",
    "đổi",
    "thay",
    "bổ sung",
    "tối ưu",
    "nâng cấp",
    "cài",
    "implement",
    "write",
    "edit",
    "create",
    "update",
    "delete",
    "build",
    "scaffold",
    "refactor",
    "install",
    "generate",
]
READ_SIGNALS = [
    "đọc",
    "xem",
    "giải thích",
    "phân tích",
    "review",
    "tóm tắt",
    "trả lời",
    "là gì",
    "kiểm tra",
    "soi",
    "đánh giá",
    "tìm hiểu",
    "nghiên cứu",
    "explain",
    "summarize",
    "read",
    "analyze",
    "inspect",
]
NO_EDIT_SIGNALS = ["không sửa", "khong sua", "chỉ đọc", "chi doc", "read-only", "đừng sửa", "dung sua", "chưa sửa", "không đụng file"]
CONDITIONAL_EDIT_SIGNALS = ["sửa luôn", "fix luôn", "nếu sai thì sửa", "nếu có lỗi thì sửa", "nếu thấy lỗi thì sửa", "sai thì sửa"]
HIGH_RISK_SIGNALS = [
    "deploy",
    "production",
    "prod",
    "migration",
    "migrate",
    "database",
    "db",
    "secret",
    "token",
    ".env",
    "auth",
    "permission",
    "infra",
    "ci",
    "workflow",
    "drop",
    "remove data",
]


def _signals(text: str, patterns: list[str]) -> list[str]:
    value = text.lower()
    return [pattern for pattern in patterns if pattern in value]


def _risk_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(str(value or "").lower(), 1)


def _merge_risk(*values: str) -> str:
    ranked = max((_risk_rank(value), str(value or "medium").lower()) for value in values)
    return ranked[1] if ranked[1] in {"low", "medium", "high"} else "medium"


def _detect_task_intent(task: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    value = task.lower().strip()
    no_edit = _signals(value, NO_EDIT_SIGNALS)
    conditional_edit = _signals(value, CONDITIONAL_EDIT_SIGNALS)
    change = [] if no_edit else _signals(value, CHANGE_SIGNALS)
    read = _signals(value, READ_SIGNALS)
    project_creation = _is_project_creation_task(task, {"problemStatement": task})
    command_only = bool(_signals(value, ["chạy test", "chạy build", "run test", "run build", "npm run", "pytest"]))
    requires_worker = bool((change or conditional_edit or project_creation or command_only) and not no_edit)
    explicit_read_only = bool((read or "?" in value or no_edit) and not requires_worker)
    mode = "modify"
    if project_creation:
        mode = "create_project"
    elif command_only and not change:
        mode = "command"
    elif explicit_read_only:
        mode = "review" if any(signal in read for signal in ["review", "kiểm tra", "soi", "đánh giá", "tìm hiểu"]) else "answer"
    elif not requires_worker:
        mode = "ambiguous"
    risk = "high" if _signals(value, HIGH_RISK_SIGNALS) else ("medium" if requires_worker else "low")
    needs_clarification = mode == "ambiguous" and len(value) < 60
    return {
        "mode": mode,
        "requiresWorker": requires_worker,
        "readOnly": not requires_worker,
        "explicitNoEdit": bool(no_edit),
        "isProjectCreation": project_creation,
        "needsClarification": needs_clarification,
        "riskClass": risk,
        "signals": {
            "change": change,
            "read": read,
            "noEdit": no_edit,
            "conditionalEdit": conditional_edit,
        },
    }


def _normalize_problem(problem: dict[str, Any], state: PipelineState) -> dict[str, Any]:
    intent = state.get("taskIntent") or _detect_task_intent(state["task"], state.get("preflight"))
    normalized = dict(problem or {})
    llm_task_type = str(normalized.get("taskType", "")).lower()
    llm_requires_worker = llm_task_type in {"modify", "edit", "create", "implement", "fix", "refactor", "build", "scaffold", "command"}
    requires_worker = bool(intent.get("requiresWorker") or (llm_requires_worker and not intent.get("explicitNoEdit")))
    if requires_worker:
        normalized["taskType"] = "create" if intent.get("isProjectCreation") else ("command" if intent.get("mode") == "command" else "modify")
    elif intent.get("mode") in {"review", "answer"}:
        normalized["taskType"] = intent["mode"]
    else:
        normalized["taskType"] = normalized.get("taskType") or "question"
    normalized["requiresWorker"] = requires_worker
    normalized["readOnly"] = not requires_worker
    normalized["taskIntent"] = intent
    normalized["riskClass"] = _merge_risk(str(normalized.get("riskClass", "medium")), str(intent.get("riskClass", "medium")))
    normalized.setdefault("problemStatement", state["task"])
    for key in ("constraints", "relevantFiles", "likelyCommands", "acceptanceCriteria", "ambiguities", "nonGoals"):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []
    if intent.get("explicitNoEdit") and "Do not modify files; answer/report only." not in normalized["constraints"]:
        normalized["constraints"].append("Do not modify files; answer/report only.")
    if intent.get("needsClarification") and "Task is underspecified; ask a clarifying question before editing." not in normalized["constraints"]:
        normalized["constraints"].append("Task is underspecified; ask a clarifying question before editing.")
    return normalized


def _is_read_only(task: str, problem: dict[str, Any], intent: dict[str, Any] | None = None) -> bool:
    intent = intent or problem.get("taskIntent") or _detect_task_intent(task)
    if intent.get("explicitNoEdit"):
        return True
    if intent.get("requiresWorker"):
        return False
    task_type = str(problem.get("taskType", "")).lower()
    return task_type in {"question", "review", "explain", "answer"} or bool(intent.get("readOnly"))


def _is_project_creation_task(task: str, problem: dict[str, Any]) -> bool:
    value = f"{task} {problem.get('problemStatement', '')}".lower()
    return any(word in value for word in ["tạo", "thiết kế", "làm ra", "build", "create", "scaffold"]) and any(
        word in value for word in ["web", "app", "todo", "ứng dụng", "application"]
    )


def _default_project_dir(task: str) -> str:
    value = task.lower()
    if "todo" in value or "to-do" in value:
        return "todo-app"
    return "app"


def _normalize_worker_task_spec(final: dict[str, Any], state: PipelineState) -> dict[str, Any]:
    spec = dict(final.get("workerTaskSpec") or {})
    if _is_project_creation_task(state["task"], state["problem"]):
        target = str(spec.get("targetProjectDir") or spec.get("projectRoot") or _default_project_dir(state["task"])).strip().strip("/\\")
        spec["targetProjectDir"] = target
        spec.setdefault("projectRoot", target)
        spec.setdefault("verificationCwd", target)
        allowed = list(spec.get("allowedFiles") or [])
        if not allowed:
            allowed = [f"{target}/**"]
        spec["allowedFiles"] = allowed
        commands = [command for command in (spec.get("verificationCommands") or []) if isinstance(command, str)]
        if not any(command.strip().lower() == "npm run build" for command in commands):
            commands.append("npm run build")
        spec["verificationCommands"] = commands
        constraints = list(spec.get("constraints") or [])
        constraints.append("Scaffold/setup commands may be used by the OpenHands worker, but verification must run from targetProjectDir.")
        constraints.append("Do not use npm run dev/npm start as verification; they are long-running dev server commands.")
        spec["constraints"] = list(dict.fromkeys(constraints))
    final["workerTaskSpec"] = spec
    return final


def build_graph(emit: Callable[[str, str], None]):
    def preflight(state: PipelineState) -> dict[str, Any]:
        emit("preflight", "Repo snapshot + trusted context")
        snapshot = get_snapshot(state["workspacePath"])
        task_intent = _detect_task_intent(state["task"], snapshot)
        mode = task_intent.get("mode")
        route = "worker" if task_intent.get("requiresWorker") else "read-only"
        emit("task_intent", f"{mode} -> {route}; risk={task_intent.get('riskClass', 'medium')}")
        return {
            "preflight": snapshot,
            "taskIntent": task_intent,
            "trustedRepoContext": trusted_context(state["workspacePath"], snapshot),
            "retryCount": 0,
        }

    def codegraph_context_node(state: PipelineState) -> dict[str, Any]:
        emit("codegraph_context", "Checking semantic repo index")
        context = codegraph_context(state["workspacePath"], state["task"], auto_init=True)
        if context.get("enabled"):
            if context.get("autoInitialized"):
                emit("codegraph_context", "Index initialized for this workspace")
            detail = "Context ready"
            if context.get("truncated"):
                detail += " (truncated)"
            emit("codegraph_context", detail)
        else:
            emit("codegraph_context", f"Skipped: {context.get('reason') or context.get('status')}")
        return {"codegraphContext": context}

    def intake_user_intent(state: PipelineState) -> dict[str, Any]:
        emit("intake_user_intent", "Read-only user intent")
        finding = _json(
            state,
            "Read-only Intake Agent A: identify user intent. Return JSON with goal, taskType, expectedOutcome, nonGoals.\n"
            + json.dumps({"task": state["task"], "deterministicTaskIntent": state["taskIntent"], "snapshot": state["preflight"]}, ensure_ascii=False),
            {"goal": state["task"], "taskType": "modify", "expectedOutcome": "", "nonGoals": []},
        )
        return {"intakeFindings": [{"agent": "user_intent", **finding}]}

    def intake_ambiguity(state: PipelineState) -> dict[str, Any]:
        emit("intake_ambiguity", "Read-only ambiguity and edge cases")
        finding = _json(
            state,
            "Read-only Intake Agent B: find ambiguities, edge cases, and risk. Return JSON with ambiguities[], assumptions[], riskClass, needsHumanApproval.\n"
            + json.dumps({"task": state["task"], "deterministicTaskIntent": state["taskIntent"], "snapshot": state["preflight"]}, ensure_ascii=False),
            {"ambiguities": [], "assumptions": [], "riskClass": "medium", "needsHumanApproval": False},
        )
        return {"intakeFindings": [{"agent": "ambiguity_edge_cases", **finding}]}

    def intake_repo_context(state: PipelineState) -> dict[str, Any]:
        emit("intake_repo_context", "Read-only trusted repo context")
        finding = _json(
            state,
            "Read-only Intake Agent C: use trusted repo context and snapshot. Return JSON with relevantFiles[], likelyCommands[], repoConventions[], warnings[].\n"
            + json.dumps(
                {
                    "task": state["task"],
                    "deterministicTaskIntent": state["taskIntent"],
                    "trustedRepoContext": state["trustedRepoContext"],
                    "codegraphContext": state.get("codegraphContext"),
                    "snapshot": state["preflight"],
                },
                ensure_ascii=False,
            ),
            {"relevantFiles": [], "likelyCommands": [], "repoConventions": [], "warnings": []},
        )
        return {"intakeFindings": [{"agent": "trusted_repo_context", **finding}]}

    def intake_synthesizer(state: PipelineState) -> dict[str, Any]:
        emit("intake_synthesizer", "Problem statement + repro + risk class")
        problem = _json(
            state,
            "Intake Synthesizer: merge findings. Return JSON with problemStatement, taskType, observedBehavior, expectedBehavior, repro, constraints[], riskClass, relevantFiles[], likelyCommands[], acceptanceCriteria[].\n"
            "Respect deterministicTaskIntent for readOnly/requiresWorker; do not classify a task as read-only when it contains explicit edit/fix/create signals.\n"
            + json.dumps({"task": state["task"], "deterministicTaskIntent": state["taskIntent"], "codegraphContext": state.get("codegraphContext"), "findings": state["intakeFindings"]}, ensure_ascii=False),
            {"problemStatement": state["task"], "taskType": "modify", "constraints": [], "riskClass": "medium", "relevantFiles": [], "likelyCommands": [], "acceptanceCriteria": []},
        )
        problem = _normalize_problem(problem, state)
        return {"problem": problem}

    def plan_node(name: str, focus: str):
        def node(state: PipelineState) -> dict[str, Any]:
            emit(f"planning_{name}", focus)
            plan = _json(
                state,
                f"Read-only Planning Agent {name}: {focus}. Return JSON with name, rationale, steps[], filesToRead[], filesLikelyToEdit[], commandsToRun[], risks[].\n"
                + json.dumps({"taskIntent": state["taskIntent"], "problem": state["problem"], "codegraphContext": state.get("codegraphContext"), "snapshot": state["preflight"]}, ensure_ascii=False),
                {"name": name, "steps": [], "filesToRead": [], "filesLikelyToEdit": [], "commandsToRun": [], "risks": []},
            )
            return {"candidatePlans": [{"agent": name, **plan}]}

        return node

    def critique_node(name: str, focus: str):
        def node(state: PipelineState) -> dict[str, Any]:
            emit(f"critique_{name}", focus)
            critique = _json(
                state,
                f"Critique Layer {name}: {focus}. Return JSON with blockers[], warnings[], riskClass, acceptanceCriteria[], reviewFocus[], requiredCommands[].\n"
                + json.dumps({"taskIntent": state["taskIntent"], "problem": state["problem"], "candidatePlans": state["candidatePlans"]}, ensure_ascii=False),
                {"blockers": [], "warnings": [], "riskClass": state["problem"].get("riskClass", "medium"), "acceptanceCriteria": [], "reviewFocus": [], "requiredCommands": []},
            )
            return {"critiqueFindings": [{"agent": name, **critique}]}

        return node

    def plan_arbiter(state: PipelineState) -> dict[str, Any]:
        emit("plan_arbiter", "Final plan + acceptance criteria + worker task spec")
        final = _json(
            state,
            "Plan Arbiter: choose final plan and produce workerTaskSpec. Return JSON with selectedPlanName, finalSteps[], riskClass, humanGateReason, workerTaskSpec{objective, filesToRead[], allowedFiles[], forbiddenPaths[], commandsToRun[], verificationCommands[], acceptanceCriteria[], constraints[], maxReworkAttempts}.\n"
            "The workerTaskSpec must be a machine-executable contract: objective, allowed paths, forbidden actions, expected files, verification commands, definition of done, and human escalation conditions.\n"
            "For new web apps, set workerTaskSpec.targetProjectDir and verificationCwd to the app folder such as todo-app. Keep scaffold/setup/dev-server commands out of verificationCommands. Use verificationCommands only for build/test/check commands such as npm run build.\n"
            + json.dumps({"taskIntent": state["taskIntent"], "problem": state["problem"], "candidatePlans": state["candidatePlans"], "critiques": state["critiqueFindings"]}, ensure_ascii=False),
            {
                "selectedPlanName": "minimal",
                "finalSteps": [],
                "riskClass": state["problem"].get("riskClass", "medium"),
                "humanGateReason": "",
                "workerTaskSpec": {
                    "objective": state["problem"].get("problemStatement", state["task"]),
                    "filesToRead": state["problem"].get("relevantFiles", []),
                    "allowedFiles": [],
                    "forbiddenPaths": [],
                    "commandsToRun": [],
                    "verificationCommands": state["problem"].get("likelyCommands", []),
                    "acceptanceCriteria": state["problem"].get("acceptanceCriteria", []),
                    "constraints": state["problem"].get("constraints", []),
                    "maxReworkAttempts": 1,
                },
            },
        )
        final = _normalize_worker_task_spec(final, state)
        final["riskClass"] = _merge_risk(str(final.get("riskClass", "medium")), str(state["problem"].get("riskClass", "medium")))
        return {"finalPlan": final}

    def human_gate(state: PipelineState) -> dict[str, Any]:
        risk = str(state["finalPlan"].get("riskClass", state["problem"].get("riskClass", "medium"))).lower()
        auto_confirm = bool((state.get("settings") or {}).get("autoConfirmHumanGate"))
        if risk == "high" and auto_confirm:
            emit("human_gate", "Auto-confirm enabled; gate passed")
            return {}
        if risk == "high" and "xác nhận" not in state["task"].lower() and "confirm" not in state["task"].lower():
            emit("human_gate", "High-risk task requires confirmation")
            return {
                "result": {
                    "assistantText": "Tác vụ high-risk nên LangGraph dừng ở Human Gate. Gửi lại với chữ “xác nhận” để tiếp tục.",
                    "changedFiles": [],
                    "commandResults": [],
                    "review": None,
                    "humanGate": {
                        "status": "pending",
                        "originalTask": state["task"],
                        "riskClass": risk,
                        "reason": state["finalPlan"].get("humanGateReason") or state["problem"].get("riskClass", "high"),
                    },
                }
            }
        emit("human_gate", "Gate passed")
        return {}

    def read_only_reporter(state: PipelineState) -> dict[str, Any]:
        emit("reporter", "Read-only answer")
        answer = _client(state).chat(
            [
                {"role": "system", "content": "Answer in Vietnamese, concise and practical."},
                {"role": "user", "content": json.dumps({"task": state["task"], "problem": state["problem"], "codegraphContext": state.get("codegraphContext"), "finalPlan": state["finalPlan"]}, ensure_ascii=False)},
            ]
        )
        return {"result": {"assistantText": answer, "changedFiles": [], "commandResults": [], "review": None}}

    def load_context_files(state: PipelineState) -> dict[str, Any]:
        emit("context", "Loading worker context files")
        spec = state["finalPlan"].get("workerTaskSpec", {})
        paths = list(dict.fromkeys((state["problem"].get("relevantFiles") or []) + (spec.get("filesToRead") or []) + (spec.get("allowedFiles") or [])))[:12]
        files = []
        for path in paths:
            try:
                files.append({"path": path, "content": read_file(state["workspacePath"], path, 18000)})
            except Exception as exc:
                files.append({"path": path, "error": str(exc)})
        return {"contextFiles": files}

    def openhands_worker(state: PipelineState) -> dict[str, Any]:
        spec = state["finalPlan"].get("workerTaskSpec", {})
        emit("openhands_worker", "Single OpenHands coding worker")
        worker_result = run_openhands_worker(
            workspace=state["workspacePath"],
            server_url=state["settings"]["serverUrl"],
            model=state["settings"]["model"],
            worker_task_spec={**spec, "contextFiles": state.get("contextFiles", []), "codegraphContext": state.get("codegraphContext")},
            rework_context=state.get("latestReview"),
            emit=emit,
        )
        return {"workerAttempts": [worker_result], "retryCount": state.get("retryCount", 0) + 1}

    def automated_review_stack(state: PipelineState) -> dict[str, Any]:
        emit("automated_review", "Full tests + diff/security/regression review")
        latest = state["workerAttempts"][-1]
        spec = state["finalPlan"].get("workerTaskSpec", {})
        raw_commands = list(dict.fromkeys((spec.get("commandsToRun") or []) + (spec.get("verificationCommands") or [])))
        commands = normalize_verification_commands(state["workspacePath"], raw_commands, latest, spec)
        command_results = [
            run_command(state["workspacePath"], item["command"], cwd=item.get("cwd", "."))
            for item in commands
        ]
        affected = codegraph_affected_tests(state["workspacePath"], latest.get("changedFiles") or [])
        if affected.get("enabled") and affected.get("status") == "ok":
            emit("codegraph_affected", "Affected test candidates ready")
        review = _json(
            state,
            "Automated Review Stack: review full tests, diff, security, regression. Return JSON with blockers[], warnings[], passed boolean, finalMessage.\n"
            + json.dumps(
                {
                    "problem": state["problem"],
                    "workerTaskSpec": spec,
                    "workerResult": latest,
                    "verificationCommands": commands,
                    "commandResults": command_results,
                    "codegraphAffectedTests": affected,
                    "reviewPolicy": "Scaffold/setup/dev-server commands are intentionally excluded from verification. Review only commandResults and changedFiles.",
                },
                ensure_ascii=False,
            ),
            {"blockers": [], "warnings": [], "passed": True, "finalMessage": ""},
        )
        if latest.get("error"):
            review.setdefault("blockers", []).append(f"OpenHands worker error: {latest['error']}")
            review["passed"] = False
        if any((not item.get("skipped")) and (item.get("timedOut") or item.get("code") not in (0, None)) for item in command_results):
            review.setdefault("blockers", []).append("At least one verification command failed.")
            review["passed"] = False
        return {"latestReview": {**review, "commandResults": command_results, "codegraphAffectedTests": affected}, "reviewFindings": [review]}

    def reporter(state: PipelineState) -> dict[str, Any]:
        emit("reporter", "Final report")
        attempts = state.get("workerAttempts", [])
        latest = attempts[-1] if attempts else {}
        review = state.get("latestReview", {})
        changed = latest.get("changedFiles", [])
        lines = [latest.get("summary") or state["problem"].get("problemStatement", state["task"])]
        if changed:
            lines.append("\nFile đã thay đổi:")
            for item in changed:
                lines.append(f"- {item.get('status')}: {item.get('path')}")
        else:
            lines.append("\nKhông có file nào bị thay đổi.")
        if review.get("commandResults"):
            lines.append("\nLệnh verification:")
            for item in review["commandResults"]:
                status = f"skipped: {item.get('reason')}" if item.get("skipped") else ("timeout" if item.get("timedOut") else f"exit {item.get('code')}")
                cwd = item.get("cwd") or "."
                lines.append(f"- ({cwd}) {item.get('command')}: {status}")
        affected = review.get("codegraphAffectedTests") or {}
        if affected.get("enabled") and affected.get("status") == "ok":
            lines.append("\nCodeGraph affected tests:")
            raw = str(affected.get("raw") or "").strip()
            lines.append(raw if raw else "- Không có test liên quan được phát hiện.")
        blockers = review.get("blockers") or []
        warnings = review.get("warnings") or []
        if blockers:
            lines.append("\nBlocker: " + "; ".join(map(str, blockers)))
        if warnings:
            lines.append("\nLưu ý: " + "; ".join(map(str, warnings)))
        if review.get("finalMessage"):
            lines.append("\n" + str(review["finalMessage"]))
        return {
            "result": {
                "assistantText": "\n".join(lines),
                "changedFiles": changed,
                "commandResults": review.get("commandResults", []),
                "review": review,
                "reworkAttempts": attempts,
            }
        }

    def route_after_gate(state: PipelineState) -> str:
        if state.get("result"):
            return "reporter_end"
        if _is_read_only(state["task"], state["problem"], state.get("taskIntent")):
            return "read_only_reporter"
        return "load_context_files"

    def route_after_review(state: PipelineState) -> str:
        review = state.get("latestReview", {})
        attempts = state.get("workerAttempts", [])
        if attempts and attempts[-1].get("error"):
            return "reporter"
        spec = state["finalPlan"].get("workerTaskSpec", {})
        max_rework = min(2, int(spec.get("maxReworkAttempts") or 1))
        if review.get("blockers") and state.get("retryCount", 0) <= max_rework:
            return "openhands_worker"
        return "reporter"

    builder = StateGraph(PipelineState)
    builder.add_node("preflight", preflight)
    builder.add_node("codegraph_context", codegraph_context_node)
    builder.add_node("intake_user_intent", intake_user_intent)
    builder.add_node("intake_ambiguity", intake_ambiguity)
    builder.add_node("intake_repo_context", intake_repo_context)
    builder.add_node("intake_synthesizer", intake_synthesizer)
    builder.add_node("planning_minimal", plan_node("minimal", "minimal plan"))
    builder.add_node("planning_robust", plan_node("robust", "robust plan"))
    builder.add_node("planning_test_first", plan_node("test_first", "test-first plan"))
    builder.add_node("critique_risk", critique_node("risk", "risk"))
    builder.add_node("critique_test_coverage", critique_node("test_coverage", "test coverage"))
    builder.add_node("critique_security_regression", critique_node("security_regression", "security/regression"))
    builder.add_node("plan_arbiter", plan_arbiter)
    builder.add_node("human_gate", human_gate)
    builder.add_node("read_only_reporter", read_only_reporter)
    builder.add_node("load_context_files", load_context_files)
    builder.add_node("openhands_worker", openhands_worker)
    builder.add_node("automated_review_stack", automated_review_stack)
    builder.add_node("reporter", reporter)
    builder.add_node("reporter_end", lambda state: {})

    builder.add_edge(START, "preflight")
    builder.add_edge("preflight", "codegraph_context")
    builder.add_edge("codegraph_context", "intake_user_intent")
    builder.add_edge("codegraph_context", "intake_ambiguity")
    builder.add_edge("codegraph_context", "intake_repo_context")
    builder.add_edge(["intake_user_intent", "intake_ambiguity", "intake_repo_context"], "intake_synthesizer")
    builder.add_edge("intake_synthesizer", "planning_minimal")
    builder.add_edge("intake_synthesizer", "planning_robust")
    builder.add_edge("intake_synthesizer", "planning_test_first")
    builder.add_edge(["planning_minimal", "planning_robust", "planning_test_first"], "critique_risk")
    builder.add_edge(["planning_minimal", "planning_robust", "planning_test_first"], "critique_test_coverage")
    builder.add_edge(["planning_minimal", "planning_robust", "planning_test_first"], "critique_security_regression")
    builder.add_edge(["critique_risk", "critique_test_coverage", "critique_security_regression"], "plan_arbiter")
    builder.add_edge("plan_arbiter", "human_gate")
    builder.add_conditional_edges(
        "human_gate",
        route_after_gate,
        {"reporter_end": "reporter_end", "read_only_reporter": "read_only_reporter", "load_context_files": "load_context_files"},
    )
    builder.add_edge("read_only_reporter", END)
    builder.add_edge("reporter_end", END)
    builder.add_edge("load_context_files", "openhands_worker")
    builder.add_edge("openhands_worker", "automated_review_stack")
    builder.add_conditional_edges("automated_review_stack", route_after_review, {"openhands_worker": "openhands_worker", "reporter": "reporter"})
    builder.add_edge("reporter", END)

    return builder.compile(checkpointer=InMemorySaver())


def run_pipeline(payload: dict[str, Any], emit: Callable[[str, str], None]) -> dict[str, Any]:
    graph = build_graph(emit)
    state: PipelineState = {
        "task": payload["content"],
        "workspacePath": payload["workspacePath"],
        "settings": payload["settings"],
        "messages": payload.get("messages", []),
        "sessionId": payload.get("sessionId") or str(uuid.uuid4()),
    }
    result = graph.invoke(state, config={"configurable": {"thread_id": state["sessionId"]}})
    return {
        "id": str(uuid.uuid4()),
        "problem": result.get("problem"),
        "taskIntent": result.get("taskIntent"),
        "codegraphContext": result.get("codegraphContext"),
        "trustedRepoContext": result.get("trustedRepoContext"),
        "intake": result.get("intakeFindings", []),
        "plans": result.get("candidatePlans", []),
        "critiques": result.get("critiqueFindings", []),
        "finalPlan": result.get("finalPlan"),
        **(result.get("result") or {}),
        "task": state["task"],
    }
