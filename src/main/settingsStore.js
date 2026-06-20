const fs = require("fs");
const path = require("path");
const { safeStorage } = require("electron");
const { parseJson } = require("./appDatabase");

const DEFAULT_SETTINGS = {
  serverUrl: "http://localhost:20128/v1",
  model: "gemini/gemini-3.1-flash-lite",
  apiKey: "",
  autoConfirmHumanGate: false,
  directWorkspaceMode: true,
  modelOverrides: {},
  bypassPolicy: false
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

class SecretStore {
  constructor(userDataPath) {
    this.filePath = path.join(userDataPath, "secrets", "model-api-key.bin");
    this.memoryApiKey = "";
  }

  canPersist() {
    return Boolean(safeStorage?.isEncryptionAvailable?.());
  }

  get() {
    if (this.memoryApiKey) return this.memoryApiKey;
    if (!this.canPersist() || !fs.existsSync(this.filePath)) return "";
    try {
      this.memoryApiKey = safeStorage.decryptString(fs.readFileSync(this.filePath)).trim();
      return this.memoryApiKey;
    } catch {
      return "";
    }
  }

  set(value) {
    this.memoryApiKey = String(value || "").trim();
    if (!this.memoryApiKey) {
      try {
        fs.rmSync(this.filePath, { force: true });
      } catch {
        // best effort cleanup
      }
      return;
    }
    if (!this.canPersist()) return;
    ensureDir(path.dirname(this.filePath));
    fs.writeFileSync(this.filePath, safeStorage.encryptString(this.memoryApiKey));
  }
}

class SettingsStore {
  constructor(database, userDataPath) {
    this.database = database;
    this.legacyFilePath = path.join(userDataPath, "settings.json");
    this.secretStore = new SecretStore(userDataPath);
    this.migrateLegacyJson();
    this.migratePersistedApiKey();
  }

  migrateLegacyJson() {
    if (this.database.getJsonSetting("modelConfig")) return;
    const legacy = readJson(this.legacyFilePath, null);
    if (legacy && typeof legacy === "object") {
      const { apiKey, ...safeConfig } = legacy;
      if (apiKey) this.secretStore.set(apiKey);
      this.database.setJsonSetting("modelConfig", safeConfig);
    }
  }

  migratePersistedApiKey() {
    const stored = this.database.getJsonSetting("modelConfig", {});
    if (!stored || typeof stored !== "object" || !Object.prototype.hasOwnProperty.call(stored, "apiKey")) return;
    const { apiKey, ...safeConfig } = stored;
    if (apiKey) {
      this.secretStore.set(apiKey);
    }
    this.database.setJsonSetting("modelConfig", safeConfig);
  }

  get() {
    const stored = this.database.getJsonSetting("modelConfig", {});
    return {
      ...DEFAULT_SETTINGS,
      ...stored,
      apiKey: this.secretStore.get()
    };
  }

  save(nextSettings) {
    const current = this.get();
    if (Object.prototype.hasOwnProperty.call(nextSettings, "apiKey")) {
      this.secretStore.set(nextSettings.apiKey);
    }
    const cleaned = {
      serverUrl: String(nextSettings.serverUrl || current.serverUrl).trim().replace(/\/+$/, ""),
      model: String(nextSettings.model || current.model).trim(),
      autoConfirmHumanGate: Boolean(nextSettings.autoConfirmHumanGate),
      directWorkspaceMode: Object.prototype.hasOwnProperty.call(nextSettings, "directWorkspaceMode")
        ? Boolean(nextSettings.directWorkspaceMode)
        : Boolean(current.directWorkspaceMode),
      modelOverrides: nextSettings.modelOverrides || current.modelOverrides || {},
      bypassPolicy: Boolean(nextSettings.bypassPolicy)
    };
    const finalSettings = {
      ...DEFAULT_SETTINGS,
      ...cleaned,
      apiKey: this.secretStore.get()
    };
    const { apiKey: _apiKey, ...persistedSettings } = finalSettings;
    this.database.setJsonSetting("modelConfig", persistedSettings);
    return finalSettings;
  }
}

module.exports = {
  DEFAULT_SETTINGS,
  SettingsStore
};
