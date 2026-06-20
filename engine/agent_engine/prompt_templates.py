"""Prompt templates — separated from business logic with versioning.

Architecture:
  - Each template has a name, version, and render() method
  - Templates are stored here, not inline in graph nodes
  - Prompt version is logged per agent run for audit trail
  - Structured output schemas defined alongside prompts
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Prompt version (logged per run)
# ---------------------------------------------------------------------------

PROMPT_VERSION = "5.0.0"

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM_CODER = (
    "You are the Coder Agent in a multi-agent LangGraph pipeline.\n"
    "Follow the worker task spec exactly. Do not edit files outside allowedFiles.\n"
    "For project creation, create files beneath targetProjectDir and keep all nested paths there.\n"
    "Do not create tests unless the spec explicitly asks for tests.\n"
    "You receive only an explicit context envelope. Do not assume access to any prior conversation "
    "or agent output that is absent from it.\n"
    "Use the provided tools to read, write, and execute code within the workspace.\n"
)

SYSTEM_INTAKE = (
    "You are a read-only planning/review agent inside a LangGraph coding pipeline.\n"
    "Return valid JSON only. The user prefers Vietnamese final summaries.\n"
)

SYSTEM_READ_ONLY = "Answer in Vietnamese, concise and practical."

SYSTEM_SECURITY_REVIEWER = (
    "Security Reviewer Agent: review policy, auth, secret, permission, injection, "
    "destructive action, and sandbox violations.\n"
    "Docker/Podman absence is not a blocker when containerRequired is false and "
    "verification used the isolated host allowlist fallback.\n"
    "Return JSON with blockers[], warnings[], riskClass, reviewFocus[], passed boolean.\n"
)

SYSTEM_CODE_REVIEWER = (
    "Code Reviewer Agent: decide correctness, maintainability, regression risk, and merge readiness.\n"
    "Do not require an npm test script that the project does not define when its selected build/check commands pass.\n"
    "Optional Docker/Podman fallback is a warning, not a blocker.\n"
    "When repoIntelligence.codebase_memory is present, use its hits[] qualified_name list as ground truth "
    "for what code actually exists — flag claims that contradict it as blockers, not assumptions.\n"
    "Return JSON with blockers[], warnings[], passed boolean, finalMessage.\n"
)

SYSTEM_TESTER = (
    "Tester Agent: interpret isolated verification results. Only failed executed commands, "
    "coder errors, or sandbox violations are blockers.\n"
    "A missing optional npm script or optional Docker/Podman fallback is a warning, not a blocker.\n"
    "Return JSON with blockers[], warnings[], passed boolean, finalMessage.\n"
)

SYSTEM_PLANNING = (
    "You are a planning agent. Return a structured plan with steps, file changes needed, "
    "and verification strategy. Be specific. Return valid JSON.\n"
    "If repoIntelligence.codebase_memory.hits[] is present, ground every file/symbol reference "
    "in your plan on entries from that list — do not invent function or file names. Each hit gives "
    "name + qualified_name + file + line; cite the file path in fileChanges[].\n"
)

SYSTEM_CRITIQUE = (
    "You are a critique agent. Review the plan for risks, test coverage gaps, "
    "security issues, and regression risks. Return valid JSON.\n"
)


# ---------------------------------------------------------------------------
# Prompt templates (rendered at call time with state context)
# ---------------------------------------------------------------------------


class PromptTemplate:
    """A versioned prompt template."""

    def __init__(self, name: str, version: str, template: str) -> None:
        self.name = name
        self.version = version
        self.template = template

    def render(self, **variables: Any) -> str:
        """Render the template with given variables."""
        result = self.template
        for key, value in variables.items():
            placeholder = "{{" + key + "}}"
            result = result.replace(placeholder, str(value))
        return result.strip()


# --- Intake prompts ---

INTAKE_USER_INTENT = PromptTemplate(
    name="intake_user_intent",
    version=PROMPT_VERSION,
    template=(
        "Read-only Intake Agent A: identify user intent.\n"
        "Return JSON with goal, taskType, expectedOutcome, nonGoals.\n"
        "{{context}}"
    ),
)

INTAKE_AMBIGUITY = PromptTemplate(
    name="intake_ambiguity",
    version=PROMPT_VERSION,
    template=(
        "Read-only Intake Agent B: find ambiguities, edge cases, and risk.\n"
        "Return JSON with ambiguities[], assumptions[], riskClass, needsHumanApproval.\n"
        "{{context}}"
    ),
)

INTAKE_REPO_CONTEXT = PromptTemplate(
    name="intake_repo_context",
    version=PROMPT_VERSION,
    template=(
        "Read-only Intake Agent C: use trusted repo context and snapshot.\n"
        "When repoIntelligence.codebase_memory.hits[] is provided, those are EXACT matches from a "
        "tree-sitter knowledge graph (not a fuzzy LLM guess). Treat them as authoritative for "
        "relevantFiles[]: each hit's file is a file that genuinely contains a matching symbol.\n"
        "Return JSON with relevantFiles[], likelyCommands[], repoConventions[], warnings[].\n"
        "{{context}}"
    ),
)

INTAKE_SYNTHESIZER = PromptTemplate(
    name="intake_synthesizer",
    version=PROMPT_VERSION,
    template=(
        "Intake Synthesizer: merge findings.\n"
        "Return JSON with problemStatement, taskType, observedBehavior, expectedBehavior, "
        "repro, constraints[], riskClass, relevantFiles[], likelyCommands[], acceptanceCriteria[].\n"
        "Respect deterministicTaskIntent for readOnly/requiresWorker; do not classify a task "
        "as read-only when it contains explicit edit/fix/create signals.\n"
        "{{context}}"
    ),
)

# --- Planning prompts ---

PLANNING_TEMPLATE = PromptTemplate(
    name="planning",
    version=PROMPT_VERSION,
    template=(
        "Read-only Planning Agent {{name}}: {{focus}}.\n"
        "Return JSON with name, rationale, steps[], filesToRead[], filesLikelyToEdit[], "
        "commandsToRun[], risks[].\n"
        "{{context}}"
    ),
)

# --- Critique prompts ---

CRITIQUE_TEMPLATE = PromptTemplate(
    name="critique",
    version=PROMPT_VERSION,
    template=(
        "Critique Layer {{name}}: {{focus}}.\n"
        "Return JSON with blockers[], warnings[], riskClass, acceptanceCriteria[], "
        "reviewFocus[], requiredCommands[].\n"
        "{{context}}"
    ),
)

# --- Arbiter ---

PLAN_ARBITER = PromptTemplate(
    name="plan_arbiter",
    version=PROMPT_VERSION,
    template=(
        "Plan Arbiter: choose final plan and produce workerTaskSpec.\n"
        "Return JSON with selectedPlanName, finalSteps[], riskClass, humanGateReason, "
        "workerTaskSpec{objective, filesToRead[], allowedFiles[], forbiddenPaths[], "
        "commandsToRun[], verificationCommands[], acceptanceCriteria[], constraints[], maxReworkAttempts}.\n"
        "The workerTaskSpec must be a machine-executable contract: objective, allowed paths, "
        "forbidden actions, expected files, verification commands, definition of done, and "
        "human escalation conditions.\n"
        "For new web apps, set workerTaskSpec.targetProjectDir and verificationCwd to the app "
        "folder such as todo-app. Keep scaffold/setup/dev-server commands out of "
        "verificationCommands. Use verificationCommands only for build/test/check commands "
        "such as npm run build.\n"
        "{{context}}"
    ),
)

# --- Tester ---

TESTER_REVIEW = PromptTemplate(
    name="tester_review",
    version=PROMPT_VERSION,
    template=(
        "{{system_prompt}}\n{{context}}\n"
        "verificationCommands: {{verification_commands}}\n"
        "commandResults: {{command_results}}\n"
        "codegraphAffectedTests: {{affected_tests}}\n"
        "reviewPolicy: {{review_policy}}"
    ),
)

# --- Security reviewer ---

SECURITY_REVIEW = PromptTemplate(
    name="security_review",
    version=PROMPT_VERSION,
    template=(
        "{{system_prompt}}\n{{context}}"
    ),
)

# --- Code reviewer ---

CODE_REVIEW = PromptTemplate(
    name="code_review",
    version=PROMPT_VERSION,
    template=(
        "{{system_prompt}}\n{{context}}"
    ),
)

# --- Read-only reporter ---

READ_ONLY_REPORT = PromptTemplate(
    name="read_only_report",
    version=PROMPT_VERSION,
    template="System: {{system_prompt}}\nUser: {{context}}",
)


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------


def json_schema_intake_finding() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "taskType": {"type": "string", "enum": ["modify", "review", "answer", "create", "command"]},
            "expectedOutcome": {"type": "string"},
            "nonGoals": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["goal", "taskType", "expectedOutcome"],
    }


def json_schema_ambiguity() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "riskClass": {"type": "string", "enum": ["low", "medium", "high"]},
            "needsHumanApproval": {"type": "boolean"},
        },
    }


def json_schema_repo_context() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "relevantFiles": {"type": "array", "items": {"type": "string"}},
            "likelyCommands": {"type": "array", "items": {"type": "string"}},
            "repoConventions": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


def json_schema_problem() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "problemStatement": {"type": "string"},
            "taskType": {"type": "string"},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "riskClass": {"type": "string", "enum": ["low", "medium", "high"]},
            "relevantFiles": {"type": "array", "items": {"type": "string"}},
            "likelyCommands": {"type": "array", "items": {"type": "string"}},
            "acceptanceCriteria": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["problemStatement", "taskType"],
    }


def json_schema_plan() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "rationale": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "string"}},
            "filesToRead": {"type": "array", "items": {"type": "string"}},
            "filesLikelyToEdit": {"type": "array", "items": {"type": "string"}},
            "commandsToRun": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
        },
    }


def json_schema_critique() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "blockers": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "riskClass": {"type": "string", "enum": ["low", "medium", "high"]},
            "acceptanceCriteria": {"type": "array", "items": {"type": "string"}},
            "reviewFocus": {"type": "array", "items": {"type": "string"}},
            "requiredCommands": {"type": "array", "items": {"type": "string"}},
        },
    }


def json_schema_review() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "blockers": {"type": "array", "items": {"type": "string"}},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "passed": {"type": "boolean"},
            "finalMessage": {"type": "string"},
        },
    }


def json_schema_final_plan() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "selectedPlanName": {"type": "string"},
            "finalSteps": {"type": "array", "items": {"type": "string"}},
            "riskClass": {"type": "string", "enum": ["low", "medium", "high"]},
            "humanGateReason": {"type": "string"},
            "workerTaskSpec": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "filesToRead": {"type": "array", "items": {"type": "string"}},
                    "allowedFiles": {"type": "array", "items": {"type": "string"}},
                    "forbiddenPaths": {"type": "array", "items": {"type": "string"}},
                    "commandsToRun": {"type": "array", "items": {"type": "string"}},
                    "verificationCommands": {"type": "array", "items": {"type": "string"}},
                    "acceptanceCriteria": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "maxReworkAttempts": {"type": "integer"},
                },
            },
        },
    }
