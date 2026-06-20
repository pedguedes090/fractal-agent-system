# Receiving Code Review — He Thong Agent

Nhận và xử lý code review feedback một cách có hệ thống.

## Trigger

Khi reviewer trả về findings.

## Process

1. **Read all feedback** — đừng phản ứng ngay với từng item
2. **Group by severity**: Critical > Important > Minor
3. **Address Critical first** — từng cái một, verify fix
4. **Address Important** — không skip
5. **Decide on Minor** — fix hoặc defer với lý do
6. **Re-request review** nếu có Critical/Important changes

## Response Guidelines

- **Agree + fix**: "Fixed in `<commit>`. `<explanation of fix>`."
- **Disagree với reasoning**: "I disagree because `<technical reason>`. `<alternative>`."
- **Need clarification**: "Can you clarify `<specific question>`?"

## Anti-Patterns

- Ignoring feedback
- Defensive reactions
- Fixing symptoms thay vì addressing reviewer's concern
- Skipping re-review sau significant changes

## Integration với He Thong Agent

Receiving review map vào:
- Agent loop replan: review blockers → replan → re-execute
- `execution_gate` node: bounded auto-rework
- `human_gate` node: approval cho rework budget
- Durable execution: checkpoint trước/sau review
