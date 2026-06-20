# Verification Before Completion — He Thong Agent

Xác minh mọi thứ hoạt động trước khi đánh dấu "done".

## Trigger

Trước khi claim bất kỳ task/feature/bug fix nào là "done".

## Verification Checklist

### Code Quality
- [ ] Build passes (compile, typecheck, lint)
- [ ] Tất cả tests pass
- [ ] New tests cover new behavior
- [ ] Không regression trong existing tests

### Correctness
- [ ] Code match plan/spec
- [ ] Không stray changes ngoài scope
- [ ] Error handling covers edge cases
- [ ] Không hardcoded secrets/paths

### Integration
- [ ] Không break existing API
- [ ] Dependencies được khai báo
- [ ] Migration được test (nếu có)

### Documentation
- [ ] Code comments cho logic phức tạp
- [ ] API docs updated (nếu applicable)
- [ ] Plan updated (nếu có deviation)

## Red Flags

- "It works on my machine"
- "Tests pass locally" (không verify CI)
- "Just a small change, no need to verify"
- Nhảy qua checklist vì "time pressure"

## Process

1. Run full verification suite
2. Check từng item trong checklist
3. Nếu bất kỳ item nào fail → fix → re-verify FULL checklist
4. Chỉ claim "done" khi MỌI item pass

## Integration với He Thong Agent

Verification tích hợp qua:
- `verifier.py`: structured post-exec verification
- `tester_agent` node: sandboxed command execution
- `security_reviewer_agent` node: security checklist
- `code_reviewer_agent` node: correctness checklist
- Agent loop: verify gate trước finalize
