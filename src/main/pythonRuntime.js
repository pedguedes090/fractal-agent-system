const fs = require("fs");
const path = require("path");

function getProjectRoot() {
  return path.resolve(__dirname, "../..");
}

function venvPythonPath(projectRoot = getProjectRoot()) {
  return process.platform === "win32"
    ? path.join(projectRoot, ".venv", "Scripts", "python.exe")
    : path.join(projectRoot, ".venv", "bin", "python");
}

function resolvePythonCommand(projectRoot = getProjectRoot()) {
  const explicit = String(process.env.AGENT_PYTHON || process.env.PYTHON || "").trim();
  if (explicit) return { command: explicit, args: [] };

  const local = venvPythonPath(projectRoot);
  if (fs.existsSync(local)) return { command: local, args: [] };

  if (process.platform === "win32") {
    return { command: "py", args: ["-3"] };
  }
  return { command: "python3", args: [] };
}

function buildPythonEnv({ projectRoot = getProjectRoot(), userDataPath, fullPower } = {}) {
  const enginePath = path.join(projectRoot, "engine");
  const localBinPath = path.join(projectRoot, "node_modules", ".bin");
  const nextPath = process.env.PATH
    ? `${localBinPath}${path.delimiter}${process.env.PATH}`
    : localBinPath;

  const env = {
    ...process.env,
    PATH: nextPath,
    AGENT_ENGINE_STATE_DIR: userDataPath || process.env.AGENT_ENGINE_STATE_DIR || path.join(projectRoot, ".agent-state"),
    CODEGRAPH_TELEMETRY: "0",
    LANGGRAPH_STRICT_MSGPACK: "true",
    OPENHANDS_SUPPRESS_BANNER: "1",
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    PYTHONPATH: process.env.PYTHONPATH ? `${enginePath}${path.delimiter}${process.env.PYTHONPATH}` : enginePath
  };
  // DANGEROUS — when fullPower.bypassSafeCommands is on, the agent can run
  // arbitrary shell commands in your workspace. The Python side reads this
  // env var in workspace.py:682 + :848 to skip both setup and verification
  // allowlists. Keep this OFF unless the user explicitly opted in via the
  // Full Power panel in the Settings tab.
  if (fullPower?.bypassSafeCommands) {
    env.AGENT_BYPASS_SAFE_COMMANDS = "1";
  }
  return env;
}

module.exports = {
  buildPythonEnv,
  getProjectRoot,
  resolvePythonCommand,
  venvPythonPath
};
