from __future__ import annotations

from typing import Any

from .agent_contracts import ROLE_ORDER, contracts_as_dict


def build_task_graph(task: str, problem: dict[str, Any], final_plan: dict[str, Any]) -> dict[str, Any]:
    spec = final_plan.get("workerTaskSpec") or {}
    subtasks = [
        {
            "role": "planner",
            "title": "Create task graph and role routing",
            "dependsOn": [],
            "input": {"task": task, "problem": problem, "finalPlan": final_plan},
            "expectedOutput": ["taskGraph", "subtasks", "role routing"],
        },
        {
            "role": "researcher_context",
            "title": "Ground task in trusted repository context",
            "dependsOn": ["planner"],
            "input": {"task": task, "relevantFiles": problem.get("relevantFiles", [])},
            "expectedOutput": ["contextSummary", "relevantFiles", "constraints", "riskNotes"],
        },
        {
            "role": "coder",
            "title": "Implement code changes inside allowedFiles",
            "dependsOn": ["researcher_context", "governance"],
            "input": {"workerTaskSpec": spec},
            "expectedOutput": ["changedFiles", "summary", "policyViolations"],
        },
        {
            "role": "tester",
            "title": "Run verification in sandbox",
            "dependsOn": ["coder"],
            "input": {"verificationCommands": spec.get("verificationCommands", [])},
            "expectedOutput": ["commandResults", "affectedTests", "blockers"],
        },
        {
            "role": "security_reviewer",
            "title": "Review security and policy risk",
            "dependsOn": ["coder", "tester"],
            "input": {"forbiddenPaths": spec.get("forbiddenPaths", [])},
            "expectedOutput": ["blockers", "warnings", "riskClass"],
        },
        {
            "role": "code_reviewer",
            "title": "Review correctness and merge readiness",
            "dependsOn": ["tester", "security_reviewer"],
            "input": {"acceptanceCriteria": spec.get("acceptanceCriteria", [])},
            "expectedOutput": ["blockers", "warnings", "passed", "finalMessage"],
        },
        {
            "role": "release_deploy",
            "title": "Prepare release/deploy and rollback notes",
            "dependsOn": ["code_reviewer"],
            "input": {"riskClass": final_plan.get("riskClass", problem.get("riskClass", "medium"))},
            "expectedOutput": ["releaseNotes", "deployPlan", "rollbackPlan", "needsApproval"],
        },
    ]
    return {
        "version": 1,
        "plannerRole": "planner",
        "roles": ROLE_ORDER,
        "contracts": contracts_as_dict(),
        "subtasks": subtasks,
    }


def governance_decision(
    task: str,
    task_intent: dict[str, Any],
    problem: dict[str, Any],
    final_plan: dict[str, Any],
    task_graph: dict[str, Any],
) -> dict[str, Any]:
    spec = final_plan.get("workerTaskSpec") or {}
    # Routing risk is deterministic. LLM-authored plan/problem risk fields are
    # review metadata only and cannot choose a graph branch.
    risk = str(task_intent.get("riskClass") or "medium").lower()
    text = f"{task} {problem.get('problemStatement', '')}".lower()
    sensitive_signals = [
        signal
        for signal in ["deploy", "release", "production", "prod", "migration", "database", "secret", "token", ".env", "permission", "auth", "delete", "drop"]
        if signal in text
    ]
    missing_allowed = not bool(spec.get("allowedFiles"))
    needs_approval = risk == "high" or bool(sensitive_signals) or missing_allowed
    blockers = []
    if missing_allowed and problem.get("requiresWorker"):
        blockers.append("Coder cannot run without workerTaskSpec.allowedFiles.")
    return {
        "service": "governance",
        "riskClass": "high" if needs_approval else risk,
        "needsApproval": needs_approval,
        "sensitiveSignals": sensitive_signals,
        "blockers": blockers,
        "approvalPolicy": "Human approval required for high-risk, deploy/release, secret/auth, permission, migration, or missing allowedFiles changes.",
        "taskGraphVersion": task_graph.get("version"),
    }


