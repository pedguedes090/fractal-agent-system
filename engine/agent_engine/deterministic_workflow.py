from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from langgraph.graph import END, START


class WorkflowConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RouteCase:
    expression: str
    target: str


class DeterministicWorkflow:
    def __init__(self, raw: dict[str, Any], source: Path) -> None:
        self.raw = raw
        self.source = source
        self.name = str(raw.get("name") or source.stem)
        self.version = int(raw.get("version") or 0)
        self.nodes = tuple(map(str, raw.get("nodes") or []))
        self.node_set = set(self.nodes)
        self.limits = dict(raw.get("limits") or {})
        self.context_routes = {
            str(node): tuple(map(str, paths or []))
            for node, paths in (raw.get("contextRoutes") or {}).items()
        }
        self._environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        self._validate()

    @classmethod
    def load(cls, path: Path | None = None) -> "DeterministicWorkflow":
        source = path or Path(__file__).resolve().parent / "workflows" / "default.yaml"
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise WorkflowConfigError(f"Workflow {source} must contain a YAML object.")
        return cls(raw, source)

    def _validate_target(self, value: Any) -> str:
        target = str(value)
        if target not in self.node_set and target != "END":
            raise WorkflowConfigError(f"Unknown workflow target: {target}")
        return target

    def _validate_source(self, value: Any) -> str:
        source = str(value)
        if source not in self.node_set and source != "START":
            raise WorkflowConfigError(f"Unknown workflow source: {source}")
        return source

    def _validate(self) -> None:
        if self.version != 1:
            raise WorkflowConfigError(f"Unsupported workflow version: {self.version}")
        if not self.nodes or len(self.nodes) != len(self.node_set):
            raise WorkflowConfigError("Workflow nodes must be a non-empty unique list.")
        for edge in self.raw.get("edges") or []:
            if not isinstance(edge, list) or len(edge) != 2:
                raise WorkflowConfigError(f"Invalid edge: {edge}")
            self._validate_source(edge[0])
            self._validate_target(edge[1])
        for source, targets in (self.raw.get("fanOut") or {}).items():
            self._validate_source(source)
            for target in targets or []:
                self._validate_target(target)
        for join in self.raw.get("joins") or []:
            sources = join.get("sources") if isinstance(join, dict) else None
            target = join.get("target") if isinstance(join, dict) else None
            if not sources or not target:
                raise WorkflowConfigError(f"Invalid join: {join}")
            for source in sources:
                self._validate_source(source)
            self._validate_target(target)
        for route_node, config in (self.raw.get("routes") or {}).items():
            self._validate_source(route_node)
            if not isinstance(config, dict) or not config.get("default"):
                raise WorkflowConfigError(f"Route {route_node} must define a default target.")
            self._validate_target(config["default"])
            for case in config.get("cases") or []:
                if not isinstance(case, dict) or not case.get("when") or not case.get("target"):
                    raise WorkflowConfigError(f"Invalid route case for {route_node}: {case}")
                self._environment.compile_expression(str(case["when"]), undefined_to_none=False)
                self._validate_target(case["target"])
        for node, paths in self.context_routes.items():
            if node not in self.node_set:
                raise WorkflowConfigError(f"Context route references unknown node: {node}")
            if not paths:
                raise WorkflowConfigError(f"Context route for {node} cannot be empty.")

    def route(self, node_name: str, facts: dict[str, Any]) -> str:
        config = (self.raw.get("routes") or {}).get(node_name)
        if not isinstance(config, dict):
            raise WorkflowConfigError(f"Node {node_name} has no deterministic route.")
        for raw_case in config.get("cases") or []:
            expression = str(raw_case["when"])
            evaluator = self._environment.compile_expression(expression, undefined_to_none=True)
            if evaluator(**facts):
                return str(raw_case["target"])
        return str(config["default"])

    def context_for(self, node_name: str, state: dict[str, Any]) -> dict[str, Any]:
        paths = self.context_routes.get(node_name)
        if not paths:
            raise WorkflowConfigError(f"No explicit context route configured for {node_name}.")
        inputs: dict[str, Any] = {}
        for path in paths:
            value = _get_path(state, path)
            if value is not _MISSING:
                inputs[path] = copy.deepcopy(value)
        return {
            "schemaVersion": 1,
            "workflow": self.name,
            "targetNode": node_name,
            "allowedPaths": list(paths),
            "inputs": inputs,
        }

    def apply(
        self,
        builder: Any,
        fact_provider: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        for raw_source, raw_target in self.raw.get("edges") or []:
            builder.add_edge(_graph_token(raw_source), _graph_token(raw_target))
        for raw_source, raw_targets in (self.raw.get("fanOut") or {}).items():
            for raw_target in raw_targets or []:
                builder.add_edge(_graph_token(raw_source), _graph_token(raw_target))
        for join in self.raw.get("joins") or []:
            builder.add_edge([_graph_token(source) for source in join["sources"]], _graph_token(join["target"]))
        for route_node, config in (self.raw.get("routes") or {}).items():
            targets = {
                str(case["target"]): _graph_token(case["target"])
                for case in config.get("cases") or []
            }
            targets[str(config["default"])] = _graph_token(config["default"])

            def router(state: dict[str, Any], active_node: str = str(route_node)) -> str:
                return self.route(active_node, fact_provider(active_node, state))

            builder.add_conditional_edges(str(route_node), router, targets)


_MISSING = object()


def _get_path(state: dict[str, Any], dotted_path: str) -> Any:
    current: Any = state
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _graph_token(value: Any) -> Any:
    name = str(value)
    if name == "START":
        return START
    if name == "END":
        return END
    return name


DEFAULT_WORKFLOW = DeterministicWorkflow.load()
