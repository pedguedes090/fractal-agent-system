// ── API layer ──────────────────────────────────────────────────────────
// Uses preload bridge when running inside Electron; falls back to
// fetch-based stubs for dev / browser load.
const appApi = window.agentApp || (() => {
  // Detect if a Python backend is reachable on the same host
  let _backendBase = "";
  async function _resolveBase() {
    if (_backendBase) return _backendBase;
    const ports = []; // will be discovered
    // Try common ports
    const candidates = [
      window.location.port ? `http://127.0.0.1:${window.location.port}` : null,
      "http://127.0.0.1:20128",
    ].filter(Boolean);
    for (const base of candidates) {
      try {
        const r = await fetch(`${base}/health`, { signal: AbortSignal.timeout(1200) });
        if (r.ok) { _backendBase = base; return base; }
      } catch {}
    }
    _backendBase = candidates[0]; // best guess
    return _backendBase;
  }
  return {
    async getInitialState() {
      try {
        const base = await _resolveBase();
        const r = await fetch(`${base}/v1/observability`, { signal: AbortSignal.timeout(3000) });
        const d = await r.json();
        return {
          settings: {
            serverUrl: "http://localhost:20128/v1", model: "gemini/gemini-3.1-flash-lite",
            apiKey: "", autoConfirmHumanGate: false, directWorkspaceMode: true,
            modelOverrides: {}, bypassPolicy: false
          },
          sessions: { sessions: [] },
          activeSession: null,
          _backend: d
        };
      } catch(e) {
        return { settings: {}, sessions: { sessions: [] }, activeSession: null, _initError: e.message };
      }
    },
    async saveSettings(s) { return s; },
    async chooseWorkspace() { return prompt("Nhập đường dẫn workspace:"); },
    async listSessions() { return { sessions: [] }; },
    async createSession() {
      return { session: { id: "web-session", title: "Browser", workspacePath: "", messages: [], runs: [] }, sessions: { sessions: [] } };
    },
    async loadSession() { return null; },
    async updateSessionWorkspace(id, ws) {
      return { session: { id, workspacePath: ws, messages: [], runs: [] }, sessions: { sessions: [] } };
    },
    async deleteSession() { return { sessions: [] }; },
    async sendMessage(payload) {
      const base = await _resolveBase();
      const controller = new AbortController();
      window.__currentSendController = controller;
      const r = await fetch(`${base}/v1/runs`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload), signal: controller.signal,
      });
      if (!r.ok) { const t = await r.text().catch(() => ""); throw new Error(`Backend ${r.status}: ${t.slice(0, 500)}`); }
      // Server now emits X-Execution-Id header AND a first {type:"ready",executionId} line.
      window.__currentExecutionId = r.headers.get("X-Execution-Id") || null;
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "", result = null;
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split(/\r?\n/); buf = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const m = JSON.parse(line);
            if (m.type === "ready" && m.executionId) window.__currentExecutionId = m.executionId;
            if (m.type === "progress") { window.dispatchEvent(new CustomEvent("agent:progress", { detail: m })); }
            if (m.type === "result") result = m.result;
          } catch(e) {}
        }
      }
      if (buf.trim()) { try { const m = JSON.parse(buf.trim()); if (m.type === "result") result = m.result; } catch(e) {} }
      if (!result) throw new Error("Backend returned no result");
      return { ...result, id: result.id || "web-run", executionId: result.executionId || result.id };
    },
    async cancelRun(executionId) {
      const base = await _resolveBase();
      const r = await fetch(`${base}/v1/runs/cancel`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ executionId }), signal: AbortSignal.timeout(5000),
      });
      return r.json().catch(() => ({ ok: r.ok }));
    },
    async getObservability() {
      const base = await _resolveBase();
      const r = await fetch(`${base}/v1/observability`, { signal: AbortSignal.timeout(5000) });
      return r.json();
    },
    async getAutonomyStatus() {
      const base = await _resolveBase();
      const r = await fetch(`${base}/v1/autonomy/status`, { signal: AbortSignal.timeout(5000) });
      return r.json();
    },
    async runAutonomyScan(p) {
      const base = await _resolveBase();
      const r = await fetch(`${base}/v1/autonomy/idle-scan`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(p), signal: AbortSignal.timeout(300_000)
      });
      return r.json();
    },
    async getTopology() {
      const base = await _resolveBase();
      const r = await fetch(`${base}/v1/topology`, { signal: AbortSignal.timeout(5000) });
      if (!r.ok) throw new Error(`topology ${r.status}`);
      return r.json();
    },
    onProgress(cb) {
      const h = (e) => cb(e.detail);
      window.addEventListener("agent:progress", h);
      return () => window.removeEventListener("agent:progress", h);
    }
  };
})();

// ── Helpers ────────────────────────────────────────────────────────────
function safeQuery(sel) { return document.querySelector(sel); }
function safeQueryAll(sel) { return document.querySelectorAll(sel); }

const state = {
  settings: null,
  sessions: [],
  activeSession: null,
  progress: [],
  observability: null,
  autonomy: null,
  autonomyScanning: false,
  autonomyAutoScanScheduled: false,
  running: false,
  activeTask: ""
};

const WORKFLOW_GROUPS = [
  {
    title: "Intake",
    stages: ["preflight", "task_intent", "actr_memory", "codegraph_context", "intake_user_intent", "intake_ambiguity", "intake_repo_context", "intake_synthesizer"]
  },
  {
    title: "Planning",
    stages: ["planning_minimal", "planning_robust", "planning_test_first", "critique_risk", "critique_test_coverage", "critique_security_regression", "plan_arbiter"]
  },
  {
    title: "Governance",
    stages: ["planner_task_graph", "researcher_context_agent", "governance_service", "human_gate", "environment_gate"]
  },
  {
    title: "Execution",
    stages: ["workspace_mode", "load_context_files", "setup_commands", "openhands_worker", "tester_agent"]
  },
  {
    title: "Review",
    stages: ["security_reviewer_agent", "code_reviewer_agent", "release_deploy_agent", "reviewer_decision", "execution_gate"]
  },
  {
    title: "Release",
    stages: ["reporter", "finalize_workspace", "reporter_end"]
  }
];

const STAGE_LABELS = {
  preflight: "Preflight",
  task_intent: "Task intent",
  actr_memory: "ACT-R memory",
  codegraph_context: "CodeGraph",
  codegraph_affected: "Affected tests",
  intake_user_intent: "User intent",
  intake_ambiguity: "Ambiguity",
  intake_repo_context: "Repo context",
  intake_synthesizer: "Synthesis",
  planning_minimal: "Plan: minimal",
  planning_robust: "Plan: robust",
  planning_test_first: "Plan: test-first",
  critique_risk: "Risk critique",
  critique_test_coverage: "Test critique",
  critique_security_regression: "Security critique",
  plan_arbiter: "Plan arbiter",
  planner_task_graph: "Task graph",
  researcher_context_agent: "Researcher",
  governance_service: "Governance",
  human_gate: "Human gate",
  environment_gate: "Environment gate",
  workspace_mode: "Workspace mode",
  load_context_files: "Context files",
  context: "Context",
  setup_commands: "Setup & install",
  openhands_worker: "OpenHands coder",
  openhands_context: "OpenHands context",
  openhands_plugins: "Plugins",
  openhands_mcp: "MCP",
  openhands_message: "OpenHands message",
  openhands_action: "OpenHands action",
  openhands_observation: "OpenHands observation",
  automated_review: "Automated review",
  tester_agent: "Tester",
  security_reviewer_agent: "Security reviewer",
  code_reviewer_agent: "Code reviewer",
  release_deploy_agent: "Release plan",
  reviewer_decision: "Review decision",
  execution_gate: "Execution gate",
  queued: "Queued",
  running: "Running",
  resume: "Resume",
  reporter: "Reporter",
  finalize_workspace: "Final merge",
  reporter_end: "Complete",
  done: "Done",
  error: "Error"
};

// Lazy element lookup — DOM may not be ready at script parse time
function _$(sel) {
  const el = document.querySelector(sel);
  if (!el) console.warn("[app] Element not found:", sel);
  return el;
}
const E = {
  get workspaceLabel() { return _$("#workspaceLabel"); },
  get chooseFolderBtn() { return _$("#chooseFolderBtn"); },
  get sessionSelect() { return _$("#sessionSelect"); },
  get newSessionBtn() { return _$("#newSessionBtn"); },
  get settingsToggleBtn() { return _$("#settingsToggleBtn"); },
  get serverInput() { return _$("#serverInput"); },
  get modelInput() { return _$("#modelInput"); },
  get apiKeyInput() { return _$("#apiKeyInput"); },
  get autoConfirmInput() { return _$("#autoConfirmInput"); },
  get directWorkspaceInput() { return _$("#directWorkspaceInput"); },
  get bypassPolicyInput() { return _$("#bypassPolicyInput"); },
  get plannerModelInput() { return _$("#plannerModelInput"); },
  get coderModelInput() { return _$("#coderModelInput"); },
  get reviewerModelInput() { return _$("#reviewerModelInput"); },
  get saveSettingsBtn() { return _$("#saveSettingsBtn"); },
  get saveStatus() { return _$("#saveStatus"); },
  get messageInput() { return _$("#messageInput"); },
  get sendBtn() { return _$("#sendBtn"); },
  get dashboardMetrics() { return _$("#dashboardMetrics"); },
  get dagBoard() { return _$("#dagBoard"); },
  get dagBoardFull() { return _$("#dagBoardFull"); },
  get nodeDetail() { return _$("#nodeDetail"); },
  get flowCanvas() { return _$("#flowCanvas"); },
  get flowDetail() { return _$("#flowDetail"); },
  get flowIOPanel() { return _$("#flowIOPanel"); },
  get runSummary() { return _$("#ovResult"); },
  get artifactList() { return _$("#changesFileList"); },
  get runState() { return _$("#ovStatus"); },
  get refreshObservabilityBtn() { return _$("#refreshObservabilityBtn"); },
  get autonomyScanBtn() { return _$("#autonomyScanBtn"); },
  get autonomyStatus() { return _$("#autonomyStatus"); },
  get autonomySummary() { return _$("#autonomySummary"); },
  get execStream() { return _$("#execStream"); },
  get execProgress() { return _$("#execProgress"); },
  get reviewContent() { return _$("#reviewContent"); },
  get changesDiffViewer() { return _$("#changesDiffViewer"); },
  get logFilterNode() { return _$("#logFilterNode"); },
  get logFilterType() { return _$("#logFilterType"); },
  get logToggleDebug() { return _$("#logToggleDebug"); },
};
// Expose for debugging
window.E = E;

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit"
  });
}

