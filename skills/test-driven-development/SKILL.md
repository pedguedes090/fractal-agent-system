# Test-Driven Development — He Thong Agent

Viết test trước. Xem nó fail. Viết code tối thiểu để pass.

## Iron Law

**KHÔNG CODE PRODUCTION NÀO MÀ KHÔNG CÓ FAILING TEST TRƯỚC**

Nếu viết code trước test → **xóa code đó đi**. Không giữ làm reference.

## RED-GREEN-REFACTOR Cycle

1. **RED** — Viết 1 test tối thiểu thể hiện behavior cần có
2. **Verify RED** — Xem nó fail đúng lý do (feature missing, không phải typo)
3. **GREEN** — Viết code đơn giản nhất để pass
4. **Verify GREEN** — Test pass, tất cả test khác vẫn pass
5. **REFACTOR** — Clean up, giữ green
6. **Repeat** cho feature tiếp theo

## Test Quality Standards

- **Minimal**: 1 behavior/test
- **Clear**: Tên mô tả behavior
- **Real code**: Tránh mock nếu không cần thiết
- **Shows intent**: Thể hiện API mong muốn

## Khi Nào Dùng TDD

**Luôn luôn**: new features, bug fixes, refactoring, behavior changes
**Ngoại lệ** (hỏi human partner): throwaway prototypes, generated code, config files

## Tại Sao Thứ Tự Quan Trọng

- Test viết sau code → pass ngay lập tức → không chứng minh gì
- Manual testing = ad-hoc, không systematic
- Test-first trả lời "cái này nên làm gì"; test-after trả lời "cái này làm gì"
- Sunk cost fallacy: giữ untested code = technical debt

## Red Flags — Dừng Lại và Làm Lại

- Code trước test
- Test pass ngay lập tức
- Không giải thích được tại sao test fail
- "Just this once"
- "Keep as reference" hoặc "adapt existing code"

## Verification Checklist

- [ ] Mọi function có test
- [ ] Đã xem từng test fail trước khi implement
- [ ] Mỗi test fail đúng lý do
- [ ] Code tối thiểu để pass
- [ ] Tất cả test pass
- [ ] Edge cases và errors đã covered

## Khi Bí

- **Không biết test thế nào** → Viết wished-for API, assertion first
- **Test quá phức tạp** → Design quá phức tạp; simplify
- **Mock mọi thứ** → Code quá coupled; dùng dependency injection

## Debugging

Không bao giờ fix bug không có test. Viết failing test reproduce bug → follow TDD cycle.

## Integration với He Thong Agent

TDD tích hợp qua:
- `verifier.py`: chạy test sau mỗi coder execution
- `tester_agent` node: sandboxed verification
- `code_reviewer_agent` node: kiểm tra test coverage
- Agent loop replan: test fail → replan → re-execute
