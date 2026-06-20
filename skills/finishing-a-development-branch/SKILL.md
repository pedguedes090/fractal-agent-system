# Finishing a Development Branch — He Thong Agent

Hoàn tất development branch: verify, clean up, merge/PR.

## Trigger

Sau khi tất cả tasks trong plan đã hoàn thành + review passed.

## Process

1. **Verify tests** — chạy toàn bộ test suite, phải xanh
2. **Check git status** — không còn uncommitted changes
3. **Review diff** — tổng kết thay đổi
4. **Present options**:
   - Merge vào main (nếu authorized)
   - Create PR (nếu cần review thêm)
   - Keep branch (nếu chưa sẵn sàng)
   - Discard (nếu không cần)
5. **Execute choice** — merge/PR/discard
6. **Clean up worktree** nếu dùng isolated mode

## Safety Checks

- [ ] Tất cả tests pass
- [ ] Code review completed + passed
- [ ] Security review completed + passed
- [ ] Không còn pending human gate
- [ ] Không conflict với source branch
- [ ] Worktree changes đã merge (nếu isolated mode)

## Integration với He Thong Agent

Finish branch map vào:
- `finalize_workspace` node: merge worktree vào source
- `worktree_manager.py`: conflict detection + safe merge
- `reviewer_decision` node: xác nhận all reviews passed
- `reporter` node: tổng kết changes + release notes
- Durable execution: mark run completed
