const path = require("path");
const crypto = require("crypto");
const { app, BrowserWindow, dialog, ipcMain } = require("electron");

// On Windows the default shader cache lives under %LOCALAPPDATA%\<app>\GPUCache
// and Chromium sometimes can't move it on first launch ("Unable to move the cache:
// Access is denied"). Forcing in-process-gpu / disable-gpu-sandbox was used to
// silence those errors, but on Windows 11 with hardware acceleration that combo
// makes the GPU process crash *after* the renderer paints — the JS runs, the DOM
// is built, and then the window stays black because the framebuffer is never
// presented. Set AGENT_DISABLE_HW_ACCEL=1 if you need to opt out instead.
if (process.env.AGENT_DISABLE_HW_ACCEL === "1") {
  app.disableHardwareAcceleration();
}

const { AppDatabase } = require("./appDatabase");
const { AgentBackendService } = require("./backendService");
const { SettingsStore } = require("./settingsStore");
const { SessionStore } = require("./sessionStore");
const { getWorkspaceDiff } = require("./workspaceDiff");
const { execFileSync, spawn: childSpawn } = require("child_process");

// ── UAC self-elevation (Windows) ──────────────────────────────────────────
// Cheap synchronous check: `net session` requires admin privileges on Windows;
// it exits 0 silently when elevated and prints "Access is denied" + exits non-zero
// otherwise. Cached after first call.
let _isElevatedCache = null;
function isElevatedWindows() {
  if (_isElevatedCache !== null) return _isElevatedCache;
  if (process.platform !== "win32") { _isElevatedCache = false; return false; }
  try {
    execFileSync("net", ["session"], { stdio: "ignore", windowsHide: true, timeout: 3000 });
    _isElevatedCache = true;
  } catch {
    _isElevatedCache = false;
  }
  return _isElevatedCache;
}

// Relaunch the current Electron app through PowerShell's "Start-Process -Verb RunAs"
// which triggers the UAC consent prompt. The new process inherits the user's
// Administrator token; this process then exits cleanly.
//
// Caller MUST ensure require_admin is the user's explicit choice (Full Power
// panel). We refuse to elevate if AGENT_NO_ELEVATE=1 so a dev can debug
// without the UAC dialog stealing focus on every relaunch.
function relaunchElevatedWindows() {
  if (process.env.AGENT_NO_ELEVATE === "1") {
    console.warn("AGENT_NO_ELEVATE=1 — skipping UAC relaunch.");
    return false;
  }
  try {
    const exe = process.execPath;
    // In dev mode (`electron .`), argv = [electron.exe, ".", ...userArgs] and
    // cwd is the project root. The relaunched elevated Electron loses its cwd
    // (Start-Process defaults to System32) so we MUST pass -WorkingDirectory.
    //
    // For packaged builds, process.execPath IS the app exe and argv[1..] is
    // safe to forward.
    //
    // We also need to detect dev mode and forward "." (or the resolved app
    // path) as the first arg so elevated electron knows where to find
    // package.json + main.js. Heuristic: process.defaultApp is true when
    // running via the system electron CLI.
    const isDev = !!process.defaultApp;
    const cwd = process.cwd();
    const forwardArgs = isDev
      // Dev: skip argv[1] (which was "." resolved from current cwd) and
      // explicitly pass the cwd as the app entry so it resolves correctly
      // even when Start-Process changes the working directory.
      ? [cwd, ...process.argv.slice(2)]
      : process.argv.slice(1);

    const quote = (s) => `'${String(s).replace(/'/g, "''")}'`;
    const psParts = [
      `-FilePath ${quote(exe)}`,
      `-WorkingDirectory ${quote(cwd)}`,
      "-Verb RunAs"
    ];
    if (forwardArgs.length) {
      psParts.push(`-ArgumentList ${forwardArgs.map(quote).join(",")}`);
    }
    const psCommand = `Start-Process ${psParts.join(" ")}`;
    childSpawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", psCommand], {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
      cwd
    }).unref();
    return true;
  } catch (err) {
    console.error("Elevation relaunch failed:", err);
    return false;
  }
}

