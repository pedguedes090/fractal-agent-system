const appApi = window.agentApp;

const state = {
  settings: null,
  sessions: [],
  activeSession: null,
  progress: [],
  streamLines: [],
  running: false
};

const STREAM_LINE_LIMIT = 120;

const elements = {
  workspaceLabel: document.querySelector("#workspaceLabel"),
  chooseFolderBtn: document.querySelector("#chooseFolderBtn"),
  sessionSelect: document.querySelector("#sessionSelect"),
  newSessionBtn: document.querySelector("#newSessionBtn"),
  serverInput: document.querySelector("#serverInput"),
  modelInput: document.querySelector("#modelInput"),
  autoConfirmInput: document.querySelector("#autoConfirmInput"),
  saveSettingsBtn: document.querySelector("#saveSettingsBtn"),
  saveStatus: document.querySelector("#saveStatus"),
  messages: document.querySelector("#messages"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#messageInput"),
  sendBtn: document.querySelector("#sendBtn"),
  progressList: document.querySelector("#progressList"),
  runState: document.querySelector("#runState")
};

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit"
  });
}

function getWorkspacePath() {
  return state.activeSession?.workspacePath || "";
}

function stageLabel(stage) {
  const labels = {
    preflight: "Preflight",
    task_intent: "Task intent",
    codegraph_context: "CodeGraph",
    codegraph_affected: "CodeGraph",
    intake_user_intent: "Intake",
    intake_ambiguity: "Ambiguity",
    intake_repo_context: "Repo context",
    intake_synthesizer: "Synthesis",
    planning_minimal: "Planning",
    planning_robust: "Planning",
    planning_test_first: "Planning",
    critique_risk: "Critique",
    critique_test_coverage: "Critique",
    critique_security_regression: "Critique",
    plan_arbiter: "Plan",
    human_gate: "Gate",
    context: "Context",
    openhands_worker: "OpenHands",
    openhands_context: "OpenHands",
    openhands_plugins: "Plugins",
    openhands_mcp: "MCP",
    openhands_message: "Message",
    openhands_action: "Action",
    openhands_observation: "Observation",
    automated_review: "Review",
    reporter: "Reporter",
    done: "Done",
    error: "Error"
  };
  return labels[stage] || String(stage || "Step").replaceAll("_", " ");
}

function formatProgressLine(progress) {
  const time = formatTime(progress.at);
  const label = stageLabel(progress.stage);
  const detail = progress.detail ? `: ${progress.detail}` : "";
  return `${time ? `${time}  ` : ""}${label}${detail}`;
}

function pushStreamLine(progress) {
  state.streamLines.push(formatProgressLine(progress));
  if (state.streamLines.length > STREAM_LINE_LIMIT) {
    state.streamLines = state.streamLines.slice(-STREAM_LINE_LIMIT);
  }
}

function setStatus(text, timeout = 1800) {
  elements.saveStatus.textContent = text;
  if (timeout) {
    setTimeout(() => {
      if (elements.saveStatus.textContent === text) elements.saveStatus.textContent = "";
    }, timeout);
  }
}

function renderSettings() {
  elements.serverInput.value = state.settings?.serverUrl || "";
  elements.modelInput.value = state.settings?.model || "";
  elements.autoConfirmInput.checked = Boolean(state.settings?.autoConfirmHumanGate);
}

function renderWorkspace() {
  const workspacePath = getWorkspacePath();
  elements.workspaceLabel.textContent = workspacePath || "Chưa mở thư mục";
}

function renderSessions() {
  elements.sessionSelect.innerHTML = "";
  if (!state.sessions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Chưa có phiên";
    elements.sessionSelect.appendChild(option);
    return;
  }

  for (const session of state.sessions) {
    const option = document.createElement("option");
    option.value = session.id;
    option.textContent = session.title || "Phiên mới";
    elements.sessionSelect.appendChild(option);
  }

  elements.sessionSelect.value = state.activeSession?.id || state.sessions[0]?.id || "";
}

function renderMessages() {
  const messages = [...(state.activeSession?.messages || [])];
  if (state.streamLines.length) {
    messages.push({
      id: "active-stream",
      role: "assistant",
      content: state.streamLines.join("\n"),
      streaming: true,
      createdAt: new Date().toISOString()
    });
  }
  elements.messages.innerHTML = "";

  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Chọn thư mục, nhập yêu cầu, rồi gửi.";
    elements.messages.appendChild(empty);
    return;
  }

  for (const message of messages) {
    const item = document.createElement("article");
    item.className = `message ${message.role} ${message.error ? "error" : ""} ${message.streaming ? "streaming" : ""}`;

    const meta = document.createElement("div");
    meta.className = "message-meta";
    if (message.streaming) {
      meta.textContent = "Agent đang chạy";
    } else {
      meta.textContent = message.role === "user" ? `Bạn · ${formatTime(message.createdAt)}` : `Agent · ${formatTime(message.createdAt)}`;
    }

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = message.content;

    item.append(meta, bubble);
    elements.messages.appendChild(item);
  }

  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function renderProgress() {
  elements.progressList.innerHTML = "";
  elements.runState.textContent = state.running ? "Đang chạy" : "Sẵn sàng";

  if (!state.progress.length) {
    const empty = document.createElement("div");
    empty.className = "progress-detail";
    empty.textContent = "Chưa có bước chạy.";
    elements.progressList.appendChild(empty);
    return;
  }

  for (const item of state.progress.slice(-18)) {
    const row = document.createElement("div");
    row.className = `progress-item ${item.stage === "done" || item.stage === "error" ? "" : "active"}`;

    const stage = document.createElement("div");
    stage.className = "progress-stage";
    stage.textContent = item.stage;

    const detail = document.createElement("div");
    detail.className = "progress-detail";
    detail.textContent = item.detail;

    row.append(stage, detail);
    elements.progressList.appendChild(row);
  }

  elements.progressList.scrollTop = elements.progressList.scrollHeight;
}

