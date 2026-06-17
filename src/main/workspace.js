const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

const TEXT_EXTENSIONS = new Set([
  ".c",
  ".cc",
  ".cpp",
  ".cs",
  ".css",
  ".go",
  ".h",
  ".html",
  ".java",
  ".js",
  ".jsx",
  ".json",
  ".md",
  ".mjs",
  ".py",
  ".rb",
  ".rs",
  ".scss",
  ".sh",
  ".sql",
  ".svelte",
  ".toml",
  ".ts",
  ".tsx",
  ".txt",
  ".vue",
  ".xml",
  ".yaml",
  ".yml"
]);

const IGNORED_DIRS = new Set([
  ".git",
  ".next",
  ".nuxt",
  ".venv",
  "build",
  "coverage",
  "dist",
  "node_modules",
  "out",
  "target",
  "vendor"
]);

function normalizeRelativePath(relativePath) {
  return String(relativePath || "").replace(/\\/g, "/").replace(/^\/+/, "");
}

function resolveInsideWorkspace(workspacePath, relativePath) {
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized || normalized.includes("\0")) {
    throw new Error("Duong dan file khong hop le.");
  }
  const absolute = path.resolve(workspacePath, normalized);
  const root = path.resolve(workspacePath);
  const relative = path.relative(root, absolute);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`File nam ngoai workspace: ${relativePath}`);
  }
  return absolute;
}

function isProbablyText(filePath) {
  const extension = path.extname(filePath).toLowerCase();
  return TEXT_EXTENSIONS.has(extension);
}

function walkWorkspace(workspacePath, options = {}) {
  const maxFiles = options.maxFiles || 160;
  const maxDepth = options.maxDepth || 5;
  const files = [];

  function walk(currentPath, depth) {
    if (files.length >= maxFiles || depth > maxDepth) return;
    let entries = [];
    try {
      entries = fs.readdirSync(currentPath, { withFileTypes: true });
    } catch {
      return;
    }

    entries.sort((a, b) => a.name.localeCompare(b.name));

    for (const entry of entries) {
      if (files.length >= maxFiles) return;
      const absolute = path.join(currentPath, entry.name);
      const relative = normalizeRelativePath(path.relative(workspacePath, absolute));

      if (entry.isDirectory()) {
        if (!IGNORED_DIRS.has(entry.name)) walk(absolute, depth + 1);
        continue;
      }

      if (!entry.isFile()) continue;

      let size = 0;
      try {
        size = fs.statSync(absolute).size;
      } catch {
        size = 0;
      }

      files.push({
        path: relative,
        size,
        text: isProbablyText(absolute)
      });
    }
  }

  walk(path.resolve(workspacePath), 0);
  return files;
}

function readWorkspaceFile(workspacePath, relativePath, maxBytes = 200000) {
  const absolute = resolveInsideWorkspace(workspacePath, relativePath);
  if (!fs.existsSync(absolute)) {
    throw new Error(`Khong tim thay file: ${relativePath}`);
  }
  const stat = fs.statSync(absolute);
  if (stat.size > maxBytes) {
    throw new Error(`File qua lon de doc truc tiep: ${relativePath}`);
  }
  return fs.readFileSync(absolute, "utf8");
}

function writeWorkspaceFile(workspacePath, relativePath, content) {
  const absolute = resolveInsideWorkspace(workspacePath, relativePath);
  fs.mkdirSync(path.dirname(absolute), { recursive: true });
  fs.writeFileSync(absolute, String(content ?? ""), "utf8");
}

function getRepoSnapshot(workspacePath) {
  const files = walkWorkspace(workspacePath);
  const packageJson = files.find((file) => file.path === "package.json");
  const pyproject = files.find((file) => file.path === "pyproject.toml");
  const requirements = files.find((file) => file.path === "requirements.txt");
  const readme = files.find((file) => /^readme\.md$/i.test(file.path));

  let packageInfo = null;
  if (packageJson) {
    try {
      packageInfo = JSON.parse(readWorkspaceFile(workspacePath, "package.json", 100000));
    } catch {
      packageInfo = null;
    }
  }

  return {
    workspacePath,
    files,
    hints: {
      hasPackageJson: Boolean(packageJson),
      hasPyproject: Boolean(pyproject),
      hasRequirements: Boolean(requirements),
      hasReadme: Boolean(readme)
    },
    packageInfo: packageInfo
      ? {
          name: packageInfo.name,
          scripts: packageInfo.scripts || {},
          dependencies: Object.keys(packageInfo.dependencies || {}),
          devDependencies: Object.keys(packageInfo.devDependencies || {})
        }
      : null
  };
}

function runCommand(workspacePath, command, timeoutMs = 120000) {
  return new Promise((resolve) => {
    const child = spawn(command, {
      cwd: workspacePath,
      shell: true,
      windowsHide: true
    });

    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      resolve({
        command,
        code: null,
        timedOut: true,
        stdout,
        stderr
      });
    }, timeoutMs);

    child.stdout.on("data", (data) => {
      stdout += data.toString();
      if (stdout.length > 50000) stdout = stdout.slice(-50000);
    });
    child.stderr.on("data", (data) => {
      stderr += data.toString();
      if (stderr.length > 50000) stderr = stderr.slice(-50000);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({
        command,
        code,
        timedOut: false,
        stdout,
        stderr
      });
    });
  });
}

function isSafeCommand(command) {
  const trimmed = String(command || "").trim();
  if (!trimmed) return false;
  const lower = trimmed.toLowerCase();
  if (/[;&|<>]/.test(trimmed)) return false;
  const forbidden = [
    " rm ",
    " del ",
    " rmdir ",
    " remove-item",
    " format ",
    " shutdown",
    " reboot",
    " reg ",
    " set-executionpolicy",
    " curl ",
    " wget "
  ];
  if (forbidden.some((word) => ` ${lower} `.includes(word))) return false;
  if (lower.startsWith("git ")) {
    return /^(git status|git diff|git log|git rev-parse)\b/.test(lower);
  }
  if (/^(npm|pnpm|yarn)\b/.test(lower)) {
    return /^(npm|pnpm|yarn) (test|run (check|test|lint|build|typecheck|verify))\b/.test(lower);
  }
  if (lower.startsWith("node ")) {
    return /^node --check\b/.test(lower);
  }
  if (/^(python|py)\b/.test(lower)) {
    return /^(python|py) -m (pytest|compileall)\b/.test(lower);
  }
  return /^(pytest|go test|cargo test|dotnet test|mvn test|gradle test)\b/.test(lower);
}

module.exports = {
  getRepoSnapshot,
  isSafeCommand,
  normalizeRelativePath,
  readWorkspaceFile,
  resolveInsideWorkspace,
  runCommand,
  walkWorkspace,
  writeWorkspaceFile
};
