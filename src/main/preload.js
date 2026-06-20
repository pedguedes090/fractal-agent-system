const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("agentApp", {
  getInitialState: () => ipcRenderer.invoke("app:getInitialState"),
  saveSettings: (settings) => ipcRenderer.invoke("settings:save", settings),
  chooseWorkspace: () => ipcRenderer.invoke("workspace:choose"),
  listSessions: () => ipcRenderer.invoke("sessions:list"),
  createSession: (initial) => ipcRenderer.invoke("sessions:create", initial),
  loadSession: (sessionId) => ipcRenderer.invoke("sessions:load", sessionId),
  updateSessionWorkspace: (sessionId, workspacePath) =>
    ipcRenderer.invoke("sessions:updateWorkspace", sessionId, workspacePath),
  getWorkspaceDiff: (sessionId, relativePath, status) =>
    ipcRenderer.invoke("workspace:diff", sessionId, relativePath, status),
  deleteSession: (sessionId) => ipcRenderer.invoke("sessions:delete", sessionId),
  sendMessage: (payload) => ipcRenderer.invoke("agent:send", payload),
  getObservability: () => ipcRenderer.invoke("agent:observability"),
  getAutonomyStatus: () => ipcRenderer.invoke("agent:autonomy-status"),
  runAutonomyScan: (payload) => ipcRenderer.invoke("agent:autonomy-scan", payload),
  requestAutonomyNextTask: (payload) => ipcRenderer.invoke("agent:autonomy-next-task", payload),
  getFullPowerStatus: () => ipcRenderer.invoke("agent:full-power-status"),
  getTopology: () => ipcRenderer.invoke("agent:topology"),
  cancelRun: (executionId) => ipcRenderer.invoke("agent:cancel", executionId),
  runDoctor: (payload) => ipcRenderer.invoke("doctor:run", payload),
  onDoctorEvent: (callback) => {
    const listener = (_event, event) => callback(event);
    ipcRenderer.on("doctor:event", listener);
    return () => ipcRenderer.removeListener("doctor:event", listener);
  },
  onProgress: (callback) => {
    const listener = (_event, progress) => callback(progress);
    ipcRenderer.on("agent:progress", listener);
    return () => ipcRenderer.removeListener("agent:progress", listener);
  }
});
