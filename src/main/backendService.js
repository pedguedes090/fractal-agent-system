const { spawn } = require("child_process");
const crypto = require("crypto");
const { buildPythonEnv, getProjectRoot, resolvePythonCommand } = require("./pythonRuntime");

function parseJsonLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

class AgentBackendService {
  constructor(userDataPath) {
    this.userDataPath = userDataPath;
    this.child = null;
    this.endpoint = null;
    this.stderr = "";
    this.readyPromise = null;
  }

  start() {
    if (this.readyPromise) return this.readyPromise;

    this.readyPromise = new Promise((resolve, reject) => {
      const projectRoot = getProjectRoot();
      const python = resolvePythonCommand(projectRoot);
      const child = spawn(
        python.command,
        [...python.args, "-m", "agent_engine.server", "--host", "127.0.0.1", "--port", "0"],
        {
          cwd: projectRoot,
          windowsHide: true,
          env: buildPythonEnv({ projectRoot, userDataPath: this.userDataPath })
        }
      );

      this.child = child;
      let stdoutBuffer = "";
      let settled = false;

      const fail = (error) => {
        if (settled) return;
        settled = true;
        if (this.child === child) {
          this.child = null;
          this.endpoint = null;
          this.readyPromise = null;
        }
        reject(error instanceof Error ? error : new Error(String(error)));
      };

      child.stdout.on("data", (chunk) => {
        stdoutBuffer += chunk.toString("utf8");
        const lines = stdoutBuffer.split(/\r?\n/);
        stdoutBuffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          const message = parseJsonLine(line);
          if (message?.type === "ready" && message.host && message.port) {
            this.endpoint = `http://${message.host}:${message.port}`;
            settled = true;
            resolve(this.endpoint);
          }
        }
      });

      child.stderr.on("data", (chunk) => {
        this.stderr += chunk.toString("utf8");
        if (this.stderr.length > 20000) this.stderr = this.stderr.slice(-20000);
      });

      child.on("error", fail);
      child.on("exit", (code) => {
        if (this.child === child) {
          this.child = null;
          this.endpoint = null;
          this.readyPromise = null;
        }
        if (!settled) {
          fail(new Error(this.stderr || `Agent backend exited before ready with code ${code}`));
        }
      });
    });

