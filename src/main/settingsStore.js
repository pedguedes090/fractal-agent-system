const fs = require("fs");
const path = require("path");

const DEFAULT_SETTINGS = {
  serverUrl: "http://localhost:20128/v1",
  model: "gemini/gemini-3.1-flash-lite",
  autoConfirmHumanGate: false
};

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

class SettingsStore {
  constructor(userDataPath) {
    this.filePath = path.join(userDataPath, "settings.json");
  }

  get() {
    const stored = readJson(this.filePath, {});
    return {
      ...DEFAULT_SETTINGS,
      ...stored
    };
  }

  save(nextSettings) {
    const current = this.get();
    const cleaned = {
      serverUrl: String(nextSettings.serverUrl || current.serverUrl).trim().replace(/\/+$/, ""),
      model: String(nextSettings.model || current.model).trim(),
      autoConfirmHumanGate: Boolean(nextSettings.autoConfirmHumanGate)
    };
    const finalSettings = {
      ...DEFAULT_SETTINGS,
      ...cleaned
    };
    writeJson(this.filePath, finalSettings);
    return finalSettings;
  }
}

module.exports = {
  DEFAULT_SETTINGS,
  SettingsStore
};
