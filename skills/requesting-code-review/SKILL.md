# Requesting Code Review — He Thong Agent

Dispatch subagent reviewer sau mỗi task hoặc trước khi merge.

## When to Use

- **Mandatory**: sau mỗi task trong subagent-driven development
- **Mandatory**: sau major features
- **Mandatory**: trước khi merge
- **Optional**: khi stuck, trước refactoring, sau complex bug fixes

## Process

1. Get `BASE_SHA` và `HEAD_SHA` git references
2. Dispatch review subagent với:
   - Mô tả ngắn gọn thay đổi
   - Plan hoặc requirements
   - Diff context (BASE..HEAD)
3. Act on feedback theo severity:
   - **Critical**: fix ngay, block merge
   - **Important**: fix trước merge
   - **Minor**: có thể defer

## Reviewer Context

Reviewer chỉ nhận crafted context — không session history — để focus vào work product:
- Diff của thay đổi
- Plan/requirements đã approved
- Architecture constraints
- Test results

## Core Principle

"Review early, review often." Bắt lỗi trước khi compound.

## Red Flags

- Bỏ review cho "simple" changes
- Ignore Critical issues
- Merge với unfixed Important issues
- Argue với valid technical feedback (push back với reasoning OK nếu reviewer sai)

## Integration với He Thong Agent

Code review tích hợp qua:
- `code_reviewer_agent` node: correctness + merge readiness
- `security_reviewer_agent` node: security + policy
- `reviewer_decision` node: aggregate → merge/rework
- `_sanitize_review_claims()`: downgrade false blockers
- `release_deploy_agent` node: release notes + rollback plan