    return this.readyPromise;
  }

  async runPipeline({ settings, workspacePath, messages, userText, sessionId, humanGateApproval, emitProgress }) {
    const correlationId = humanGateApproval?.correlationId || crypto.randomUUID();
    const executionId = humanGateApproval?.executionId || crypto.randomUUID();
    const maxTransportRetries = 1;
    let lastError = null;

    for (let attempt = 0; attempt <= maxTransportRetries; attempt += 1) {
      try {
        return await this.requestPipeline({
          settings,
          workspacePath,
          messages,
          userText,
          sessionId,
          humanGateApproval,
          emitProgress,
          correlationId,
          executionId
        });
      } catch (error) {
        lastError = error;
        const retryable = /fetch failed|socket|connection|terminated|econn|backend exited|khong tra ve ket qua/i.test(String(error?.message || error));
        if (!retryable || attempt >= maxTransportRetries) throw error;
        if (typeof emitProgress === "function") {
          emitProgress({
            stage: "resume",
            detail: `Backend connection interrupted; resuming execution ${executionId}`,
            at: new Date().toISOString()
          });
        }
        this.stop();
        await new Promise((resolve) => setTimeout(resolve, 350));
      }
    }
    throw lastError || new Error("Agent backend transport retry failed.");
  }

  async requestPipeline({
    settings,
    workspacePath,
    messages,
    userText,
    sessionId,
    humanGateApproval,
    emitProgress,
    correlationId,
    executionId
  }) {
    const endpoint = await this.start();
    const response = await fetch(`${endpoint}/v1/runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Correlation-Id": correlationId
      },
      body: JSON.stringify({
        sessionId,
        executionId,
        correlationId,
        content: userText,
        workspacePath,
        settings,
        messages,
        humanGateApproval
      })
    });

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Agent backend loi ${response.status}: ${body.slice(0, 800)}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    let engineError = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const message = parseJsonLine(line);
        if (!message) continue;
        if (message.type === "progress" && typeof emitProgress === "function") {
          emitProgress({
            stage: message.stage,
            detail: message.detail,
            at: message.at,
            executionId: message.executionId || executionId,
            correlationId: message.correlationId || correlationId,
            sessionId: message.sessionId || sessionId,
            node: message.node || null,
            eventType: message.eventType || "progress"
          });
        }
        if (message.type === "result") result = message.result;
        if (message.type === "error") engineError = message.error;
      }
    }

    if (buffer.trim()) {
      const message = parseJsonLine(buffer.trim());
      if (message?.type === "result") result = message.result;
      if (message?.type === "error") engineError = message.error;
    }

    if (!result) throw new Error(engineError || "Agent backend khong tra ve ket qua.");
    return {
      ...result,
      id: result.id,
      executionId: result.executionId || executionId,
      correlationId: result.correlationId || correlationId,
      createdAt: new Date().toISOString(),
      workspacePath,
      settings: {
        serverUrl: settings.serverUrl,
        model: settings.model
      }
    };
  }

  async runDoctor({ workspacePath, sessionId, apiKey, model, emitEvent }) {
    const endpoint = await this.start();
    const response = await fetch(`${endpoint}/v1/doctor`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspacePath, sessionId, apiKey, model })
    });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Doctor backend loi ${response.status}: ${body.slice(0, 800)}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;
    let engineError = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const message = parseJsonLine(line);
        if (!message) continue;
        if (typeof emitEvent === "function") emitEvent(message);
        if (message.type === "doctor.result") {
          finalResult = message.result || null;
          if (message.error) engineError = message.error;
        }
      }
    }
    if (buffer.trim()) {
      const message = parseJsonLine(buffer.trim());
      if (message && typeof emitEvent === "function") emitEvent(message);
      if (message?.type === "doctor.result") {
        finalResult = message.result || finalResult;
        if (message.error) engineError = message.error;
      }
    }
    if (!finalResult) throw new Error(engineError || "Doctor khong tra ve ket qua.");
    return finalResult;
  }

  async getObservability() {
    const endpoint = await this.start();
    const response = await fetch(`${endpoint}/v1/observability`);
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Agent backend observability loi ${response.status}: ${body.slice(0, 800)}`);
    }
    return response.json();
  }

  async getAutonomyStatus() {
    const endpoint = await this.start();
    const response = await fetch(`${endpoint}/v1/autonomy/status`);
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Agent backend autonomy status loi ${response.status}: ${body.slice(0, 800)}`);
    }
    return response.json();
  }

  async getTopology() {
    const endpoint = await this.start();
    const response = await fetch(`${endpoint}/v1/topology`);
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Agent backend topology loi ${response.status}: ${body.slice(0, 800)}`);
    }
    return response.json();
  }

  async cancelRun(executionId) {
    if (!executionId) return { ok: false, error: "no executionId" };
    const endpoint = await this.start();
    const response = await fetch(`${endpoint}/v1/runs/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ executionId }),
    });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      return { ok: false, error: `cancel ${response.status}: ${body.slice(0, 300)}` };
    }
    return response.json();
  }

  async runAutonomyScan({ workspacePath }) {
    const endpoint = await this.start();
    const correlationId = crypto.randomUUID();
    const response = await fetch(`${endpoint}/v1/autonomy/idle-scan`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Correlation-Id": correlationId
      },
      body: JSON.stringify({
        workspacePath,
        correlationId
      })
    });
    const body = await response.text().catch(() => "");
    let payload = null;
    try {
      payload = body ? JSON.parse(body) : null;
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const message = payload?.error || body.slice(0, 800) || "unknown";
      throw new Error(`Agent backend autonomy scan loi ${response.status}: ${message}`);
    }
    return payload;
  }

  stop() {
    if (!this.child) return;
    this.child.kill();
    this.child = null;
    this.endpoint = null;
    this.readyPromise = null;
  }
}

module.exports = {
  AgentBackendService
};
