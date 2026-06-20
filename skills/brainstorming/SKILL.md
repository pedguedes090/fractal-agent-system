# Brainstorming — He Thong Agent

Biến ý tưởng thành design qua collaborative dialogue trước implementation. Hard gate: không code, scaffold, hoặc implementation skill đến khi design được phê duyệt.

## Trigger

Bất kỳ yêu cầu mới nào: feature, bug fix, refactor, project creation.

## Core Principles

- Mỗi lần một câu hỏi
- Prefer multiple choice (2-4 lựa chọn) hơn open-ended
- YAGNI ruthlessly
- Explore 2-3 alternatives trước khi chốt
- Validate incrementally
- Stay flexible

## Checklist (Phải theo thứ tự)

1. **Explore project context** — Đọc codebase hiện tại, CLAUDE.md, architecture docs
2. **Ask clarifying questions** — 1 câu hỏi/lần, multiple choice khi có thể
3. **Propose 2-3 approaches** — Mỗi approach có trade-offs rõ ràng
4. **Present design in sections** — Architecture, components, data flow, error handling, testing
5. **Write design doc** → `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
6. **Spec self-review** — Kiểm tra placeholder, contradiction, ambiguity, scope
7. **User reviews written spec** → approve → transition qua `writing-plans`

## Spec Self-Review Checklist

- [ ] No TBD, TODO, "fill in details"
- [ ] No internal contradictions
- [ ] Scope đủ nhỏ cho 1 plan
- [ ] Không ambiguous requirements
- [ ] Fix issues inline, không re-review

## Scope Check

Nếu request cover nhiều subsystem độc lập → flag ngay → decompose thành sub-projects. Mỗi sub-project có spec → plan → implementation riêng.

## Anti-Pattern

"Quá đơn giản, không cần design" → MỌI thứ đều cần design. Simple projects có design ngắn (vài câu), nhưng approval vẫn mandatory.

## Design Coverage

Address: architecture, components, data flow, error handling, testing. Scale mỗi section theo complexity.

## Transition

Sau khi user approve spec → invoke `writing-plans`. KHÔNG invoke skill implementation khác.

## Integration với He Thong Agent

Brainstorming output (spec document) map vào:
- `intake_user_intent` node: phân tích yêu cầu
- `intake_synthesizer` node: problem statement
- `planning_*` nodes: các plan alternatives
- `plan_arbiter` node: final plan = approved design