let mainWindow;
let appDatabase;
let backendService;
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
    if (task) {
      return {
        ...run.humanGate,
        task: String(task),
        originalTask: String(task),
        correlationId: run.humanGate.correlationId || run.correlationId || null,
        executionId: run.humanGate.executionId || run.executionId || run.id || null,
        runIndex: index
      };
    }
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
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false  // must be false for preload to use Node require()
    }
  });

  // Fallback: force-show window after 2s even if ready-to-show hasn't fired
  const forceShowTimer = setTimeout(() => {
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) {
      console.warn("[main] Force-showing window after timeout");
      mainWindow.show();
      mainWindow.webContents.openDevTools({ mode: "bottom" });
    }
  }, 2000);

  mainWindow.once("ready-to-show", () => {
    clearTimeout(forceShowTimer);
    mainWindow.show();
  });

  // Capture renderer console errors
  mainWindow.webContents.on("console-message", (_event, _level, message, _line, _sourceId) => {
    const prefix = _level >= 3 ? "[ERROR]" : _level >= 2 ? "[WARN]" : "[LOG]";
    console.log(`[renderer] ${prefix} ${message}`);
  });

  mainWindow.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    console.error("[renderer] did-fail-load:", errorCode, errorDescription, validatedURL);
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(`data:text/html,<html><body style="background:#fef2f2;padding:40px;font-family:sans-serif"><h2>Load failed</h2><p>${errorDescription} (${errorCode})</p><p>URL: ${validatedURL}</p></body></html>`);
    }
  });

  mainWindow.webContents.on("preload-error", (_event, _preloadPath, error) => {
    console.error("[preload-error]", error);
  });

  // Open DevTools in dev mode to catch errors
  if (process.env.AGENT_DEVTOOLS === "1") {
    mainWindow.webContents.openDevTools({ mode: "bottom" });
  }

  const indexPath = path.join(__dirname, "../renderer/index.html");
  console.log("[main] Loading:", indexPath, "exists:", require("fs").existsSync(indexPath));
  mainWindow.loadFile(indexPath).catch((err) => {
    console.error("[main] loadFile failed:", err.message);
  });
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

  ipcMain.handle("workspace:diff", async (_event, sessionId, relativePath, status) => {
    const session = sessionStore.get(sessionId);
    if (!session?.workspacePath) throw new Error("Phiên không có workspace hợp lệ.");
    const latestRun = Array.isArray(session.runs) ? session.runs[session.runs.length - 1] : null;
    const allowedPaths = new Set((latestRun?.changedFiles || []).map((file) =>
      String(typeof file === "string" ? file : (file?.path || "")).replace(/\\/g, "/")
    ));
    const requestedPath = String(relativePath || "").replace(/\\/g, "/");
    if (!allowedPaths.has(requestedPath)) throw new Error("File không thuộc thay đổi của run gần nhất.");
    return getWorkspaceDiff(session.workspacePath, requestedPath, status);
  });

  ipcMain.handle("sessions:delete", (_event, sessionId) => {
    return sessionStore.delete(sessionId);
  });

  ipcMain.handle("agent:observability", async () => {
    return backendService.getObservability();
  });

  ipcMain.handle("agent:autonomy-status", async () => {
    return backendService.getAutonomyStatus();
  });

  ipcMain.handle("agent:autonomy-scan", async (_event, payload) => {
    return backendService.runAutonomyScan({ workspacePath: payload?.workspacePath || "" });
  });

  ipcMain.handle("agent:full-power-status", async () => {
    const s = settingsStore.get();
    return {
      isElevated: process.platform === "win32" ? isElevatedWindows() : false,
      platform: process.platform,
      fullPower: s?.fullPower || { bypassSafeCommands: false, requireAdmin: false, autoLoopAllowAdmin: false }
    };
  });

  ipcMain.handle("agent:autonomy-next-task", async (_event, payload) => {
    return backendService.requestAutonomyNextTask({
      workspacePath: payload?.workspacePath || "",
      completedIds: payload?.completedIds || [],
      ideaCursor: payload?.ideaCursor || 0,
      rescanIfStale: !!payload?.rescanIfStale,
    });
  });

  ipcMain.handle("agent:topology", async () => {
    return backendService.getTopology();
  });

  ipcMain.handle("agent:cancel", async (_event, executionId) => {
    return backendService.cancelRun(executionId);
  });

  ipcMain.handle("doctor:run", async (event, payload) => {
    const settings = settingsStore.get();
    const session = payload?.sessionId ? sessionStore.get(payload.sessionId) : null;
    const workspacePath = payload?.workspacePath || session?.workspacePath || "";
    if (!workspacePath) throw new Error("workspacePath is required");
    const emitEvent = (message) => {
      try {
        event.sender.send("doctor:event", message);
      } catch {
        /* renderer may be gone; ignore */
      }
    };
    try {
      const result = await backendService.runDoctor({
        workspacePath,
        sessionId: session?.id || null,
        apiKey: settings.apiKey || "",
        model: settings.model || "",
        emitEvent
      });
      emitEvent({ type: "doctor.complete", ok: !!result?.ok, result });
      return { ok: !!result?.ok, result };
    } catch (error) {
      emitEvent({ type: "doctor.error", error: error.message });
      throw error;
    }
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
    const pendingHumanGate = isConfirmationText(originalContent)
      ? sessionStore.getPendingApproval(session.id) || findPendingHumanGateTask(session)
      : null;
    const engineContent = pendingHumanGate
      ? pendingHumanGate.originalTask || pendingHumanGate.task
      : originalContent;

    const userMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: originalContent,
      createdAt: new Date().toISOString()
    };
    if (!userMessage.content) throw new Error("Bạn chưa nhập yêu cầu.");

    session.workspacePath = payload?.workspacePath || session.workspacePath || "";
    let approvedAt = null;
    if (pendingHumanGate?.id) {
      approvedAt = sessionStore.approvePendingApproval(session.id, pendingHumanGate.id);
      session = sessionStore.get(session.id) || session;
    } else if (pendingHumanGate && Array.isArray(session.runs)) {
      approvedAt = new Date().toISOString();
      session.runs = session.runs.map((run, index) =>
        index === pendingHumanGate.runIndex
          ? {
              ...run,
              humanGate: {
                ...run.humanGate,
                status: "approved",
                approvedAt
              }
            }
          : run
      );
    }
    session.messages = [...(session.messages || []), userMessage];
    session = sessionStore.save(session);

    activeRuns.add(session.id);
    const progressEvents = [];
    const emitProgress = (progress) => {
      const eventRecord = {
        sessionId: session.id,
        ...progress
      };
      progressEvents.push(eventRecord);
      event.sender.send("agent:progress", eventRecord);
    };
    if (pendingHumanGate) {
      emitProgress({
        stage: "human_gate",
        detail: "Resuming approved high-risk task",
        at: new Date().toISOString()
      });
    }

    try {
      const run = await backendService.runPipeline({
        settings,
        workspacePath: session.workspacePath,
        messages: session.messages,
        userText: engineContent,
        sessionId: session.id,
        humanGateApproval: pendingHumanGate
          ? {
              status: "approved",
              id: pendingHumanGate.id || null,
              createdAt: pendingHumanGate.createdAt || null,
              approvedAt,
              correlationId: pendingHumanGate.correlationId || null,
              executionId: pendingHumanGate.executionId || null,
              originalTask: pendingHumanGate.originalTask || pendingHumanGate.task,
              riskClass: pendingHumanGate.riskClass || "high",
              reason: pendingHumanGate.reason || "",
              kind: pendingHumanGate.kind || "risk_approval",
              retryCount: Number(pendingHumanGate.retryCount || 0),
              reworkCycle: Number(pendingHumanGate.reworkCycle || 0),
              grantAdditionalAttempts: Number(pendingHumanGate.grantAdditionalAttempts || 0)
            }
          : null,
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
      const runIdentity = run.executionId || run.id;
      const completionProgress = {
        stage: "done",
        detail: "Hoàn tất",
        at: new Date().toISOString()
      };
      const observedRun = {
        ...run,
        progressEvents: [
          ...progressEvents,
          {
            sessionId: session.id,
            ...completionProgress
          }
        ]
      };
      session.runs = [
        ...(session.runs || []).filter((item) => (item.executionId || item.id) !== runIdentity),
        observedRun
      ];
      session.title = session.title === "Phiên mới" ? userMessage.content.slice(0, 48) : session.title;
      session = sessionStore.save(session);

      event.sender.send("agent:progress", {
        sessionId: session.id,
        ...completionProgress
      });

      return {
        session,
        sessions: sessionStore.list(),
        run: observedRun
      };
    } catch (error) {
      const errorProgress = {
        stage: "error",
        detail: error.message,
        at: new Date().toISOString()
      };
      const errorRun = {
        id: crypto.randomUUID(),
        executionId: null,
        correlationId: progressEvents.find((item) => item.correlationId)?.correlationId || null,
        task: engineContent,
        assistantText: `Mình chưa chạy xong được: ${error.message}`,
        changedFiles: [],
        commandResults: [],
        review: null,
        error: error.message,
        createdAt: errorProgress.at,
        progressEvents: [
          ...progressEvents,
          {
            sessionId: session.id,
            ...errorProgress
          }
        ]
      };
      const assistantMessage = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: errorRun.assistantText,
        createdAt: new Date().toISOString(),
        error: true
      };
      session.messages = [...session.messages, assistantMessage];
      session.runs = [...(session.runs || []), errorRun];
      session = sessionStore.save(session);
      event.sender.send("agent:progress", {
        sessionId: session.id,
        ...errorProgress
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

app.whenReady().then(async () => {
  const userDataPath = app.getPath("userData");
  appDatabase = new AppDatabase(userDataPath);
  settingsStore = new SettingsStore(appDatabase, userDataPath);
  sessionStore = new SessionStore(appDatabase, userDataPath);

  // Full Power: if requireAdmin is on and we are not already elevated, relaunch
  // through UAC and bail out. Done BEFORE backend spawn so the Python process
  // inherits the Administrator token. Honored only on Windows.
  //
  // Anti-loop guard: if the last elevation attempt happened < 30s ago, skip —
  // the previous attempt is probably still loading, or it crashed and we'd
  // spin forever. Stamp the attempt time on disk so concurrent launches see it.
  try {
    const requireAdmin = !!settingsStore.get()?.fullPower?.requireAdmin;
    if (requireAdmin && process.platform === "win32" && !isElevatedWindows()) {
      const stampPath = path.join(userDataPath, "elevation-attempt.txt");
      let lastAttempt = 0;
      try { lastAttempt = parseInt(require("fs").readFileSync(stampPath, "utf8"), 10) || 0; } catch {}
      const sinceMs = Date.now() - lastAttempt;
      if (lastAttempt && sinceMs < 30000) {
        console.warn(`Elevation requested ${Math.round(sinceMs/1000)}s ago — skipping to avoid loop.`);
        // Auto-disable the flag so the user can recover via UI even if UAC
        // keeps failing. They can re-enable after fixing the root cause.
        const s = settingsStore.get();
        settingsStore.save({ ...s, fullPower: { ...s.fullPower, requireAdmin: false } });
        console.warn("Auto-disabled fullPower.requireAdmin to break the loop. Re-enable in Settings when ready.");
      } else {
        try { require("fs").writeFileSync(stampPath, String(Date.now()), "utf8"); } catch {}
        const relaunched = relaunchElevatedWindows();
        if (relaunched) {
          console.warn("Relaunching with Administrator privileges via UAC…");
          app.exit(0);
          return;
        }
        console.warn("UAC relaunch failed; continuing without elevation.");
      }
    }
  } catch (e) {
    console.warn("Elevation check failed:", e?.message || e);
  }
  const appRecovery = sessionStore.reconcileStartupState();
  if (appRecovery.recoveredRuns) {
    console.warn(`Recovered ${appRecovery.recoveredRuns} non-terminal UI run(s) in app DB.`);
  }
  backendService = new AgentBackendService(userDataPath, {
    getSettings: () => settingsStore.get()
  });
  registerIpc();
  createWindow();
  backendService.start().catch((error) => {
    console.error("Agent backend failed to start:", error);
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", () => {
  backendService?.stop();
  appDatabase?.close();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
