const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const test = require("node:test");

const { getWorkspaceDiff, resolveWorkspaceFile } = require("../src/main/workspaceDiff");

test("workspace diff rejects path traversal", () => {
  assert.throws(() => resolveWorkspaceFile("C:\\workspace", "../secret.txt"), /không hợp lệ|ngoài workspace/i);
});

test("workspace diff previews a newly created non-git file", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-diff-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(root, "new.txt"), "hello\n", "utf8");

  const result = await getWorkspaceDiff(root, "new.txt", "created");

  assert.equal(result.kind, "content");
  assert.match(result.content, /hello/);
});

test("workspace diff returns the current git patch", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-git-diff-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  execFileSync("git", ["init"], { cwd: root, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: root });
  execFileSync("git", ["config", "user.name", "Test"], { cwd: root });
  fs.writeFileSync(path.join(root, "app.txt"), "before\n", "utf8");
  execFileSync("git", ["add", "app.txt"], { cwd: root });
  execFileSync("git", ["commit", "-m", "initial"], { cwd: root, stdio: "ignore" });
  fs.writeFileSync(path.join(root, "app.txt"), "after\n", "utf8");

  const result = await getWorkspaceDiff(root, "app.txt", "modified");

  assert.equal(result.kind, "diff");
  assert.match(result.diff, /-before/);
  assert.match(result.diff, /\+after/);
});
