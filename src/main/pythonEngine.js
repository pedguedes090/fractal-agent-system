const path = require("path");
const { spawn } = require("child_process");

function getProjectRoot() {
  return path.resolve(__dirname, "../..");
}

function getPythonPath(projectRoot) {
  const venvPython = path.join(projectRoot, ".venv", "Scripts", "python.exe");
  return venvPython;
}

function parseJsonLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

function runPythonAgentPipeline({ settings, workspacePath, messages, userText, sessionId, emitProgress }) {
  return new Promise((resolve, reject) => {
    const projectRoot = getProjectRoot();
    const pythonPath = getPythonPath(projectRoot);
    const enginePath = path.join(projectRoot, "engine");
    const localBinPath = path.join(projectRoot, "node_modules", ".bin");
    const nextPath = process.env.PATH
      ? `${localBinPath}${path.delimiter}${process.env.PATH}`
      : localBinPath;
    const child = spawn(pythonPath, ["-m", "agent_engine.run"], {
      cwd: projectRoot,
      windowsHide: true,
      env: {
        ...process.env,
        PATH: nextPath,
        CODEGRAPH_TELEMETRY: "0",
        OPENHANDS_SUPPRESS_BANNER: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
        PYTHONPATH: process.env.PYTHONPATH ? `${enginePath}${path.delimiter}${process.env.PYTHONPATH}` : enginePath
      }
    });

    let stdoutBuffer = "";
    let stderr = "";
    let result = null;
    let engineError = null;

    child.stdout.on("data", (chunk) => {
      stdoutBuffer += chunk.toString("utf8");
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.trim()) continue;
        const message = parseJsonLine(line);
        if (!message) continue;
        if (message.type === "progress" && typeof emitProgress === "function") {
          emitProgress({
            stage: message.stage,
            detail: message.detail,
            at: message.at
          });
        }
        if (message.type === "result") result = message.result;
        if (message.type === "error") engineError = message.error;
      }
    });

    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
      if (stderr.length > 20000) stderr = stderr.slice(-20000);
    });

    child.on("error", (error) => {
      reject(error);
    });

    child.on("close", (code) => {
      if (stdoutBuffer.trim()) {
        const message = parseJsonLine(stdoutBuffer.trim());
        if (message?.type === "result") result = message.result;
        if (message?.type === "error") engineError = message.error;
      }

      if (result) {
        resolve({
          ...result,
          id: result.id,
          createdAt: new Date().toISOString(),
          workspacePath,
          settings: {
            serverUrl: settings.serverUrl,
            model: settings.model
          }
        });
        return;
      }

      reject(new Error(engineError || stderr || `Python engine exited with code ${code}`));
    });

    child.stdin.write(
      JSON.stringify({
        sessionId,
        content: userText,
        workspacePath,
        settings,
        messages
      })
    );
    child.stdin.end();
  });
}

module.exports = {
  runPythonAgentPipeline
};
