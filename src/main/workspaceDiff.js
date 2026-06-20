const fs = require("fs/promises");
const path = require("path");
const { execFile } = require("child_process");
const { promisify } = require("util");

const execFileAsync = promisify(execFile);
const MAX_DIFF_BYTES = 512 * 1024;

function resolveWorkspaceFile(workspacePath, relativePath) {
  const root = path.resolve(String(workspacePath || ""));
  const relative = String(relativePath || "").replace(/\\/g, "/");
  if (!workspacePath || !relative || path.isAbsolute(relative) || relative.split("/").includes("..")) {
    throw new Error("Đường dẫn file không hợp lệ.");
  }
  const target = path.resolve(root, relative);
  if (target === root || !target.startsWith(`${root}${path.sep}`)) {
    throw new Error("File nằm ngoài workspace.");
  }
  return { root, relative, target };
}

async function readTextPreview(target, relative) {
  let stat;
  try {
    stat = await fs.stat(target);
  } catch {
    return { kind: "message", message: "File không còn tồn tại trong workspace." };
  }
  if (!stat.isFile()) return { kind: "message", message: "Đường dẫn này không phải file." };
  if (stat.size > MAX_DIFF_BYTES) {
    return { kind: "message", message: `File quá lớn để xem (${stat.size} bytes).` };
  }
  const buffer = await fs.readFile(target);
  if (buffer.includes(0)) return { kind: "message", message: "File nhị phân không có bản xem trước dạng text." };
  const content = buffer.toString("utf8");
  return {
    kind: "content",
    content: `Nội dung hiện tại (git chưa có patch cho file này):\n\n${content}`,
    path: relative
  };
}

async function getWorkspaceDiff(workspacePath, relativePath, status = "changed") {
  const { root, relative, target } = resolveWorkspaceFile(workspacePath, relativePath);
  try {
    const { stdout } = await execFileAsync(
      "git",
      ["-c", "core.quotepath=false", "diff", "--no-ext-diff", "--no-color", "--", relative],
      { cwd: root, windowsHide: true, maxBuffer: MAX_DIFF_BYTES }
    );
    if (stdout.trim()) return { kind: "diff", diff: stdout, path: relative };
  } catch {
    // Non-git workspaces and oversized patches fall back to a bounded preview.
  }

  if (String(status).toLowerCase() === "deleted") {
    return { kind: "message", message: "File đã bị xóa và git không còn patch để hiển thị.", path: relative };
  }
  return readTextPreview(target, relative);
}

module.exports = { getWorkspaceDiff, resolveWorkspaceFile };
