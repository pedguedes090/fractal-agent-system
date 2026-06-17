const path = require("path");
const crypto = require("crypto");
const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const { SettingsStore } = require("./settingsStore");
const { SessionStore } = require("./sessionStore");
const { runPythonAgentPipeline } = require("./pythonEngine");

let mainWindow;
let settingsStore;
let sessionStore;
const activeRuns = new Set();

function isConfirmationText(text) {
  const normalized = String(text || "").trim().toLowerCase();
  return ["xác nhận", "xac nhan", "confirm", "ok", "đồng ý", "dong y"].includes(normalized);
}

function findPendingHumanGateTask(session) {
  const runs = Array.isArray(session?.runs) ? session.runs : [];
  for (let index = runs.length - 1; index >= 0; index -= 1) {
    const run = runs[index];
    if (run?.humanGate?.status !== "pending") continue;
    const task = run.humanGate.originalTask || run.task || run.problem?.problemStatement;
    if (task) return { task: String(task), runIndex: index };
  }
  return null;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 780,
    minWidth: 920,
    minHeight: 620,
    title: "He Thong Agent",
    backgroundColor: "#f7f5ef",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
}

function getInitialState() {
  const sessions = sessionStore.list();
  const activeSession = sessions.activeSessionId ? sessionStore.get(sessions.activeSessionId) : null;
  return {
    settings: settingsStore.get(),
    sessions,
    activeSession
  };
}

function registerIpc() {
  ipcMain.handle("app:getInitialState", () => getInitialState());

  ipcMain.handle("settings:save", (_event, nextSettings) => {
    return settingsStore.save(nextSettings || {});
  });

  ipcMain.handle("workspace:choose", async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "Chọn thư mục làm việc",
      properties: ["openDirectory", "createDirectory"]
    });
    if (result.canceled || !result.filePaths.length) return null;
    return result.filePaths[0];
  });

  ipcMain.handle("sessions:list", () => sessionStore.list());

  ipcMain.handle("sessions:create", (_event, initial) => {
    const session = sessionStore.create(initial || {});
    return {
      session,
      sessions: sessionStore.list()
    };
  });

  ipcMain.handle("sessions:load", (_event, sessionId) => {
    return sessionStore.get(sessionId);
  });

  ipcMain.handle("sessions:updateWorkspace", (_event, sessionId, workspacePath) => {
    const session = sessionStore.get(sessionId) || sessionStore.create({ workspacePath });
    session.workspacePath = workspacePath || "";
    const saved = sessionStore.save(session);
    return {
      session: saved,
      sessions: sessionStore.list()
    };
  });

  ipcMain.handle("sessions:delete", (_event, sessionId) => {
    return sessionStore.delete(sessionId);
  });

  ipcMain.handle("agent:send", async (event, payload) => {
    const settings = settingsStore.save(payload?.settings || settingsStore.get());
    let session = payload?.sessionId ? sessionStore.get(payload.sessionId) : null;
    if (!session) {
      session = sessionStore.create({
        title: payload?.content,
        workspacePath: payload?.workspacePath || ""
      });
    }

    if (activeRuns.has(session.id)) {
      throw new Error("Phiên này đang chạy. Vui lòng chờ kết quả hiện tại.");
    }

    const originalContent = String(payload?.content || "").trim();
    const pendingHumanGate = isConfirmationText(originalContent) ? findPendingHumanGateTask(session) : null;
    const engineContent = pendingHumanGate
      ? `${pendingHumanGate.task}\n\nxác nhận`
      : originalContent;

    const userMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: originalContent,
      createdAt: new Date().toISOString()
    };
    if (!userMessage.content) throw new Error("Bạn chưa nhập yêu cầu.");

    session.workspacePath = payload?.workspacePath || session.workspacePath || "";
    if (pendingHumanGate && Array.isArray(session.runs)) {
      session.runs = session.runs.map((run, index) =>
        index === pendingHumanGate.runIndex
          ? {
              ...run,
              humanGate: {
                ...run.humanGate,
                status: "approved",
                approvedAt: new Date().toISOString()
              }
            }
          : run
      );
    }
    session.messages = [...(session.messages || []), userMessage];
    session = sessionStore.save(session);

    activeRuns.add(session.id);
    const emitProgress = (progress) => {
      event.sender.send("agent:progress", {
        sessionId: session.id,
        ...progress
      });
    };
    if (pendingHumanGate) {
      emitProgress({
        stage: "human_gate",
        detail: "Resuming approved high-risk task",
        at: new Date().toISOString()
      });
    }

    try {
      const run = await runPythonAgentPipeline({
        settings,
        workspacePath: session.workspacePath,
        messages: session.messages,
        userText: engineContent,
        sessionId: session.id,
        emitProgress
      });

      const assistantMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: run.assistantText,
        runId: run.id,
        createdAt: new Date().toISOString()
      };

      session.messages = [...session.messages, assistantMessage];
      session.runs = [...(session.runs || []), run];
      session.title = session.title === "Phiên mới" ? userMessage.content.slice(0, 48) : session.title;
      session = sessionStore.save(session);

      emitProgress({
        stage: "done",
        detail: "Hoàn tất",
        at: new Date().toISOString()
      });

      return {
        session,
        sessions: sessionStore.list(),
        run
      };
    } catch (error) {
      const assistantMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: `Mình chưa chạy xong được: ${error.message}`,
        createdAt: new Date().toISOString(),
        error: true
      };
      session.messages = [...session.messages, assistantMessage];
      session = sessionStore.save(session);
      emitProgress({
        stage: "error",
        detail: error.message,
        at: new Date().toISOString()
      });
      return {
        session,
        sessions: sessionStore.list(),
        error: error.message
      };
    } finally {
      activeRuns.delete(session.id);
    }
  });
}

app.whenReady().then(() => {
  settingsStore = new SettingsStore(app.getPath("userData"));
  sessionStore = new SessionStore(app.getPath("userData"));
  registerIpc();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