function compactText(value, limit = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function getWorkspacePath() {
  return state.activeSession?.workspacePath || "";
}

function latestRun() {
  const runs = Array.isArray(state.activeSession?.runs) ? state.activeSession.runs : [];
  return runs.length ? runs[runs.length - 1] : null;
}

function latestAutonomyReport() {
  return state.autonomy?.report || state.autonomy?.lastReport || null;
}

function autonomyWorkspaceMatches() {
  const report = latestAutonomyReport();
  return Boolean(report?.workspacePath && getWorkspacePath() && report.workspacePath === getWorkspacePath());
}

function timeline() {
  const run = latestRun();
  const persisted = Array.isArray(run?.progressEvents) ? run.progressEvents : [];
  return state.running || state.progress.length ? state.progress : persisted;
}

function stageLabel(stage) {
  return STAGE_LABELS[stage] || String(stage || "Step").replaceAll("_", " ");
}

function runStatus(run = latestRun()) {
  if (state.running) return "running";
  if (!run) return "idle";
  if (run.error) return "error";
  if (run.humanGate?.status === "pending") return "waiting";
  const blockers = run.review?.blockers || [];
  if (Array.isArray(blockers) && blockers.length) return "blocked";
  return "completed";
}

function statusLabel(status) {
  const labels = {
    idle: "Sẵn sàng",
    running: "Đang chạy",
    waiting: "Chờ phê duyệt",
    blocked: "Bị chặn",
    error: "Lỗi",
    completed: "Hoàn tất"
  };
  return labels[status] || status;
}

function timelineIndex() {
  const events = timeline();
  const byStage = new Map();
  events.forEach((item, index) => {
    byStage.set(item.stage, { ...item, index });
  });
  return { events, byStage, latest: events[events.length - 1] || null };
}

function nodeStatus(stage, indexData) {
  const event = indexData.byStage.get(stage);
  if (!event) return "idle";
  if (stage === "error" || event.stage === "error") return "error";
  if (indexData.latest?.stage === stage && state.running) return "running";
  if (/approval|phê duyệt|pending|chờ/i.test(String(event.detail || ""))) return "waiting";
  return "done";
}

function renderSettings() {
  if (E.serverInput) E.serverInput.value = state.settings?.serverUrl || "";
  if (E.modelInput) E.modelInput.value = state.settings?.model || "";
  if (E.apiKeyInput) E.apiKeyInput.value = state.settings?.apiKey || "";
  if (E.autoConfirmInput) E.autoConfirmInput.checked = Boolean(state.settings?.autoConfirmHumanGate);
  if (E.directWorkspaceInput) E.directWorkspaceInput.checked = state.settings?.directWorkspaceMode !== false;
  if (E.bypassPolicyInput) E.bypassPolicyInput.checked = Boolean(state.settings?.bypassPolicy);
  if (E.plannerModelInput) E.plannerModelInput.value = state.settings?.modelOverrides?.planner || "";
  if (E.coderModelInput) E.coderModelInput.value = state.settings?.modelOverrides?.coder || "";
  if (E.reviewerModelInput) E.reviewerModelInput.value = state.settings?.modelOverrides?.reviewer || "";
}

function renderWorkspace() {
  if (!E.workspaceLabel) return;
  const workspacePath = getWorkspacePath();
  E.workspaceLabel.textContent = workspacePath || "Chưa mở thư mục";
}

function renderSessions() {
  if (!E.sessionSelect) return;
  E.sessionSelect.innerHTML = "";
  if (!state.sessions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Chưa có phiên";
    E.sessionSelect.appendChild(option);
    return;
  }

  for (const session of state.sessions) {
    const option = document.createElement("option");
    option.value = session.id;
    option.textContent = session.title || "Phiên mới";
    E.sessionSelect.appendChild(option);
  }

  E.sessionSelect.value = state.activeSession?.id || state.sessions[0]?.id || "";
}

function renderMetrics() {
  if (!E.dashboardMetrics) return;
  const run = latestRun();
  const status = runStatus(run);
  const events = timeline();
  const changedFiles = Array.isArray(run?.changedFiles) ? run.changedFiles : [];
  const metrics = [
    ["Trạng thái", statusLabel(status)],
    ["Node hiện tại", stageLabel(events[events.length - 1]?.stage || "idle")],
    ["Changed files", String(changedFiles.length)],
    ["Token usage", String(run?.tokenUsage || 0)]
  ];

  E.dashboardMetrics.innerHTML = "";
  for (const [label, value] of metrics) {
    const card = document.createElement("div");
    card.className = `metric-card metric-${status}`;
    const labelNode = document.createElement("div");
    labelNode.className = "metric-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "metric-value";
    valueNode.textContent = value;
    card.append(labelNode, valueNode);
    E.dashboardMetrics.appendChild(card);
  }
}

function renderDagBoard() {
  if (!E.dagBoard) return;
  const indexData = timelineIndex();
  E.dagBoard.innerHTML = "";
  for (const group of WORKFLOW_GROUPS) {
    const column = document.createElement("section");
    column.className = "dag-column";
    const heading = document.createElement("h3");
    heading.textContent = group.title;
    column.appendChild(heading);

    for (const stage of group.stages) {
      const event = indexData.byStage.get(stage);
      const status = nodeStatus(stage, indexData);
      const card = document.createElement("article");
      card.className = `dag-node ${status}`;
      const title = document.createElement("div");
      title.className = "dag-node-title";
      const dot = document.createElement("span");
      dot.className = "status-dot";
      const label = document.createElement("span");
      label.textContent = stageLabel(stage);
      title.append(dot, label);
      const detail = document.createElement("p");
      detail.textContent = event ? compactText(event.detail, 110) : "Chưa chạy";
      card.append(title, detail);
      column.appendChild(card);
    }

    E.dagBoard.appendChild(column);
  }
}

function renderRunSummary() {
  if (!E.runSummary) return;
  if (state.running) {
    E.runSummary.textContent = `Đang chạy: ${state.activeTask || "tác vụ hiện tại"}`;
    return;
  }
  const run = latestRun();
  E.runSummary.innerHTML = "";
  if (!run) {
    E.runSummary.textContent = "Chưa có run nào. Mở workspace rồi chạy một tác vụ để xem trạng thái.";
    return;
  }

  const rows = [
    ["Task", run.task || state.activeTask || state.activeSession?.title],
    ["Execution", run.executionId || run.id],
    ["Correlation", run.correlationId],
    ["Review", run.review?.passed === false ? "Không đạt" : run.review?.passed === true ? "Đạt" : "Không có"],
    ["Human gate", run.humanGate?.status || "Không"]
  ];
  for (const [label, value] of rows) {
    if (!value) continue;
    const row = document.createElement("div");
    row.className = "summary-row";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = compactText(value, 160);
    row.append(key, val);
    E.runSummary.appendChild(row);
  }

  if (run.assistantText) {
    const text = document.createElement("p");
    text.className = "summary-text";
    text.textContent = compactText(run.assistantText, 520);
    E.runSummary.appendChild(text);
  }
}

function renderFlowIO() {
  if (!E.flowIOPanel) return;
  const run = latestRun();
  if (!run) {
    E.flowIOPanel.innerHTML =
      `<p class="muted" style="padding:24px;text-align:center">Chạy một task ở tab "Tổng quan" để xem input → output ở đây.</p>`;
    return;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const taskInput = run.task || state.activeTask || state.activeSession?.title || "(không rõ)";
  const output = run.assistantText || run.review?.finalMessage || "(chưa có output)";
  const reviewPassed = run.review?.passed;
  const blockers = Array.isArray(run.review?.blockers) ? run.review.blockers : [];
  const warnings = Array.isArray(run.review?.warnings) ? run.review.warnings : [];
  const changedFiles = Array.isArray(run.changedFiles) ? run.changedFiles : [];
  const testsPassed = run.tester?.passed;
  const status = runStatus(run);

  const verdictPill = (() => {
    if (status === "running") return `<span class="io-pill io-pill-running">Đang chạy</span>`;
    if (status === "error")   return `<span class="io-pill io-pill-error">Lỗi</span>`;
    if (reviewPassed === true)  return `<span class="io-pill io-pill-pass">Review đạt</span>`;
    if (reviewPassed === false) return `<span class="io-pill io-pill-fail">Review không đạt</span>`;
    return `<span class="io-pill io-pill-idle">Chưa có verdict</span>`;
  })();

  const testPill =
    testsPassed === true  ? `<span class="io-pill io-pill-pass">Tests đạt</span>` :
    testsPassed === false ? `<span class="io-pill io-pill-fail">Tests fail</span>` :
                            `<span class="io-pill io-pill-idle">Tests N/A</span>`;

  const fileCount = changedFiles.length;
  const fileList = changedFiles.slice(0, 8).map((f) => {
    const path = typeof f === "string" ? f : (f.path || JSON.stringify(f));
    const stat = typeof f === "object" ? (f.status || "") : "";
    return `<li><code>${esc(path)}</code>${stat ? ` <span class="muted">(${esc(stat)})</span>` : ""}</li>`;
  }).join("");

  const issuesHtml = blockers.length || warnings.length
    ? `<div class="io-issues">
        ${blockers.length ? `<div class="io-issues-block"><strong>Blocker (${blockers.length})</strong><ul>${
          blockers.slice(0, 5).map((b) => `<li>${esc(b.detail || b.message || JSON.stringify(b))}</li>`).join("")
        }</ul></div>` : ""}
        ${warnings.length ? `<div class="io-issues-warn"><strong>Cảnh báo (${warnings.length})</strong><ul>${
          warnings.slice(0, 5).map((w) => `<li>${esc(w.detail || w.message || JSON.stringify(w))}</li>`).join("")
        }</ul></div>` : ""}
      </div>`
    : "";

  E.flowIOPanel.innerHTML = `
    <div class="io-panel-header">
      <h3 style="margin:0;font-size:14px">Kiểm chứng kết quả</h3>
      <div class="io-pills">${verdictPill} ${testPill}
        <span class="io-pill io-pill-info">${fileCount} file thay đổi</span>
      </div>
    </div>
    <div class="io-grid">
      <section class="io-col">
        <h4>Đầu vào (task của bạn)</h4>
        <pre class="io-content">${esc(taskInput)}</pre>
      </section>
      <section class="io-col">
        <h4>Đầu ra (Reporter)</h4>
        <pre class="io-content">${esc(output)}</pre>
      </section>
    </div>
    ${fileList ? `
      <div class="io-files">
        <h4>File đã sửa</h4>
        <ul>${fileList}</ul>
        ${fileCount > 8 ? `<p class="muted" style="font-size:11px">+${fileCount - 8} file khác</p>` : ""}
      </div>` : ""}
    ${issuesHtml}
    <p class="muted" style="font-size:11px;text-align:right;margin:6px 0 0">
      Execution: <code>${esc(run.executionId || run.id || "")}</code>
    </p>`;
}

function renderArtifacts() {
  if (!E.artifactList) return;
  const run = latestRun();
  E.artifactList.innerHTML = "";
  const changedFiles = Array.isArray(run?.changedFiles) ? run.changedFiles : [];
  const blockers = Array.isArray(run?.review?.blockers) ? run.review.blockers : [];
  const items = [
    ...changedFiles.map((file) => ({
      label: file.path || file,
      detail: file.status || "changed",
      type: "file"
    })),
    ...blockers.map((blocker) => ({
      label: blocker.agent || blocker.type || "blocker",
      detail: blocker.detail || blocker.message || JSON.stringify(blocker),
      type: "blocker"
    }))
  ];
  if (run?.humanGate?.status === "pending") {
    items.push({
      label: "human_gate",
      detail: run.humanGate.reason || "Cần xác nhận trước khi chạy tiếp",
      type: "blocker"
    });
  }
  if (!items.length) {
    E.artifactList.textContent = "Chưa có tệp thay đổi hoặc blocker.";
    return;
  }
  for (const item of items.slice(0, 20)) {
    const row = document.createElement("div");
    row.className = `artifact-item ${item.type}`;
    const label = document.createElement("strong");
    label.textContent = item.label;
    const detail = document.createElement("span");
    detail.textContent = compactText(item.detail, 160);
    row.append(label, detail);
    E.artifactList.appendChild(row);
  }
}

function renderAutonomy() {
  if (!E.autonomySummary && !E.autonomyScanBtn) return;
  const report = latestAutonomyReport();
  const memory = state.autonomy?.memory || report?.memory || {};
  const findings = Array.isArray(report?.findings) ? report.findings : [];
  const initiatives = Array.isArray(report?.longHorizonPlan?.initiatives) ? report.longHorizonPlan.initiatives : [];
  const proposals = Array.isArray(report?.skillProposals) ? report.skillProposals : [];

  if (E.autonomySummary) E.autonomySummary.innerHTML = "";
  if (E.autonomyScanBtn) E.autonomyScanBtn.textContent = state.autonomyScanning ? "Đang quét…" : "Quét idle";

  if (!getWorkspacePath()) {
    if (E.autonomyStatus) E.autonomyStatus.textContent = "Mở workspace để bật quét nợ kỹ thuật khi idle.";
    return;
  }

  if (state.autonomyScanning) {
    if (E.autonomyStatus) E.autonomyStatus.textContent = "Autonomy đang quét read-only; không ghi workspace và không execute command.";
  } else if (!report) {
    if (E.autonomyStatus) E.autonomyStatus.textContent = "Chưa có báo cáo L4/L5. Dashboard sẽ tự quét khi hệ thống idle, hoặc bấm Quét idle.";
  } else {
    const workspaceNote = autonomyWorkspaceMatches() ? "" : " · báo cáo thuộc workspace khác";
    if (E.autonomyStatus) E.autonomyStatus.textContent = `${findings.length} finding · ${initiatives.length} initiative · ${proposals.length} skill proposal · memory ${memory.total || 0}${workspaceNote}`;
  }

  const metrics = [
    ["Memory activation", memory.averageActivation ?? "—"],
    ["Findings", String(findings.length)],
    ["Initiatives", String(initiatives.length)],
    ["L5 proposals", String(proposals.length)]
  ];
  const metricGrid = document.createElement("div");
  metricGrid.className = "autonomy-metrics";
  for (const [label, value] of metrics) {
    const item = document.createElement("div");
    item.className = "autonomy-metric";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = String(value);
    item.append(key, val);
    metricGrid.appendChild(item);
  }
  E.autonomySummary.appendChild(metricGrid);

  const topFinding = findings[0];
  const topInitiative = initiatives[0];
  const topProposal = proposals[0];
  const rows = [
    topFinding && {
      label: "Priority finding",
      title: `${topFinding.severity || "risk"} · ${topFinding.title || topFinding.category}`,
      detail: `${topFinding.source || ""} — ${topFinding.recommendation || topFinding.evidence || ""}`
    },
    topInitiative && {
      label: "Long horizon",
      title: topInitiative.title,
      detail: topInitiative.strategicTradeoff || topInitiative.objective
    },
    topProposal && {
      label: "L5 proposal",
      title: topProposal.name,
      detail: topProposal.proposedModel
    }
  ].filter(Boolean);

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "progress-detail";
    empty.textContent = report ? "Không có vấn đề ưu tiên cao trong lần quét gần nhất." : "Đang chờ báo cáo autonomy.";
    E.autonomySummary.appendChild(empty);
    return;
  }

  for (const item of rows) {
    const row = document.createElement("div");
    row.className = "autonomy-item";
    const label = document.createElement("span");
    label.textContent = item.label;
    const title = document.createElement("strong");
    title.textContent = compactText(item.title, 120);
    const detail = document.createElement("p");
    detail.textContent = compactText(item.detail, 220);
    row.append(label, title, detail);
    E.autonomySummary.appendChild(row);
  }
}

function renderControls() {
  const disabled = state.running;
  // sendBtn stays interactive — it morphs into a Stop button while running.
  const lockedEls = [E.chooseFolderBtn, E.newSessionBtn, E.sessionSelect,
    E.saveSettingsBtn, E.serverInput, E.modelInput, E.apiKeyInput,
    E.autoConfirmInput, E.directWorkspaceInput, E.messageInput,
    E.refreshObservabilityBtn, E.autonomyScanBtn];
  lockedEls.forEach(el => { if (el) el.disabled = disabled; });
  if (E.sendBtn) {
    if (state.running) {
      E.sendBtn.textContent = "Dừng";
      E.sendBtn.dataset.mode = "stop";
      E.sendBtn.classList.add("stop-button");
      E.sendBtn.disabled = false;
    } else {
      E.sendBtn.textContent = "Chạy";
      E.sendBtn.dataset.mode = "send";
      E.sendBtn.classList.remove("stop-button");
      E.sendBtn.disabled = false;
    }
  }
  if (E.autonomyScanBtn) E.autonomyScanBtn.disabled = disabled || state.autonomyScanning || !getWorkspacePath();
}

function renderDashboard() {
  // Throttle: progress events arrive at high frequency (30+/s) during a run;
  // re-rendering every tab on each event wastes CPU and causes jank. We only
  // redraw the full dashboard at most once per animation frame.
  if (renderDashboard._rafId) return;
  renderDashboard._rafId = requestAnimationFrame(function() {
    renderDashboard._rafId = null;
    console.log("[app] renderDashboard start");
    try { renderMetrics(); } catch(e) { console.warn("renderMetrics:", e); }
    try { renderDagBoard(); } catch(e) { console.warn("renderDagBoard:", e); }
    try { fillTabsFromRun(); } catch(e) { console.warn("fillTabsFromRun:", e); }
    try { renderRunSummary(); } catch(e) { console.warn("renderRunSummary:", e); }
    try { renderFlowIO(); } catch(e) { console.warn("renderFlowIO:", e); }
    try { renderAutonomy(); } catch(e) { console.warn("renderAutonomy:", e); }
    console.log("[app] renderDashboard done");
  });
}

function render() {
  renderSettings();
  renderWorkspace();
  renderSessions();
  renderDashboard();
  renderControls();
}

function setStatus(text, timeout = 1800) {
  if (!E.saveStatus) return;
  E.saveStatus.textContent = text;
  if (timeout) {
    setTimeout(() => {
      if (E.saveStatus && E.saveStatus.textContent === text) E.saveStatus.textContent = "";
    }, timeout);
  }
}

// Show a loading overlay while init is in progress
function showLoading(msg) {
  let el = document.getElementById("__loading");
  if (!el) {
    el = document.createElement("div");
    el.id = "__loading";
    el.style.cssText = "position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:16px;font-family:sans-serif;color:#69727a;background:#f7f5ef";
    el.innerHTML = `<div style="width:32px;height:32px;border:3px solid #ddd8cd;border-top-color:#176b63;border-radius:50%;animation:spin .7s linear infinite"></div><p id="__loadingMsg" style="font-size:14px">Đang khởi động…</p><style>@keyframes spin{to{transform:rotate(360deg)}}</style>`;
    document.body.prepend(el);
  }
  const msgEl = document.getElementById("__loadingMsg");
  if (msgEl) msgEl.textContent = msg || "Đang khởi động…";
}
function hideLoading() {
  const el = document.getElementById("__loading");
  if (el) el.remove();
}

async function refreshObservability() {
  const [observabilityResult, autonomyResult] = await Promise.allSettled([
    appApi.getObservability(),
    appApi.getAutonomyStatus()
  ]);

  if (observabilityResult.status === "fulfilled") {
    state.observability = observabilityResult.value;
  } else {
    state.observability = {
      recentEvents: [
        {
          eventType: "observability.error",
          error: observabilityResult.reason?.message || String(observabilityResult.reason)
        }
      ]
    };
  }

  if (autonomyResult.status === "fulfilled") {
    state.autonomy = autonomyResult.value;
  } else {
    state.autonomy = {
      error: autonomyResult.reason?.message || String(autonomyResult.reason),
      memory: state.autonomy?.memory || { total: 0 },
      lastReport: state.autonomy?.lastReport || null
    };
  }
  renderDashboard();
}

async function performAutonomyScan({ automatic = false } = {}) {
  if (!getWorkspacePath() || state.running || state.autonomyScanning) return;
  state.autonomyScanning = true;
  renderDashboard();
  renderControls();
  try {
    state.autonomy = await appApi.runAutonomyScan({ workspacePath: getWorkspacePath(), automatic });
    if (!automatic) setStatus("Đã quét autonomy L4/L5", 2200);
  } catch (error) {
    state.autonomy = {
      ...state.autonomy,
      error: error.message,
      lastReport: latestAutonomyReport()
    };
    if (!automatic) setStatus(`Autonomy chưa quét được: ${error.message}`, 3600);
  } finally {
    state.autonomyScanning = false;
    renderDashboard();
    renderControls();
  }
}

function scheduleIdleAutonomyScan() {
  if (state.autonomyAutoScanScheduled || state.autonomyScanning || state.running || !getWorkspacePath()) return;
  if (autonomyWorkspaceMatches()) return;
  state.autonomyAutoScanScheduled = true;
  setTimeout(() => {
    state.autonomyAutoScanScheduled = false;
    if (!state.running && !state.autonomyScanning && getWorkspacePath() && !autonomyWorkspaceMatches()) {
      performAutonomyScan({ automatic: true });
    }
  }, 1800);
}

async function ensureSession() {
  if (state.activeSession) return state.activeSession;
  const result = await appApi.createSession({ workspacePath: "" });
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  return state.activeSession;
}

async function saveSettings() {
  state.settings = await appApi.saveSettings({
    serverUrl: E.serverInput?.value || state.settings?.serverUrl || "",
    model: E.modelInput?.value || state.settings?.model || "",
    apiKey: E.apiKeyInput?.value || state.settings?.apiKey || "",
    autoConfirmHumanGate: E.autoConfirmInput?.checked ?? state.settings?.autoConfirmHumanGate ?? false,
    directWorkspaceMode: E.directWorkspaceInput?.checked ?? state.settings?.directWorkspaceMode ?? true,
    bypassPolicy: E.bypassPolicyInput?.checked ?? state.settings?.bypassPolicy ?? false,
    modelOverrides: {
      planner: E.plannerModelInput?.value?.trim?.() || state.settings?.modelOverrides?.planner || null,
      coder: E.coderModelInput?.value?.trim?.() || state.settings?.modelOverrides?.coder || null,
      reviewer: E.reviewerModelInput?.value?.trim?.() || state.settings?.modelOverrides?.reviewer || null
    }
  });
  renderSettings();
  setStatus("Đã lưu");
}

function resetFlowView() {
  try { window.__flowView?.reset?.(); } catch (e) { console.warn("flowView.reset:", e); }
}

async function chooseWorkspace() {
  const workspacePath = await appApi.chooseWorkspace();
  if (!workspacePath) return;
  const session = await ensureSession();
  const result = await appApi.updateSessionWorkspace(session.id, workspacePath);
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  state.progress = [];
  state.activeTask = "";
  resetFlowView();
  render();
  scheduleIdleAutonomyScan();
}

async function createSession() {
  const result = await appApi.createSession({ workspacePath: getWorkspacePath() });
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  state.progress = [];
  state.activeTask = "";
  resetFlowView();
  render();
  scheduleIdleAutonomyScan();
  E.messageInput.focus();
}

async function loadSession(sessionId) {
  if (!sessionId || sessionId === state.activeSession?.id) return;
  const session = await appApi.loadSession(sessionId);
  if (!session) return;
  state.activeSession = session;
  state.progress = [];
  state.activeTask = "";
  resetFlowView();
  // Replay persisted events of the new session's latest run (if any).
  const persisted = (session?.runs || []).slice(-1)[0]?.progressEvents || [];
  if (persisted.length) {
    try { window.__flowView?.setEventHistory(persisted); } catch (e) {}
  }
  render();
  scheduleIdleAutonomyScan();
}

async function sendMessage(event) {
  if (event && typeof event.preventDefault === "function") event.preventDefault();
  const content = (E.messageInput?.value || "").trim();
  if (!content || state.running) return;

  await saveSettings();
  const session = await ensureSession();
  if (!getWorkspacePath()) {
    setStatus("Hãy mở thư mục trước", 2600);
    return;
  }

  state.running = true;
  state.currentExecutionId = null;
  state.progress = [
    {
      stage: "running",
      detail: "Bắt đầu chạy pipeline",
      at: new Date().toISOString()
    }
  ];
  state.activeTask = content;
  // Clear FlowView before this run starts so we don't display stale nodes
  // from a previous task in the same session.
  resetFlowView();
  E.messageInput.value = "";
  E.messageInput.style.height = "auto";
  render();

  try {
    const result = await appApi.sendMessage({
      sessionId: session.id,
      workspacePath: getWorkspacePath(),
      settings: state.settings,
      content
    });
    if (result?.session) {
      state.activeSession = result.session;
      if (Array.isArray(result.sessions?.sessions)) state.sessions = result.sessions.sessions;
    } else {
      // Browser/dev mode talks to Python directly and receives a run rather
      // than Electron's { session, sessions, run } response envelope.
      const run = result?.run || result;
      const runIdentity = run?.executionId || run?.id;
      const priorRuns = Array.isArray(session.runs) ? session.runs : [];
      const observedRun = {
        ...run,
        task: run?.task || content,
        progressEvents: Array.isArray(run?.progressEvents) && run.progressEvents.length
          ? run.progressEvents
          : [...state.progress]
      };
      state.activeSession = {
        ...session,
        runs: [
          ...priorRuns.filter((item) => (item.executionId || item.id) !== runIdentity),
          observedRun
        ]
      };
      state.sessions = [
        state.activeSession,
        ...state.sessions.filter((item) => item.id !== state.activeSession.id)
      ];
    }
    if (result?.error) setStatus(result.error, 3600);
    await refreshObservability();
    scheduleIdleAutonomyScan();
  } catch (error) {
    setStatus(`Chưa gửi được yêu cầu: ${error.message}`, 3600);
  } finally {
    state.running = false;
    state.currentExecutionId = null;
    window.__currentSendController = null;
    render();
    E.messageInput.focus();
  }
}

async function cancelCurrentRun() {
  const execId = state.currentExecutionId || window.__currentExecutionId;
  if (!execId && !window.__currentSendController) {
    setStatus("Không có task nào đang chạy để dừng", 1800);
    return;
  }
  setStatus("Đang dừng pipeline...", 2400);
  // 1) Tell the backend to honor the cancel at the next node boundary.
  if (execId) {
    try {
      if (typeof appApi.cancelRun === "function") await appApi.cancelRun(execId);
    } catch (e) { console.warn("cancelRun:", e); }
  }
  // 2) Abort the inflight fetch if the renderer owns it (web-fallback path).
  if (window.__currentSendController) {
    try { window.__currentSendController.abort(); } catch (e) {}
    window.__currentSendController = null;
  }
  // UI feedback — backend may take seconds to actually unwind.
  state.progress.push({
    stage: "cancelled",
    detail: "Đã gửi yêu cầu dừng — chờ pipeline kết thúc node hiện tại",
    at: new Date().toISOString(),
  });
  render();
}

function autoResizeInput() {
  if (!E.messageInput) return;
  E.messageInput.style.height = "auto";
  E.messageInput.style.height = `${Math.min(E.messageInput.scrollHeight, 170)}px`;
}

async function init() {
  showLoading("Đang kết nối backend…");
  // Remove inline bootstrap placeholder
  const bs = document.getElementById("__bootstrap");
  if (bs) bs.remove();
  try {
    const initial = await appApi.getInitialState();
    state.settings = initial.settings || {};
    state.sessions = (initial.sessions && initial.sessions.sessions) ? initial.sessions.sessions : [];
    state.activeSession = initial.activeSession || null;

    // If we got a backend response but no settings, bootstrap defaults
    if (!state.settings.serverUrl) {
      state.settings = {
        serverUrl: "http://localhost:20128/v1",
        model: "gemini/gemini-3.1-flash-lite",
        apiKey: "",
        autoConfirmHumanGate: false,
        directWorkspaceMode: true,
        modelOverrides: {},
        bypassPolicy: false,
        ...state.settings
      };
    }

    // Guard against double init — earlier code could leave a stale progress
    // listener attached, causing each event to fire N times after a reload.
    if (window.__onProgressOff) {
      try { window.__onProgressOff(); } catch (e) {}
      window.__onProgressOff = null;
    }
    const offProgress = appApi.onProgress((progress) => {
      if (state.activeSession && progress.sessionId && progress.sessionId !== state.activeSession.id) return;
      // Capture executionId from progress so the Stop button knows what to cancel.
      if (progress.executionId && !state.currentExecutionId) state.currentExecutionId = progress.executionId;
      state.progress.push(progress);
      // Bridge IPC → window event so FlowView (which only listens to window events)
      // receives updates in Electron mode too (web-fallback dispatches inside fetch loop).
      try { window.dispatchEvent(new CustomEvent("agent:progress", { detail: progress })); } catch (e) {}
      try { renderDashboard(); } catch(e) { console.error('renderDashboard error:', e); }
      try { renderControls(); } catch(e) { console.error('renderControls error:', e); }
      // Auto-refresh review tab when review-stage events arrive so the
      // user sees blockers/warnings/verdict update live without clicking.
      if (/^(security_reviewer_agent|code_reviewer_agent|doctor_feedback|release_deploy_agent|reviewer_decision|execution_gate)$/.test(String(progress.stage || ""))) {
        try {
          const activeSubtab = document.querySelector(".review-subtabs .subtab.active")?.dataset.subtab || "code";
          // After a beat for the graph node to finish writing state
          setTimeout(() => renderReviewSubtab(activeSubtab), 200);
        } catch (e) {}
      }
    });
    if (typeof offProgress === "function") window.__onProgressOff = offProgress;

    // Boot the n8n-style flow view once the canvas is in the DOM.
    try {
      if (E.flowCanvas && window.FlowView && appApi.getTopology) {
        // Tear down any prior FlowView (in dev/HMR or after Ctrl+R the previous
        // instance leaks its window event listener).
        if (window.__flowView && typeof window.__flowView.destroy === "function") {
          try { window.__flowView.destroy(); } catch (e) {}
        }
        window.__flowView = new window.FlowView(E.flowCanvas, E.flowDetail);
        await window.__flowView.load(() => appApi.getTopology());
        const persisted = (state.activeSession?.runs || []).slice(-1)[0]?.progressEvents || [];
        if (persisted.length) window.__flowView.setEventHistory(persisted);
      }
    } catch (e) { console.error('flowView init:', e); }

    hideLoading();
    render();
    console.log("[app] init complete — dashboard rendered");
    setStatus("Backend sẵn sàng — mở workspace để bắt đầu", 0);
    try { await refreshObservability(); } catch(e) { console.error('observability error:', e); }
    scheduleIdleAutonomyScan();
    if (E.messageInput) E.messageInput.focus();
    try { _doctorBindIpc(); } catch (e) { console.error("doctor bind failed:", e); }
  } catch (error) {
    console.error('init error:', error);
    hideLoading();
    document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif;background:#fef2f2;color:#991b1b;min-height:100vh">
      <h2>Không khởi động được dashboard</h2>
      <p style="color:#c7512f;font-size:15px">${error.message}</p>
      <pre style="font-size:11px;color:#69727a;white-space:pre-wrap">${error.stack || ''}</pre>
      <p style="margin-top:20px;color:#69727a;font-size:13px">Backend Python có thể chưa khởi động xong, hoặc đang chạy ở chế độ browser không có Electron preload.</p>
      <button onclick="location.reload()" style="margin-top:16px;padding:10px 24px;cursor:pointer;font-size:14px;background:#176b63;color:#fff;border:none;border-radius:8px">Thử lại</button>
      <button onclick="window.__forceShow&&window.__forceShow()" style="margin-top:16px;margin-left:8px;padding:10px 24px;cursor:pointer;font-size:14px;background:#c7512f;color:#fff;border:none;border-radius:8px">Hiển thị dashboard tối thiểu</button>
    </div>`;
  }
}

// Force-show minimal dashboard even when backend is unreachable
window.__forceShow = function() {
  state.settings = state.settings || {
    serverUrl: "http://localhost:20128/v1", model: "gemini/gemini-3.1-flash-lite",
    apiKey: "", autoConfirmHumanGate: false, directWorkspaceMode: true,
    modelOverrides: {}, bypassPolicy: false
  };
  hideLoading();
  document.body.innerHTML = document.querySelector(".app-shell")?.outerHTML || "";
  // Re-run setup since DOM was destroyed
  render();
  setupTabs();
  setStatus("Backend chưa sẵn sàng — dashboard hiển thị ở chế độ tối thiểu", 0);
};

E.chooseFolderBtn?.addEventListener("click", chooseWorkspace);
E.newSessionBtn?.addEventListener("click", createSession);
E.saveSettingsBtn?.addEventListener("click", saveSettings);
E.sessionSelect?.addEventListener("change", (event) => loadSession(event.target.value));
E.messageInput?.addEventListener("input", autoResizeInput);
E.sendBtn?.addEventListener("click", (e) => {
  e.preventDefault();
  if (E.sendBtn.dataset.mode === "stop") {
    cancelCurrentRun();
  } else {
    sendMessage(e);
  }
});
E.messageInput?.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage(event);
  }
});
E.refreshObservabilityBtn?.addEventListener("click", refreshObservability);
E.autonomyScanBtn?.addEventListener("click", () => performAutonomyScan({ automatic: false }));

// ============ Tab switching ============
function setupTabs() {
  const tabs = document.querySelectorAll(".tab-bar .tab");
  const contents = document.querySelectorAll(".tab-content");
  if (!tabs.length) return;
  tabs.forEach(t => {
    if (t.dataset.tabsBound === "true") return;
    t.dataset.tabsBound = "true";
    t.addEventListener("click", () => {
      const name = t.dataset.tab;
      tabs.forEach(x => {
        const active = x === t;
        x.classList.toggle("active", active);
        x.setAttribute("aria-selected", String(active));
      });
      contents.forEach(c => c.classList.toggle("active", c.id === `tab-${name}`));
      if (name === "log") renderLogTab();
      if (name === "review") {
        const activeSubtab = document.querySelector(".review-subtabs .subtab.active")?.dataset.subtab || "code";
        renderReviewSubtab(activeSubtab);
      }
    });
  });
  // Review sub-tabs
  document.querySelectorAll(".review-subtabs .subtab").forEach(st => {
    if (st.dataset.tabsBound === "true") return;
    st.dataset.tabsBound = "true";
    st.addEventListener("click", () => {
      document.querySelectorAll(".review-subtabs .subtab").forEach(s => s.classList.remove("active"));
      st.classList.add("active");
      renderReviewSubtab(st.dataset.subtab);
    });
  });
  [E.logFilterNode, E.logFilterType, E.logToggleDebug].forEach((control) => {
    if (!control || control.dataset.tabsBound === "true") return;
    control.dataset.tabsBound = "true";
    control.addEventListener("change", renderLogTab);
  });
  // Settings drawer
  const drawer = document.querySelector("#settingsDrawer");
  const overlay = document.querySelector("#settingsDrawerOverlay");
  const toggleBtn = document.querySelector("#settingsToggleBtn");
  if (drawer && overlay && toggleBtn && toggleBtn.dataset.tabsBound !== "true") {
    toggleBtn.dataset.tabsBound = "true";
    toggleBtn.addEventListener("click", () => { drawer.classList.add("open"); overlay.classList.add("visible"); });
    overlay.addEventListener("click", () => { drawer.classList.remove("open"); overlay.classList.remove("visible"); });
  }
}
setupTabs();

// Rich run tabs. Every renderer owns exactly one DOM region; the old dashboard
// renderers used to overwrite changesFileList/logList immediately after these.
function _tabEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function _tabSet(id, html) {
  const element = document.getElementById(id);
  if (element) element.innerHTML = html;
}

function _tabText(value) {
  if (typeof value === "string") return value;
  if (value == null) return "";
  return value.detail || value.message || value.claim || value.summary || value.name || JSON.stringify(value);
}

function _runEvents(run = latestRun()) {
  // While a new run is active, stream live progress events from the IPC buffer.
  // Once the pipeline finishes, merge the persisted progressEvents (saved by
  // main.js when the NDJSON stream ended) with the tail of live events so
  // that the final rendered tabs — especially the log — include the verify
  // and done stages that may have arrived after session save.
  if (state.running) return state.progress;
  const persisted = Array.isArray(run?.progressEvents) ? run.progressEvents : [];
  if (!state.progress.length) return persisted;
  // Merge: use live events for anything newer than the last persisted event.
  const lastPersistedAt = persisted.length ? (persisted[persisted.length - 1].at || "") : "";
  const tail = lastPersistedAt
    ? state.progress.filter(function (ev) { return (ev.at || "") > lastPersistedAt; })
    : state.progress;
  if (!tail.length) return persisted;
  return persisted.concat(tail);
}

function fillTabsFromRun() {
  // While a new run is active, do not present the previous run as current data.
  const run = state.running ? null : latestRun();
  renderAnalysisTab(run);
  renderPlanTab(run);
  renderExecutionTab(run);
  const subtab = document.querySelector(".review-subtabs .subtab.active")?.dataset.subtab || "code";
  renderReviewSubtab(subtab, run);
  renderChangesTab(run);
  renderLogTab();
  updateTabBadges(run);
  renderOverviewTab(run);
}

function renderOverviewTab(run) {
  const ri = run?.repoIntelligence;
  const status = state.running ? "running" : runStatus(run);
  const confidence = Number(ri?.analysis_confidence);
  _tabSet("ovStatus", `<span class="run-state ${_tabEscape(status)}">${_tabEscape(statusLabel(status))}</span>${
    Number.isFinite(confidence) ? ` <span class="muted">Confidence ${confidence.toFixed(2)}</span>` : ""
  }`);
  const task = state.running
    ? state.activeTask
    : (ri?.request_understanding?.goal || run?.task || state.activeTask);
  _tabSet("ovTask", task ? `<p>${_tabEscape(task)}</p>` : `<p class="muted">Chưa có tác vụ</p>`);
  const changedCount = Array.isArray(run?.changedFiles) ? run.changedFiles.length : 0;
  const evidenceCount = Array.isArray(ri?.evidence) ? ri.evidence.length : 0;
  const impactCount = Array.isArray(ri?.change_impact_map) ? ri.change_impact_map.length : 0;
  _tabSet("ovStats", `<p>${changedCount} files · ${evidenceCount} evidence · ${impactCount} impacts</p>`);
}

function renderAnalysisTab(run) {
  const ri = run?.repoIntelligence || null;
  const request = ri?.request_understanding || run?.problem || run?.taskIntent;

  if (!request && !ri) {
    _tabSet("anRequest", `<p class="muted">${state.running ? "Đang phân tích yêu cầu…" : "Chưa phân tích"}</p>`);
    _tabSet("anEvidence", `<p class="muted">Chưa có evidence</p>`);
    _tabSet("anImpact", `<p class="muted">Chưa có impact analysis</p>`);
    _tabSet("anRisks", `<p class="muted">Chưa có</p>`);
    return;
  }

  // ── 1) Request understanding (structured) ──────────────────────────
  let reqHtml = "";
  if (request) {
    reqHtml = `<div class="an-block">
      <h4>🎯 Goal</h4><p>${_tabEscape(_tabText(request.goal || request))}</p>
      ${request.task_type ? `<p><span class="an-tag">Loại</span> ${_tabEscape(request.task_type)}</p>` : ""}
      ${request.expected_behavior ? `<p><span class="an-tag">Expected</span> ${_tabEscape(request.expected_behavior)}</p>` : ""}
      ${request.observed_behavior ? `<p><span class="an-tag">Observed</span> ${_tabEscape(request.observed_behavior)}</p>` : ""}
      ${request.repro ? `<p><span class="an-tag">Repro</span><code>${_tabEscape(request.repro)}</code></p>` : ""}
      ${request.confidence != null ? `<p><span class="an-tag">Confidence</span> ${_tabEscape(String(run?.repoIntelligence?.analysis_confidence || request.confidence))}</p>` : ""}
    </div>`;
  }
  _tabSet("anRequest", reqHtml || `<pre class="an-raw">${_tabEscape(JSON.stringify(request, null, 2))}</pre>`);

  // ── 2) Evidence (grouped by type) ──────────────────────────────────
  const evidence = Array.isArray(ri?.evidence) ? ri.evidence : [];
  let evHtml = "";
  if (evidence.length) {
    const byType = {};
    for (const item of evidence) {
      const kind = item.evidence_type || item.type || "other";
      if (!byType[kind]) byType[kind] = [];
      byType[kind].push(item);
    }
    for (const [kind, items] of Object.entries(byType)) {
      evHtml += `<div class="ev-group"><h4 class="ev-kind">${_tabEscape(kind)} (${items.length})</h4>`;
      for (const item of items.slice(0, 12)) {
        const loc = item.file_path || item.path || "";
        const line = item.line_number || item.line || "";
        evHtml += `<div class="ev-row"><span class="ev-snippet">${_tabEscape(_tabText(item))}</span>${loc ? `<small class="ev-loc">${_tabEscape(loc)}${line ? `:${_tabEscape(line)}` : ""}</small>` : ""}</div>`;
      }
      evHtml += `</div>`;
    }
  } else {
    evHtml = `<p class="muted">Chưa có evidence</p>`;
  }
  _tabSet("anEvidence", evHtml);

  // ── 3) Impact map ──────────────────────────────────────────────────
  const impacts = Array.isArray(ri?.change_impact_map) ? ri.change_impact_map : [];
  _tabSet("anImpact", impacts.length
    ? impacts.slice(0, 20).map((item) => {
        const level = item.level || item.risk_level || "info";
        const target = item.target || item.symbol || item.path || _tabText(item);
        const reason = item.reason || item.evidence || "";
        return `<div class="impact-row"><span class="impact-level impact-${_tabEscape(level)}">${_tabEscape(level)}</span><span class="impact-target">${_tabEscape(target)}</span>${reason ? `<small class="muted">${_tabEscape(reason)}</small>` : ""}</div>`;
      }).join("")
    : `<p class="muted">Chưa có impact analysis</p>`);

  // ── 4) Risks & unknowns ────────────────────────────────────────────
  const critiques = Array.isArray(run?.critiques) ? run.critiques : [];
  const risks = [
    ...(Array.isArray(ri?.risks) ? ri.risks : []),
    ...(Array.isArray(ri?.unknowns) ? ri.unknowns : []),
    ...(Array.isArray(ri?.task_classification?.risks) ? ri.task_classification.risks : []),
    ...critiques.flatMap((item) => [
      ...(Array.isArray(item?.risks) ? item.risks : []),
      ...(Array.isArray(item?.warnings) ? item.warnings : []),
      ...(Array.isArray(item?.blockers) ? item.blockers : [])
    ]),
    ...(Array.isArray(ri?.metadata?.quality_gate?.warnings) ? ri.metadata.quality_gate.warnings : [])
  ].filter(Boolean);
  _tabSet("anRisks", risks.length
    ? `<div class="risk-list">${risks.slice(0, 25).map((item) => `<div class="risk-row">⚠ ${_tabEscape(_tabText(item))}</div>`).join("")}</div>`
    : `<p class="muted">Không phát hiện rủi ro hoặc unknown</p>`);
}

function renderPlanTab(run) {
  const finalPlan = run?.finalPlan || null;
  const candidates = Array.isArray(run?.plans) ? run.plans : [];
  const critiques = Array.isArray(run?.critiques) ? run.critiques : [];
  if (!finalPlan) {
    _tabSet("planItems", `<p class="muted">${state.running ? "Đang lập kế hoạch…" : "Chưa có kế hoạch"}</p>`);
    _tabSet("planHistory", "");
    return;
  }

  const spec = finalPlan.workerTaskSpec || {};
  const steps = Array.isArray(finalPlan.finalSteps)
    ? finalPlan.finalSteps
    : (Array.isArray(finalPlan.steps) ? finalPlan.steps : []);
  const allowedFiles = Array.isArray(spec.allowedFiles) ? spec.allowedFiles : [];
  const forbiddenPaths = Array.isArray(spec.forbiddenPaths) ? spec.forbiddenPaths : [];
  const commands = Array.isArray(spec.commandsToRun) ? spec.commandsToRun : [];
  const verifyCmds = Array.isArray(spec.verificationCommands) ? spec.verificationCommands : [];
  const acceptance = Array.isArray(spec.acceptanceCriteria) ? spec.acceptanceCriteria : [];
  const constraints = Array.isArray(spec.constraints) ? spec.constraints : [];

  _tabSet("planItems", `
    <section class="plan-item">
      <div class="plan-header">
        <div class="plan-meta">
          <strong>${_tabEscape(finalPlan.selectedPlanName || "Kế hoạch đã chọn")}</strong>
          <span class="risk-chip risk-${_tabEscape(finalPlan.riskClass || "medium")}">Rủi ro: ${_tabEscape(finalPlan.riskClass || "medium")}</span>
        </div>
        <p class="plan-objective">${_tabEscape(spec.objective || finalPlan.selectedPlanName || "")}</p>
      </div>

      ${steps.length ? `<div class="plan-section"><h4>🧩 Các bước thực thi</h4><ol class="plan-steps">${steps.map((step) => `<li>${_tabEscape(_tabText(step))}</li>`).join("")}</ol></div>` : ""}

      <div class="plan-grid">
        ${allowedFiles.length ? `<div class="plan-section"><h4>📁 File được phép sửa (${allowedFiles.length})</h4><ul>${allowedFiles.map((f) => `<li><code>${_tabEscape(typeof f === "string" ? f : (f.path || f.file || _tabText(f)))}</code></li>`).join("")}</ul></div>` : ""}
        ${forbiddenPaths.length ? `<div class="plan-section"><h4>🚫 File cấm</h4><ul>${forbiddenPaths.map((f) => `<li><code>${_tabEscape(typeof f === "string" ? f : (f.path || _tabText(f)))}</code></li>`).join("")}</ul></div>` : ""}
        ${commands.length ? `<div class="plan-section"><h4>⚙ Lệnh chạy</h4><ul>${commands.map((c) => `<li><code>${_tabEscape(typeof c === "string" ? c : (c.command || _tabText(c)))}</code></li>`).join("")}</ul></div>` : ""}
        ${verifyCmds.length ? `<div class="plan-section"><h4>✅ Lệnh verification</h4><ul>${verifyCmds.map((c) => `<li><code>${_tabEscape(typeof c === "string" ? c : (c.command || _tabText(c)))}</code></li>`).join("")}</ul></div>` : ""}
      </div>

      ${acceptance.length ? `<div class="plan-section"><h4>🎯 Tiêu chí nghiệm thu</h4><ul>${acceptance.map((item) => `<li>✅ ${_tabEscape(_tabText(item))}</li>`).join("")}</ul></div>` : ""}
      ${constraints.length ? `<div class="plan-section"><h4>🔒 Ràng buộc</h4><ul>${constraints.map((item) => `<li>${_tabEscape(_tabText(item))}</li>`).join("")}</ul></div>` : ""}
      ${finalPlan.humanGateReason ? `<div class="plan-section ev-alert ev-alert-warn"><strong>⚠ Lý do cần xác nhận</strong><p>${_tabEscape(finalPlan.humanGateReason)}</p></div>` : ""}
    </section>`);

  // ── Candidate plan history ────────────────────────────────────────────
  let historyHtml = "";
  if (candidates.length) {
    historyHtml += `<h4>${candidates.length} phương án ứng viên</h4>`;
    for (let i = 0; i < candidates.length; i++) {
      const plan = candidates[i];
      const label = plan.agent || plan.strategy || plan.name || `Plan ${i + 1}`;
      const planSteps = Array.isArray(plan.steps) ? plan.steps : [];
      historyHtml += `<details class="plan-candidate"><summary><strong>${_tabEscape(label)}</strong></summary><pre>${_tabEscape(JSON.stringify(plan, null, 2))}</pre></details>`;
    }
  }
  if (critiques.length) {
    historyHtml += `<h4>Critique (${critiques.length})</h4>`;
    for (const c of critiques) {
      const agent = c.agent || "critique";
      historyHtml += `<details class="plan-candidate"><summary><strong>${_tabEscape(agent)}</strong> · blockers ${(c.blockers||[]).length} · warnings ${(c.warnings||[]).length}</summary><pre>${_tabEscape(JSON.stringify(c, null, 2))}</pre></details>`;
    }
  }
  _tabSet("planHistory", historyHtml);
}

function renderExecutionTab(run) {
  const events = _runEvents(run);
  const ignoredStages = new Set(["queued", "running", "resume", "cancelled", "error", "done", "checkpoint"]);
  const nodeEvents = events.filter((event) => event.stage && !ignoredStages.has(event.stage));
  const lastNodeEvent = [...events].reverse().find((event) => event.node || (event.stage && !ignoredStages.has(event.stage)));
  const progress = document.getElementById("execProgress");
  const current = document.getElementById("execCurrent");
  if (progress) progress.textContent = events.length
    ? `${nodeEvents.length} bước · ${statusLabel(runStatus(run))}`
    : state.running ? "Đang khởi động pipeline…" : "Chưa chạy";
  if (current) current.textContent = lastNodeEvent
    ? `→ ${lastNodeEvent.node || lastNodeEvent.stage} · ${_tabText(lastNodeEvent)}`
    : state.running ? "Đang khởi động…" : "";

  // ── Phased timeline ─────────────────────────────────────────────────
  const execPhases = [
    { label: "Intake & Analysis", match: /^(preflight|codegraph_context|repo_intelligence|intake|task_intent|actr_memory)/ },
    { label: "Planning", match: /^(planning|critique|plan_arbiter|planner_task_graph|researcher_context|governance|human_gate|environment_gate)/ },
    { label: "Coder + Tester", match: /^(workspace_mode|load_context|setup|openhands_worker|tester_agent|codegraph_affected)/ },
    { label: "Review", match: /^(security_reviewer_agent|code_reviewer_agent|release_deploy_agent|reviewer_decision|execution_gate)/ },
    { label: "Finalize", match: /^(reporter|finalize_workspace|reporter_end)/ },
  ];
  let phaseHtml = "";
  for (const phase of execPhases) {
    const phaseEvts = nodeEvents.filter((e) => phase.match.test(e.stage));
    if (!phaseEvts.length) continue;
    phaseHtml += `<div class="exec-phase"><h4 class="exec-phase-heading">${_tabEscape(phase.label)}</h4>`;
    for (const event of phaseEvts.slice(-20)) {
      const time = event.at ? formatTime(event.at) : "";
      const tag = event.node || event.stage || "event";
      phaseHtml += `<div class="exec-line"><span class="exec-time">${_tabEscape(time)}</span><span class="exec-tag">${_tabEscape(tag)}</span><span>${_tabEscape(compactText(event.detail || event.message || "", 400))}</span></div>`;
    }
    phaseHtml += `</div>`;
  }
  _tabSet("execStream", phaseHtml || `<span class="muted">${state.running ? "Đang chạy intake…" : "Đang chờ tác vụ…"}</span>`);

  // ── Tool/Coder detail────────────────────────────────────────────────
  const toolEvents = events.filter((event) => {
    return /^(openhands_worker|coder|tool|action|observation|setup|tester_agent|security_reviewer_agent|code_reviewer_agent)/i.test(String(event.stage || event.type || ""));
  });
  const openhandsResult = (run?.reworkAttempts && run.reworkAttempts[run.reworkAttempts.length - 1]) || (run?.workerAttempts && run.workerAttempts[0]) || {};
  const toolSummary = openhandsResult?.summary || openhandsResult?.finalMessage || "";
  const toolErrors = openhandsResult?.error ? [openhandsResult.error] : [];
  _tabSet("execToolCalls", (toolEvents.length || toolSummary)
    ? `<details open><summary><strong>Hoạt động coder/tool (${toolEvents.length} sự kiện)</strong></summary>
      ${toolSummary ? `<p style="margin:6px 0;white-space:pre-wrap">${_tabEscape(compactText(toolSummary, 600))}</p>` : ""}
      ${toolErrors.length ? `<p style="color:var(--accent);margin:6px 0">⚠ ${_tabEscape(toolErrors.join("; "))}</p>` : ""}
      ${toolEvents.slice(-30).map((event) =>
        `<div class="tool-row"><strong>${_tabEscape(event.stage || event.type)}</strong> ${_tabEscape(compactText(event.detail || event.message || "", 320))}</div>`).join("")}
      </details>`
    : "");

  // ── Tests ───────────────────────────────────────────────────────────
  const tester = run?.testerResult || run?.tester || run?.review?.testerResult || run?.review?.upstreamEvidence || {};
  const commandResults = Array.isArray(tester.commandResults)
    ? tester.commandResults
    : (Array.isArray(run?.commandResults) ? run.commandResults : []);
  if (tester.passed == null && !commandResults.length) {
    _tabSet("execTests", state.running ? `<p class="muted">Đang chạy tester…</p>` : "");
    return;
  }
  const blockers = Array.isArray(tester.blockers) ? tester.blockers : [];
  const warnings = Array.isArray(tester.warnings) ? tester.warnings : [];
  _tabSet("execTests", `
    <div class="tester-verdict ${tester.passed === true ? "pass" : tester.passed === false ? "fail" : "running"}">
      <strong>Tester: ${tester.passed === true ? "✅ Đạt" : tester.passed === false ? "❌ Không đạt" : "Đã chạy"}</strong>
      <span class="muted"> · ${tester.verificationMode || "host"} · ${commandResults.length} lệnh</span>
    </div>
    ${tester.finalMessage ? `<p style="white-space:pre-wrap">${_tabEscape(tester.finalMessage)}</p>` : ""}
    ${blockers.length ? `<div class="ev-alert ev-alert-block"><strong>Blocker (${blockers.length})</strong><ul>${blockers.map((item) => `<li>${_tabEscape(_tabText(item))}</li>`).join("")}</ul></div>` : ""}
    ${warnings.length ? `<div class="ev-alert ev-alert-warn"><strong>Cảnh báo (${warnings.length})</strong><ul>${warnings.map((item) => `<li>${_tabEscape(_tabText(item))}</li>`).join("")}</ul></div>` : ""}
    ${commandResults.length ? `<details><summary><strong>Chi tiết lệnh (${commandResults.length})</strong></summary><div class="command-results">${commandResults.map((item) => {
      const ok = item.skipped || item.code === 0;
      const status = item.skipped ? `skipped: ${item.reason || ""}` : item.timedOut ? "timeout" : `exit ${item.code}`;
      return `<div class="command-row ${ok ? "pass" : "fail"}"><code>${_tabEscape(item.command || "command")}</code><span>${_tabEscape(status)}</span>${item.stdout ? `<pre class="cmd-stdout">${_tabEscape(compactText(item.stdout, 400))}</pre>` : ""}</div>`;
    }).join("")}</div></details>` : ""}`);
}

function _reviewPart(run, name) {
  if (name === "security") return run?.securityReview || run?.review?.securityReview || null;
  if (name === "release") return run?.releaseDeployPlan || run?.releasePlan || run?.review?.releasePlan || null;
  // code/correctness: prefer top-level codeReview, then reviewerDecision, then nested review
  return run?.codeReview || run?.review?.codeReview || run?.reviewerDecision || run?.latestReview || run?.review || null;
}

function renderReviewSubtab(name, explicitRun) {
  const run = arguments.length > 1 ? explicitRun : (state.running ? null : latestRun());
  const target = document.getElementById("reviewContent");
  if (!target) return;
  const source = _reviewPart(run, name);
  if (!source || (typeof source === "object" && Object.keys(source).length === 0)) {
    target.innerHTML = `<p class="muted">${state.running ? "Review sẽ xuất hiện sau khi thực thi xong." : `Chưa có ${_tabEscape(name)} review`}</p>`;
    return;
  }
  const blockers = Array.isArray(source.blockers) ? source.blockers : [];
  const warnings = Array.isArray(source.warnings) ? source.warnings : [];
  const passed = source.passed;
  const releaseNotes = Array.isArray(source.releaseNotes) ? source.releaseNotes : [];
  const reviewFocus = Array.isArray(source.reviewFocus) ? source.reviewFocus : [];
  const verdict = passed === true
    ? "✅ Đạt"
    : passed === false
      ? "❌ Không đạt"
      : source.needsApproval === true
        ? "⏳ Cần phê duyệt"
        : source.verdict || "Đã đánh giá";
  const upstreamEvidence = source.upstreamEvidence || {};
  const upstreamBlockers = Array.isArray(upstreamEvidence.blockers) ? upstreamEvidence.blockers : [];
  const upstreamWarnings = Array.isArray(upstreamEvidence.warnings) ? upstreamEvidence.warnings : [];

  // Rework cycle bar
  const revision = Number.isFinite(source.revision) ? source.revision : (Number.isFinite(run?.revision) ? run.revision : 0);
  const reworkCycle = Number.isFinite(source.reviewCycle) ? source.reviewCycle : (Number.isFinite(run?.reviewCycle) ? run.reviewCycle : 0);
  const reworkLimit = Number.isFinite(run?.retryLimit) ? run.retryLimit : 3;
  const doctorEvidence = source.doctorEvidence || (run?.doctorStatus?.doctorFindings?.length ? { issuesCount: run.doctorStatus.issuesCount, applied: run.doctorStatus.applied, verificationPassed: run.doctorStatus.verificationPassed } : null);

  let h = "";

  // ── Rework cycle indicator ─────────────────────────────────────────────
  if (revision > 0 || reworkCycle > 0) {
    const cycleBar = reworkCycle > 0
      ? Array.from({ length: Math.min(reworkCycle, 8) }, () => `<span class="rework-dot active"></span>`).join("")
        + Array.from({ length: Math.max(0, reworkLimit - reworkCycle) }, () => `<span class="rework-dot"></span>`).join("")
      : "";
    h += `<div class="review-rework-bar">
      <span class="rework-label">Rework: ${reworkCycle}/${reworkLimit}</span>
      <span class="rework-dots">${cycleBar}</span>
      <span class="rework-label">Revision ${revision}</span>
    </div>`;
  }

  // ── Doctor relief badge ────────────────────────────────────────────────
  if (doctorEvidence) {
    const relief = doctorEvidence.verificationPassed
      ? `<span class="doctor-chip pass">🩺 Doctor: ${doctorEvidence.applied || 0} fix · verify PASS · ${doctorEvidence.issuesCount || 0} issue</span>`
      : `<span class="doctor-chip warn">🩺 Doctor: ${doctorEvidence.applied || 0} fix · verify FAIL · ${doctorEvidence.issuesCount || 0} issue</span>`;
    h += `<div class="review-doctor-bar">${relief}</div>`;
  }

  // ── Verdict ────────────────────────────────────────────────────────────
  h += `<div class="review-verdict ${passed === true ? "pass" : passed === false ? "fail" : "idle"}">
    <div><strong>Verdict: ${_tabEscape(verdict)}</strong>
    ${source.riskClass ? `<span class="risk-chip">Rủi ro: ${_tabEscape(source.riskClass)}</span>` : ""}
    ${source.executionMode ? `<span class="muted"> · ${_tabEscape(source.executionMode)}</span>` : ""}
    ${source.sandboxed != null ? `<span class="muted"> · ${source.sandboxed ? "sandboxed" : "no-sandbox"}</span>` : ""}</div>
    ${reviewFocus.length ? `<div class="review-focus">Focus: ${reviewFocus.map((f) => _tabEscape(String(f))).join(", ")}</div>` : ""}
    ${source.finalMessage ? `<p style="margin:8px 0 0;white-space:pre-wrap">${_tabEscape(source.finalMessage)}</p>` : ""}
  </div>`;

  // ── Blocker cards ──────────────────────────────────────────────────────
  if (blockers.length) {
    h += `<h4 style="margin:14px 0 8px;color:#ef4444;font-size:12px;letter-spacing:1px">BLOCKER (${blockers.length})</h4>`;
    for (const item of blockers) {
      const text = _tabEscape(_tabText(item));
      const isDoctorFixed = /^\[doctor:\s*fixed\]/i.test(text);
      h += `<div class="review-card review-card-block ${isDoctorFixed ? "review-card-relieved" : ""}">
        <div class="review-card-agent"></div>
        <div class="review-card-text">${text}</div>
      </div>`;
    }
  }

  // ── Warning cards ──────────────────────────────────────────────────────
  if (warnings.length) {
    h += `<h4 style="margin:14px 0 8px;color:#eab308;font-size:12px;letter-spacing:1px">CẢNH BÁO (${warnings.length})</h4>`;
    for (const item of warnings) {
      const text = _tabEscape(_tabText(item));
      const isDoctorFixed = /^\[doctor:\s*fixed\]/i.test(text);
      h += `<div class="review-card review-card-warn ${isDoctorFixed ? "review-card-relieved" : ""}">
        <div class="review-card-agent"></div>
        <div class="review-card-text">${text}</div>
      </div>`;
    }
  }

  // ── Upstream evidence ─────────────────────────────────────────────────
  if (upstreamBlockers.length) {
    h += `<details style="margin-top:10px"><summary style="cursor:pointer;font-size:12px;color:var(--muted)">Upstream blockers (${upstreamBlockers.length})</summary>`;
    h += `<ul style="margin-top:6px">${upstreamBlockers.map((item) => `<li>${_tabEscape(_tabText(item))}</li>`).join("")}</ul></details>`;
  }
  if (upstreamWarnings.length) {
    h += `<details><summary style="cursor:pointer;font-size:12px;color:var(--muted)">Upstream warnings (${upstreamWarnings.length})</summary>`;
    h += `<ul style="margin-top:6px">${upstreamWarnings.map((item) => `<li>${_tabEscape(_tabText(item))}</li>`).join("")}</ul></details>`;
  }

  // ── Release notes ─────────────────────────────────────────────────────
  if (releaseNotes.length) {
    h += `<h4 style="margin-top:14px">Release notes</h4><ul>${releaseNotes.map((item) => `<li>${_tabEscape(_tabText(item))}</li>`).join("")}</ul>`;
  }
  if (source.rollbackPlan) {
    h += `<h4>Rollback</h4><p>${_tabEscape(source.rollbackPlan)}</p>`;
  }

  // ── Raw JSON ──────────────────────────────────────────────────────────
  h += `<details style="margin-top:10px"><summary class="muted">Raw JSON</summary><pre>${_tabEscape(JSON.stringify(source, null, 2))}</pre></details>`;

  target.innerHTML = h;
}

function renderChangesTab(run) {
  const list = document.getElementById("changesFileList");
  const viewer = document.getElementById("changesDiffViewer");
  if (!list) return;
  const changed = Array.isArray(run?.changedFiles) ? run.changedFiles : [];
  if (!changed.length) {
    list.innerHTML = `<p class="muted">${state.running ? "Đang theo dõi thay đổi…" : "Chưa có thay đổi"}</p>`;
    if (viewer) viewer.innerHTML = `<p class="muted">Chọn file để xem diff</p>`;
    return;
  }
  list.innerHTML = changed.map((file, index) => {
    const filePath = typeof file === "string" ? file : (file.path || "");
    const status = typeof file === "object" ? (file.status || "changed") : "changed";
    return `<button type="button" class="change-file" data-idx="${index}"><span class="change-stat change-${_tabEscape(status)}">${_tabEscape(status)}</span><code>${_tabEscape(filePath)}</code></button>`;
  }).join("");

  list.querySelectorAll(".change-file").forEach((element) => {
    element.addEventListener("click", async () => {
      list.querySelectorAll(".change-file").forEach((item) => item.classList.remove("active"));
      element.classList.add("active");
      if (!viewer) return;
      const file = changed[Number(element.dataset.idx)];
      const filePath = typeof file === "string" ? file : (file.path || "");
      const status = typeof file === "object" ? (file.status || "changed") : "changed";
      const inlineDiff = typeof file === "object" ? (file.diff || file.patch) : null;
      const requestId = `${Date.now()}-${element.dataset.idx}`;
      viewer.dataset.requestId = requestId;
      if (inlineDiff) {
        viewer.innerHTML = `<h4>${_tabEscape(filePath)}</h4><pre class="diff-pre">${_tabEscape(inlineDiff)}</pre>`;
        return;
      }
      if (typeof appApi.getWorkspaceDiff !== "function" || !state.activeSession?.id) {
        viewer.innerHTML = `<h4>${_tabEscape(filePath)}</h4><p class="muted">Run cũ không lưu patch; diff theo workspace chỉ có trong app desktop.</p>`;
        return;
      }
      viewer.innerHTML = `<h4>${_tabEscape(filePath)}</h4><p class="muted">Đang đọc diff hiện tại…</p>`;
      try {
        const payload = await appApi.getWorkspaceDiff(state.activeSession.id, filePath, status);
        if (viewer.dataset.requestId !== requestId) return;
        const content = payload?.diff || payload?.content || payload?.message || "Không có diff để hiển thị.";
        viewer.innerHTML = `<h4>${_tabEscape(filePath)}</h4>${payload?.kind === "message"
          ? `<p class="muted">${_tabEscape(content)}</p>`
          : `<pre class="diff-pre">${_tabEscape(content)}</pre>`}`;
      } catch (error) {
        if (viewer.dataset.requestId !== requestId) return;
        viewer.innerHTML = `<h4>${_tabEscape(filePath)}</h4><p class="muted">Không đọc được diff: ${_tabEscape(error.message)}</p>`;
      }
    });
  });
}

function _allLogEvents() {
  const runEvents = _runEvents(latestRun());
  const backendEvents = Array.isArray(state.observability?.recentEvents) ? state.observability.recentEvents : [];
  const unique = new Map();
  [...backendEvents, ...runEvents].forEach((event, index) => {
    if (!event || typeof event !== "object") return;
    const key = [event.at || event.timestamp || index, event.eventType || event.type || event.stage, event.node, event.detail || event.error].join("|");
    unique.set(key, event);
  });
  return [...unique.values()].sort((left, right) => {
    const a = Date.parse(left.at || left.timestamp || "") || 0;
    const b = Date.parse(right.at || right.timestamp || "") || 0;
    return a - b;
  });
}

function renderLogTab() {
  // Only do the expensive work (filter dropdown rebuild, dedup, DOM write)
  // when the log tab is actually visible.  This is called on every progress
  // event via renderDashboard → fillTabsFromRun, even while the user is
  // watching another tab.
  const tabPanel = document.getElementById("tab-log");
  if (tabPanel && !tabPanel.classList.contains("active")) return;
  const list = document.getElementById("logList");
  if (!list) return;
  const filterNode = document.getElementById("logFilterNode");
  const filterType = document.getElementById("logFilterType");
  const debugToggle = document.getElementById("logToggleDebug");
  const events = _allLogEvents().map((event) => ({
    event,
    node: String(event.node || event.stage || event.agent || event.role || ""),
    type: String(event.eventType || event.type || (event.stage ? "progress" : "event"))
  }));

  const selectedNode = filterNode?.value || "";
  const selectedType = filterType?.value || "";
  const nodes = [...new Set(events.map((item) => item.node).filter(Boolean))].sort();
  const types = [...new Set(events.map((item) => item.type).filter(Boolean))].sort();
  if (filterNode) {
    filterNode.innerHTML = `<option value="">Tất cả node</option>` + nodes.map((node) =>
      `<option value="${_tabEscape(node)}"${node === selectedNode ? " selected" : ""}>${_tabEscape(node)}</option>`).join("");
  }
  if (filterType) {
    filterType.innerHTML = `<option value="">Tất cả loại</option>` + types.map((type) =>
      `<option value="${_tabEscape(type)}"${type === selectedType ? " selected" : ""}>${_tabEscape(type)}</option>`).join("");
  }
  const nodeFilter = nodes.includes(selectedNode) ? selectedNode : "";
  const typeFilter = types.includes(selectedType) ? selectedType : "";
  const filtered = events.filter((item) => (!nodeFilter || item.node === nodeFilter) && (!typeFilter || item.type === typeFilter));
  if (!filtered.length) {
    list.innerHTML = `<p class="muted">${events.length ? "Không có sự kiện khớp bộ lọc" : "Chưa có sự kiện"}</p>`;
    return;
  }
  const showDebug = Boolean(debugToggle?.checked);
  list.innerHTML = filtered.slice(-300).map(({ event, node, type }) => {
    const time = event.at || event.timestamp ? formatTime(event.at || event.timestamp) : "";
    const detail = event.detail || event.error || event.taskPreview || event.message || "";
    return `<div class="log-entry">
      <span class="ts">${_tabEscape(time)}</span>
      <span class="node">${_tabEscape(node)}</span>
      <span class="type">${_tabEscape(type)}</span>
      <span>${_tabEscape(compactText(detail, 320))}</span>
      ${showDebug ? `<details class="debug-json visible"><summary>JSON</summary><pre>${_tabEscape(JSON.stringify(event, null, 2))}</pre></details>` : ""}
    </div>`;
  }).join("");
}

function updateTabBadges(run) {
  const setBadge = (id, count) => {
    const element = document.getElementById(id);
    if (!element) return;
    const safeCount = Number.isFinite(Number(count)) ? Number(count) : 0;
    if (safeCount > 0) {
      element.textContent = String(safeCount);
      element.style.display = "";
    } else {
      element.style.display = "none";
    }
  };
  const executionEvents = _runEvents(run).filter((event) =>
    /^(workspace|load_context|context|setup|openhands_worker|coder|tester_agent|tester|verify|execute|rework|security_reviewer_agent|code_reviewer_agent|release_deploy_agent|reviewer_decision|execution_gate)/i.test(String(event.stage || event.type || ""))
  );
  const aggregate = run?.review || {};
  const issueCount = (Array.isArray(aggregate.blockers) ? aggregate.blockers.length : 0)
    + (Array.isArray(aggregate.warnings) ? aggregate.warnings.length : 0);
  setBadge("execBadge", executionEvents.length || (Array.isArray(run?.commandResults) ? run.commandResults.length : 0));
  setBadge("reviewBadge", issueCount);
  setBadge("changesBadge", Array.isArray(run?.changedFiles) ? run.changedFiles.length : 0);
}

// ── Doctor tab ────────────────────────────────────────────────────────────────
const _doctorState = { running: false, lastResult: null, eventCount: 0 };

function _doctorTime() {
  return new Date().toISOString().slice(11, 19);
}

function _doctorStageClass(stage) {
  if (!stage) return "";
  if (stage.endsWith(".applied")) return "applied";
  if (stage.endsWith(".skip")) return "skip";
  if (stage.endsWith(".error") || stage === "doctor.error") return "err";
  return "";
}

function _doctorAppendEvent(stage, detail) {
  const list = document.getElementById("doctorEvents");
  if (!list) return;
  if (list.querySelector(".muted") && _doctorState.eventCount === 0) list.innerHTML = "";
  _doctorState.eventCount++;
  const row = document.createElement("div");
  row.className = "ev";
  row.innerHTML = `<span class="ev-time">${_tabEscape(_doctorTime())}</span>` +
    `<span class="ev-stage ${_doctorStageClass(stage)}">${_tabEscape(stage)}</span>` +
    `<span>${_tabEscape(compactText(detail || "", 600))}</span>`;
  list.appendChild(row);
  list.scrollTop = list.scrollHeight;
}

function _doctorAppendChunk(token) {
  const pre = document.getElementById("doctorPatchStream");
  if (!pre) return;
  const muted = pre.querySelector(".muted");
  if (muted) pre.innerHTML = "";
  pre.appendChild(document.createTextNode(token));
  pre.scrollTop = pre.scrollHeight;
}

function _doctorResetStream() {
  const pre = document.getElementById("doctorPatchStream");
  if (pre) pre.innerHTML = `<span class="muted">— chờ stream —</span>`;
  const list = document.getElementById("doctorEvents");
  if (list) list.innerHTML = `<p class="muted">Sự kiện sẽ hiện ở đây khi Doctor chạy.</p>`;
  const summary = document.getElementById("doctorSummary");
  if (summary) { summary.textContent = ""; summary.className = "doctor-summary"; }
  _doctorState.eventCount = 0;
}

function _doctorRenderSummary(result) {
  const summary = document.getElementById("doctorSummary");
  if (!summary) return;
  const scan = result?.scan || {};
  const fix = result?.fix || {};
  const verify = result?.verify || {};
  const issuesCount = (scan.issues || []).length;
  const applied = (fix.applied || []).length;
  const skipped = (fix.skipped || []).length;
  const ok = !!result?.ok;
  summary.className = `doctor-summary ${ok ? "pass" : "fail"}`;
  summary.innerHTML = `<strong>${ok ? "✅ PASS" : "❌ FAIL"}</strong> · ${issuesCount} issue · ${applied} fix · ${skipped} skip · ${(verify.runs || []).length} command<br>` +
    (verify.runs || []).map((r) => `<code>${_tabEscape((r.command || []).join(" "))}</code> → exit ${r.code}${r.ok ? " ✓" : " ✗"}`).join("<br>");
  const badge = document.getElementById("doctorBadge");
  if (badge) {
    if (issuesCount > 0) { badge.textContent = String(issuesCount); badge.style.display = ""; }
    else badge.style.display = "none";
  }
}

async function _doctorRun() {
  if (_doctorState.running) return;
  const btn = document.getElementById("doctorRunBtn");
  const status = document.getElementById("doctorStatus");
  const session = state.activeSession;
  if (!session?.workspacePath) {
    if (status) status.textContent = "Chưa chọn workspace.";
    return;
  }
  _doctorResetStream();
  _doctorState.running = true;
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Đang chạy chẩn đoán…";
  try {
    const out = await appApi.runDoctor({ sessionId: session.id, workspacePath: session.workspacePath });
    _doctorState.lastResult = out?.result || null;
    _doctorRenderSummary(out?.result || {});
    if (status) status.textContent = out?.ok ? "Xong: PASS" : "Xong: có vấn đề";
  } catch (error) {
    if (status) status.textContent = `Lỗi: ${error.message}`;
    _doctorAppendEvent("doctor.error", error.message);
  } finally {
    _doctorState.running = false;
    if (btn) btn.disabled = false;
  }
}

function _doctorBindIpc() {
  if (typeof appApi.onDoctorEvent !== "function") return;
  appApi.onDoctorEvent((message) => {
    if (!message) return;
    if (message.type === "progress") {
      if (message.stage === "doctor.patch.chunk") {
        _doctorAppendChunk(message.detail || "");
      } else {
        _doctorAppendEvent(message.stage, message.detail);
      }
    } else if (message.type === "doctor.complete" || message.type === "doctor.result") {
      if (message.result) _doctorRenderSummary(message.result);
    } else if (message.type === "doctor.error") {
      _doctorAppendEvent("doctor.error", message.error || "");
    }
  });
  const btn = document.getElementById("doctorRunBtn");
  if (btn) btn.addEventListener("click", _doctorRun);
}

// Start when DOM is ready
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => init().catch((error) => {
    console.error("init() failed:", error);
  }));
} else {
  init().catch((error) => {
    console.error("init() failed:", error);
  });
}
