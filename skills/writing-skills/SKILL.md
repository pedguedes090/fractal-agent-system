# Writing Skills — He Thong Agent

Tạo hoặc cập nhật skills trong He Thong Agent.

## Trigger

Khi cần thêm capability mới hoặc cải thiện workflow hiện có.

## Skill Anatomy

Mỗi skill là 1 file `SKILL.md` trong `skills/<skill-name>/`:

```markdown
# Skill Name — He Thong Agent

Mô tả 1 câu về mục đích.

## Trigger
Khi nào skill này được kích hoạt.

## Process
Các bước cụ thể.

## Rules
Quy tắc bắt buộc.

## Integration với He Thong Agent
Cách skill map vào codebase hiện có.
```

## Principles

- **1 skill = 1 responsibility** — không multi-purpose
- **Concrete triggers** — rõ khi nào dùng
- **Checklists** — dễ verify
- **Red flags** — cảnh báo anti-patterns
- **Integration section** — map vào modules hiện có

## Quality Standards

- [ ] Trigger rõ ràng, không mơ hồ
- [ ] Process steps cụ thể, actionable
- [ ] Rules mandatory, không optional
- [ ] Integration section map đúng modules
- [ ] Không overlap với skills hiện có
- [ ] Follow Superpowers naming convention

## Testing

Sau khi viết skill:
1. Chạy skill trên ít nhất 1 real task
2. Verify tất cả steps follow được
3. Check integration points hoạt động
4. Iterate dựa trên feedback

## Integration

Skills được lưu trong `skills/` directory. Mỗi skill có thể reference:
- `engine/agent_engine/` modules
- `docs/architecture/` ADRs
- Skills khác qua tên (ví dụ: "sau đó invoke `writing-plans`")
