const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function readJson(filePath, fallback) {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJson(filePath, value) {
  ensureDir(path.dirname(filePath));
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2), "utf8");
}

function nowIso() {
  return new Date().toISOString();
}

function makeTitle(text) {
  const compact = String(text || "Phiên mới").replace(/\s+/g, " ").trim();
  return compact.length > 48 ? `${compact.slice(0, 45)}...` : compact || "Phiên mới";
}

class SessionStore {
  constructor(userDataPath) {
    this.filePath = path.join(userDataPath, "sessions.json");
  }

  readState() {
    const state = readJson(this.filePath, { sessions: [], activeSessionId: null });
    return {
      sessions: Array.isArray(state.sessions) ? state.sessions : [],
      activeSessionId: state.activeSessionId || null
    };
  }

  writeState(state) {
    writeJson(this.filePath, state);
  }

  list() {
    const state = this.readState();
    return {
      activeSessionId: state.activeSessionId,
      sessions: state.sessions
        .map((session) => ({
          id: session.id,
          title: session.title,
          workspacePath: session.workspacePath || "",
          createdAt: session.createdAt,
          updatedAt: session.updatedAt
        }))
        .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
    };
  }

  create(initial = {}) {
    const state = this.readState();
    const timestamp = nowIso();
    const session = {
      id: crypto.randomUUID(),
      title: makeTitle(initial.title),
      workspacePath: initial.workspacePath || "",
      messages: [],
      runs: [],
      createdAt: timestamp,
      updatedAt: timestamp
    };
    state.sessions.unshift(session);
    state.activeSessionId = session.id;
    this.writeState(state);
    return session;
  }

  get(sessionId) {
    const state = this.readState();
    return state.sessions.find((session) => session.id === sessionId) || null;
  }

  save(session) {
    const state = this.readState();
    const index = state.sessions.findIndex((item) => item.id === session.id);
    const nextSession = {
      ...session,
      title: session.title || makeTitle(session.messages?.[0]?.content),
      messages: Array.isArray(session.messages) ? session.messages : [],
      runs: Array.isArray(session.runs) ? session.runs : [],
      updatedAt: nowIso()
    };

    if (index >= 0) {
      state.sessions[index] = nextSession;
    } else {
      state.sessions.unshift({
        ...nextSession,
        createdAt: nextSession.createdAt || nowIso()
      });
    }

    state.activeSessionId = nextSession.id;
    this.writeState(state);
    return nextSession;
  }

  delete(sessionId) {
    const state = this.readState();
    state.sessions = state.sessions.filter((session) => session.id !== sessionId);
    if (state.activeSessionId === sessionId) {
      state.activeSessionId = state.sessions[0]?.id || null;
    }
    this.writeState(state);
    return this.list();
  }
}

module.exports = {
  SessionStore
};
