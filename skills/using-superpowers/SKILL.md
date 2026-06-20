# Using Superpowers — He Thong Agent

He Thong Agent follows the Superpowers methodology: clarify before building, plan before coding, test before implementing, review before merging.

## Workflow (7 Steps)

1. **brainstorming** — Làm rõ yêu cầu, khám phá alternatives, phê duyệt design
2. **using-git-worktrees** — Tạo workspace cô lập trên branch mới
3. **writing-plans** — Chia task thành bước 2-5 phút với đường dẫn file + code cụ thể
4. **subagent-driven-development** hoặc **executing-plans** — Dispatch subagents kèm review
5. **test-driven-development** — RED-GREEN-REFACTOR cycle
6. **requesting-code-review** — Review so với plan, block critical issues
7. **finishing-a-development-branch** — Verify tests, merge/PR

## Principles

- Design trước khi code. Không nhảy vào implementation.
- Mỗi task là đơn vị nhỏ nhất có test cycle riêng.
- Review sau mỗi task, không chỉ cuối branch.
- Subagents dùng context cô lập — không share toàn bộ session history.
- YAGNI ruthlessly — chỉ build thứ cần.
- Evidence over claims — test chứng minh code đúng.

## Integration với He Thong Agent Pipeline

Superpowers skills hoạt động cùng với LangGraph pipeline:
- **brainstorming** → đọc codebase, phân tích requirement, viết spec
- **writing-plans** → tạo workerTaskSpec cho graph nodes
- **executing-plans** → OpenHands worker thực thi task
- **test-driven-development** → verifier chạy test sau mỗi coder run
- **requesting-code-review** → security_reviewer + code_reviewer nodes

## References

- Architecture: `docs/architecture/0001-local-first-control-plane.md`
- Pipeline: `engine/agent_engine/workflows/default.yaml`
- Storage: `engine/agent_engine/storage/`
