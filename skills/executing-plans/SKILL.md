# Executing Plans — He Thong Agent

Thực thi implementation plan trong session hiện tại với review checkpoints.

## Trigger

Sau khi có plan. Announce: "I'm using the executing-plans skill."

## Subagent Recommendation

Nếu subagents available → dùng `subagent-driven-development` thay vì executing-plans. Subagent-driven tốt hơn vì context cô lập, review tự động, parallel execution.

## Process

### Step 1: Load and Review Plan
- Đọc plan file
- Review critically — contradictions, gaps, risks
- Raise concerns với human partner trước khi bắt đầu
- Nếu không có concerns → tạo todos, proceed

### Step 2: Execute Tasks
- Mark mỗi task `in_progress`, sau đó `completed`
- Follow steps exactly as written
- Chạy TẤT CẢ verifications được chỉ định
- Commit sau mỗi task

### Step 3: Complete Development
- Announce: "I'm using the finishing-a-development-branch skill"
- Dùng `finishing-a-development-branch` skill
- Verify tests, present merge options

## When to Stop

Dừng ngay và hỏi khi:
- Hit blockers
- Tìm thấy critical gaps trong plan
- Không hiểu instructions
- Verification thất bại liên tục

## Key Rules

- Review plan critically trước khi bắt đầu
- Follow plan steps exactly
- KHÔNG skip verifications
- Reference skills khi plan yêu cầu
- KHÔNG implementation trên main/master khi chưa có consent
- Đừng force qua blockers — stop and ask

## Integration với He Thong Agent

Executing plans map vào:
- `openhands_worker` node: thực thi coding tasks
- `tester_agent` node: chạy verification commands
- `reviewer_decision` node: quyết định merge/rework
- Agent loop: timeout + bounded retry
