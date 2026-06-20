# Writing Plans — He Thong Agent

Biến approved spec thành implementation plan chi tiết — từng bước 2-5 phút mà engineer không cần full codebase context cũng follow được.

## Trigger

Sau khi spec được approved trong brainstorming.

## Principles

- DRY, YAGNI, TDD, frequent commits
- Giả định developer skilled, minimal domain knowledge, weak test design
- Mỗi step = 1 action trong 2-5 phút
- Mỗi task có test cycle + review gate riêng

## Plan Structure

### Mandatory Header
```markdown
# Plan: <feature-name>
- **Goal**: <1 câu mô tả>
- **Architecture**: <pattern, layers>
- **Tech Stack**: <languages, frameworks>
- **Global Constraints**: <giới hạn toàn cục>
```

### File Structure
Map files trước khi viết tasks:
```
src/module/file.py     — responsibility
tests/test_file.py      — tests
```

### Task Format
```markdown
## Task N: <mô tả>

**Files**: Create/Modify/Test
- `path/to/file.py`

**Interfaces**:
- Consumes: <dependencies>
- Produces: <output interface>

**Steps**:
- [ ] 1. Write failing test: `test_<behavior>()`
  ```python
  def test_<behavior>():
      assert ...
  ```
- [ ] 2. Verify test fails correctly
- [ ] 3. Implement minimal code
  ```python
  def <function>():
      return ...
  ```
- [ ] 4. Verify test passes + all other tests green
- [ ] 5. Commit: `feat: <message>`
```

## Critical Prohibitions (Plan Failures)

- TBD, TODO, "implement later", "fill in details"
- Vague directives: "add appropriate error handling"
- "Similar to Task N" (luôn repeat content đầy đủ)
- Steps mô tả WHAT không SHOW HOW
- Reference undefined types hoặc functions

## Process Flow

1. Announce: "I'm using the writing-plans skill"
2. Scope check — split multi-subsystem specs
3. Map file structure
4. Viết tasks đầy đủ detail
5. Self-review: spec coverage, placeholders, type consistency
6. Save → `docs/superpowers/plans/YYYY-MM-DD-<feature-name>.md`
7. Offer execution choice: Subagent-Driven (recommended) hoặc Inline

## Integration với He Thong Agent

Plan output map vào:
- `planner_task_graph` node: tạo task graph + subtasks
- `plan_arbiter` node: finalPlan.workerTaskSpec
- `agent_loop.py` `Plan` model: structured plan items
- SQLite broker: dispatch subtasks
