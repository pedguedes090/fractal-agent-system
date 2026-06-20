# Dispatching Parallel Agents — He Thong Agent

Pattern dispatch nhiều subagents chạy song song cho các task độc lập.

## Trigger

Khi có 2+ independent tasks/failures có thể chạy parallel.

## Decision Flow

1. Multiple tasks/failures tồn tại?
2. Chúng có independent (không related, không shared state)?
3. Có thể chạy parallel?
4. Yes to all → dispatch parallel agents

Nếu related hoặc shared state → single agent hoặc sequential.

## Four Steps

1. **Group by independent domains** — ví dụ: theo test file, theo subsystem
2. **Create focused agent tasks** — scope cụ thể, goal rõ ràng, constraints, expected output
3. **Dispatch all trong cùng response** — multiple dispatch = parallel
4. **Review summaries** — verify không conflicts, chạy full test suite

## Effective Agent Prompts

- **Focused**: 1 problem domain
- **Self-contained**: đủ context
- **Specific output**: rõ expected format
- **Constraints**: giới hạn scope

## Tránh

- Quá rộng: "Fix all the tests"
- Không context: "Fix the race condition"
- Không constraints: agent refactor mọi thứ
- Vague output: không rõ expected format

## Không Dùng Khi

- Failures related — fix 1 có thể fix others
- Cần full system context
- Problem exploratory
- Agents sẽ interfere qua shared state

## Integration với He Thong Agent

Parallel dispatch map vào:
- `default.yaml` fanOut: `intake_*`, `planning_*`, `critique_*` nodes chạy parallel
- `multi_agent.py`: subtasks có `dependsOn` DAG → parallel khi không dependency
- `SQLiteAgentBroker`: dispatch subtasks concurrently
- `tool_registry.py`: ToolRegistry.execute() có thể gọi nhiều tools parallel