def researcher_output(
    problem: dict[str, Any],
    trusted_context: dict[str, Any],
    codegraph_context: dict[str, Any] | None,
    long_term_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trusted_files = [item.get("path") for item in trusted_context.get("files", []) if item.get("path")]
    codegraph_content = str((codegraph_context or {}).get("content") or "")
    memories = list((long_term_memory or {}).get("memories") or [])
    return {
        "contextSummary": "Trusted root instructions, CodeGraph context, and ACT-R long-term memory collected for downstream agents.",
        "relevantFiles": problem.get("relevantFiles", []),
        "trustedFiles": trusted_files,
        "codegraphEnabled": bool((codegraph_context or {}).get("enabled")),
        "codegraphSummary": codegraph_content[:6000],
        "longTermMemoryEnabled": bool((long_term_memory or {}).get("enabled")),
        "longTermMemorySummary": [
            {
                "kind": item.get("kind"),
                "source": item.get("source"),
                "activation": item.get("activation"),
                "tags": item.get("tags", []),
                "content": str(item.get("content") or "")[:600],
            }
            for item in memories[:6]
        ],
        "riskNotes": problem.get("constraints", []),
    }


def security_review_fallback(problem: dict[str, Any], worker_result: dict[str, Any], tester_result: dict[str, Any], governance: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    violations = worker_result.get("policyViolations") or []
    if violations:
        blockers.append("Worker attempted file changes outside allowedFiles or inside forbiddenPaths.")
    if governance.get("blockers"):
        blockers.extend(map(str, governance["blockers"]))
    if governance.get("needsApproval") and governance.get("sensitiveSignals"):
        warnings.append(f"Sensitive signals detected: {', '.join(governance['sensitiveSignals'])}")
    return {
        "blockers": blockers,
        "warnings": warnings,
        "riskClass": governance.get("riskClass") or problem.get("riskClass", "medium"),
        "reviewFocus": ["policy violations", "secrets/auth", "permissions", "destructive actions"],
        "passed": not blockers,
    }


def code_review_fallback(tester_result: dict[str, Any], security_review: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    blockers.extend(map(str, tester_result.get("blockers") or []))
    blockers.extend(map(str, security_review.get("blockers") or []))
    warnings.extend(map(str, tester_result.get("warnings") or []))
    warnings.extend(map(str, security_review.get("warnings") or []))
    return {
        "blockers": blockers,
        "warnings": warnings,
        "passed": not blockers,
        "finalMessage": "Code reviewer aggregated tester and security reviewer results.",
    }


def release_deploy_plan(final_plan: dict[str, Any], review: dict[str, Any], changed_files: list[dict[str, Any]]) -> dict[str, Any]:
    risk = str(final_plan.get("riskClass", "medium")).lower()
    needs_approval = risk == "high"
    paths = [item.get("path") for item in changed_files if item.get("path")]
    return {
        "releaseNotes": [f"Changed {path}" for path in paths] or ["No file changes to release."],
        "deployPlan": "No automatic deploy in desktop phase. Prepare manual deploy only after reviewer pass.",
        "rollbackPlan": "Use VCS diff/revert for changed files if post-review validation fails.",
        "needsApproval": needs_approval,
        "passed": bool(review.get("passed")) and not needs_approval,
    }


def reviewer_decision(tester_result: dict[str, Any], security_review: dict[str, Any], code_review: dict[str, Any], release_plan: dict[str, Any]) -> dict[str, Any]:
    blockers = []
    warnings = []
    for item in (tester_result, security_review, code_review):
        blockers.extend(map(str, item.get("blockers") or []))
        warnings.extend(map(str, item.get("warnings") or []))
    if release_plan.get("needsApproval"):
        warnings.append("Release/deploy agent requires manual approval before deploy.")
    passed = not blockers and bool(code_review.get("passed", True))

    # Structured verdict — determines graph routing
    if not blockers:
        verdict = "approved"
    elif any(
        signal in str(blocker).lower()
        for signal in ["architecture", "replan", "design", "redesign", "wrong approach", "kiến trúc", "thiết kế lại", "làm lại"]
        for blocker in blockers
    ):
        verdict = "replan_required"
    elif release_plan.get("needsApproval") and not passed:
        verdict = "blocked"
    else:
        verdict = "changes_required"

    return {
        "passed": passed,
        "verdict": verdict,
        "blockers": list(dict.fromkeys(blockers)),
        "warnings": list(dict.fromkeys(warnings)),
        "finalMessage": code_review.get("finalMessage") or "Reviewer decision complete.",
        "releasePlan": release_plan,
    }