function renderControls() {
  const disabled = state.running;
  elements.chooseFolderBtn.disabled = disabled;
  elements.newSessionBtn.disabled = disabled;
  elements.sessionSelect.disabled = disabled;
  elements.saveSettingsBtn.disabled = disabled;
  elements.serverInput.disabled = disabled;
  elements.modelInput.disabled = disabled;
  elements.autoConfirmInput.disabled = disabled;
  elements.messageInput.disabled = disabled;
  elements.sendBtn.disabled = disabled;
}

function render() {
  renderSettings();
  renderWorkspace();
  renderSessions();
  renderMessages();
  renderProgress();
  renderControls();
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
    serverUrl: elements.serverInput.value,
    model: elements.modelInput.value,
    autoConfirmHumanGate: elements.autoConfirmInput.checked
  });
  renderSettings();
  setStatus("Đã lưu");
}

async function chooseWorkspace() {
  const workspacePath = await appApi.chooseWorkspace();
  if (!workspacePath) return;
  const session = await ensureSession();
  const result = await appApi.updateSessionWorkspace(session.id, workspacePath);
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  state.progress = [];
  state.streamLines = [];
  render();
}

async function createSession() {
  const result = await appApi.createSession({ workspacePath: getWorkspacePath() });
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  state.progress = [];
  state.streamLines = [];
  render();
  elements.messageInput.focus();
}

async function loadSession(sessionId) {
  if (!sessionId || sessionId === state.activeSession?.id) return;
  const session = await appApi.loadSession(sessionId);
  if (!session) return;
  state.activeSession = session;
  state.progress = [];
  state.streamLines = [];
  render();
}

function addOptimisticMessage(content) {
  const current = state.activeSession || { messages: [] };
  state.activeSession = {
    ...current,
    messages: [
      ...(current.messages || []),
      {
        id: `local-${Date.now()}`,
        role: "user",
        content,
        createdAt: new Date().toISOString()
      }
    ]
  };
}

async function sendMessage(event) {
  event.preventDefault();
  const content = elements.messageInput.value.trim();
  if (!content || state.running) return;

  await saveSettings();
  const session = await ensureSession();
  if (!getWorkspacePath()) {
    setStatus("Hãy mở thư mục trước", 2600);
    return;
  }

  state.running = true;
  state.progress = [];
  state.streamLines = ["Bắt đầu chạy pipeline..."];
  elements.messageInput.value = "";
  elements.messageInput.style.height = "auto";
  addOptimisticMessage(content);
  render();

  try {
    const result = await appApi.sendMessage({
      sessionId: session.id,
      workspacePath: getWorkspacePath(),
      settings: state.settings,
      content
    });
    state.activeSession = result.session;
    state.sessions = result.sessions.sessions;
  } catch (error) {
    state.activeSession = {
      ...state.activeSession,
      messages: [
        ...(state.activeSession?.messages || []),
        {
          id: `error-${Date.now()}`,
          role: "assistant",
          content: `Mình chưa gửi được yêu cầu: ${error.message}`,
          error: true,
          createdAt: new Date().toISOString()
        }
      ]
    };
  } finally {
    state.running = false;
    state.streamLines = [];
    render();
    elements.messageInput.focus();
  }
}

function autoResizeInput() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 170)}px`;
}

async function init() {
  const initial = await appApi.getInitialState();
  state.settings = initial.settings;
  state.sessions = initial.sessions.sessions || [];
  state.activeSession = initial.activeSession || null;

  appApi.onProgress((progress) => {
    if (state.activeSession && progress.sessionId !== state.activeSession.id) return;
    if (!state.running && !state.streamLines.length) return;
    state.progress.push(progress);
    pushStreamLine(progress);
    renderProgress();
    renderMessages();
    renderControls();
  });

  render();
  elements.messageInput.focus();
}

elements.chooseFolderBtn.addEventListener("click", chooseWorkspace);
elements.newSessionBtn.addEventListener("click", createSession);
elements.saveSettingsBtn.addEventListener("click", saveSettings);
elements.sessionSelect.addEventListener("change", (event) => loadSession(event.target.value));
elements.composer.addEventListener("submit", sendMessage);
elements.messageInput.addEventListener("input", autoResizeInput);
elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

init().catch((error) => {
  elements.messages.textContent = `Không khởi động được giao diện: ${error.message}`;
});
