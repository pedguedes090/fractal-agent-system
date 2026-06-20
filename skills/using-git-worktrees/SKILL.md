# Using Git Worktrees — He Thong Agent

Cô lập feature work trong git worktree riêng.

## Trigger

Trước khi bắt đầu implementation (sau khi plan được approved).

## Process

### Step 0: Detect Existing Isolation
- Check nếu đã trong worktree (`GIT_DIR != GIT_COMMON`)
- Nếu đã isolated → skip creation
- Guard chống false positives trong submodules

### Step 1: Create Workspace
- **Preferred**: dùng native tool (`EnterWorktree`, `/worktree`)
- **Fallback**: `git worktree add` với directory priority:
  1. Explicit instructions
  2. Existing `.worktrees/` hoặc `worktrees/`
  3. Default `.worktrees/`
- Verify directory được gitignored

### Step 2: Setup + Verify
- Auto-detect project type (Node/Rust/Python/Go)
- Install dependencies
- Run baseline tests — phải xanh trước khi bắt đầu

## Key Rules

- KHÔNG nest worktrees bên trong worktrees khác
- KHÔNG skip detection (Step 0)
- KHÔNG dùng `git worktree add` khi có native tool
- LUÔN verify `.gitignore` coverage
- LUÔN confirm clean test baseline

## Integration với He Thong Agent

Worktrees đã được tích hợp sẵn:
- `worktree_manager.py`: `prepare_execution_worktree()`, `merge_execution_worktree()`, `cleanup_execution_worktree()`
- `graph.py` `environment_gate`: auto-detect worktree vs direct workspace mode
- `container_sandbox.py`: container isolation trên worktree
- Direct workspace mode (default): edit trực tiếp trong folder đang mở
- Isolated mode: git worktree + merge sau review
