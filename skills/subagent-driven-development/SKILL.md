# Subagent-Driven Development — He Thong Agent

Dispatch fresh subagents per task từ implementation plan, với review gate sau mỗi task.

## Trigger

Sau khi có plan được approved trong `writing-plans`.

## Process

1. **Pre-flight review** — Đọc plan, phát hiện contradictions giữa tasks hoặc với global constraints. Surface tất cả findings trong 1 batch question trước khi bắt đầu.

2. **Per task loop:**
   - Trích xuất task brief từ plan
   - Dispatch implementer subagent với: brief, interfaces từ task trước, context cần thiết
   - Implementer: implements → tests → commits → self-reviews → writes report
   - Dispatch task reviewer: kiểm tra code so với brief + plan
   - Nếu có Critical/Important findings → dispatch fix subagent → re-review
   - Mark complete, cập nhật progress ledger

3. **Final:** Dispatch whole-branch reviewer, sau đó `finishing-a-development-branch`

## Key Principles

- **Fresh subagent per task** — không context pollution
- **Không cho subagent đọc toàn bộ plan** — chỉ task brief hiện tại + interfaces
- **Continuous execution** — không check-in giữa các task trừ khi BLOCKED
- **Bulk artifacts as files** — briefs, reports, diffs (không paste text)
- **Explicit model choice** — dùng model yếu nhất đủ cho task
- **Progress ledger** — track trong `.superpowers/sdd/progress.md`

## Task Statuses

- `DONE` → ready for review
- `DONE_WITH_CONCERNS` → đọc concerns trước
- `NEEDS_CONTEXT` → cung cấp context thiếu
- `BLOCKED` → assess root cause (context, model, scope, plan)

## Comparison

- vs `executing-plans`: subagent-driven chạy nhanh hơn (parallel), review kỹ hơn, tự động hơn
- vs manual: bắt lỗi sớm hơn (review mỗi task), không bỏ sót

## Integration với He Thong Agent

Subagent-driven development map vào:
- `multi_agent.py`: task graph với subtasks + role contracts
- `SQLiteAgentBroker`: dispatch subtasks, track status
- `openhands_worker.py`: coder subagent thực thi
- `reviewer_decision` node: aggregate review results
- `execution_gate` node: bounded rework
