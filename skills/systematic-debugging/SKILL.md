# Systematic Debugging — He Thong Agent

Framework 4-phase để tìm root cause trước khi fix.

## Core Mandate

**KHÔNG FIX KHI CHƯA TÌM ROOT CAUSE**

Random fixes = lãng phí thời gian + tạo bug mới.

## The Four Phases

### Phase 1: Root Cause Investigation
- Đọc errors cẩn thận
- Reproduce consistently
- Check recent changes (`git diff`, `git log`)
- Gather evidence across components
- Trace data flow backward qua call stacks
- Không đưa ra hypotheses trong phase này

### Phase 2: Pattern Analysis
- Tìm working examples tương tự
- Compare với references (đọc COMPLETELY)
- Identify differences
- Understand dependencies

### Phase 3: Hypothesis and Testing
- Form 1 hypothesis rõ ràng
- Test minimally — 1 variable mỗi lần
- Verify trước khi tiếp tục
- Nếu hypothesis sai → back to Phase 1

### Phase 4: Implementation
- Viết failing test case FIRST
- Implement single fix tại root cause
- Verify fix works
- Check regressions
- Commit với message mô tả root cause

## Critical Escalation Rule

Nếu **3+ fixes đã fail** → STOP, question the architecture. Pattern này gợi ý design fundamentally unsound.

## When to Apply

**Luôn luôn** — kể cả "simple" bugs, đặc biệt dưới time pressure. Systematic debugging thực sự NHANH HƠN guess-and-check.

## Red Flags

- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "I've seen this before, I know the fix"
- "The error message is misleading"

Bất kỳ red flag nào → back to Phase 1.

## Integration với He Thong Agent

Systematic debugging tích hợp qua:
- `debug_log.py`: JSONL structured logging
- `telemetry.py`: OpenTelemetry traces
- `durable_execution.py`: checkpoint để reproduce
- Agent loop: verify failure → replan (có bounded retry)
- `code_reviewer_agent` node: phát hiện regression
