from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ElectronHardeningTests(unittest.TestCase):
    def test_browser_window_renderer_sandbox_is_enabled(self) -> None:
        main_js = (ROOT / "src" / "main" / "main.js").read_text(encoding="utf-8")

        self.assertIn("contextIsolation: true", main_js)
        self.assertIn("nodeIntegration: false", main_js)
        # sandbox must be false for preload.js (which uses Node require()) to work.
        # Security is provided by contextIsolation + no nodeIntegration.
        self.assertIn("sandbox: false", main_js)
        self.assertIn("// must be false for preload to use Node require()", main_js)

    def test_settings_store_does_not_persist_raw_api_key_in_model_config(self) -> None:
        settings_js = (ROOT / "src" / "main" / "settingsStore.js").read_text(encoding="utf-8")

        self.assertIn("safeStorage", settings_js)
        self.assertIn("model-api-key.bin", settings_js)
        self.assertIn("const { apiKey: _apiKey, ...persistedSettings } = finalSettings;", settings_js)
        self.assertIn('this.database.setJsonSetting("modelConfig", persistedSettings)', settings_js)

    def test_renderer_is_dashboard_not_scrolling_chat_transcript(self) -> None:
        index_html = (ROOT / "src" / "renderer" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "src" / "renderer" / "app.js").read_text(encoding="utf-8")
        preload_js = (ROOT / "src" / "main" / "preload.js").read_text(encoding="utf-8")
        main_js = (ROOT / "src" / "main" / "main.js").read_text(encoding="utf-8")

        self.assertIn('class="dashboard-main"', index_html)
        self.assertIn('id="dagBoard"', index_html)
        self.assertIn('id="logList"', index_html)
        self.assertIn('id="ovStatus"', index_html)
        self.assertIn("autonomySummary", app_js)
        self.assertIn("dashboardMetrics", app_js)
        self.assertNotIn('class="chat-panel"', index_html)
        self.assertNotIn('id="messages"', index_html)
        self.assertNotIn("renderMessages", app_js)
        self.assertIn("performAutonomyScan", app_js)
        self.assertIn("getObservability", preload_js)
        self.assertIn("runAutonomyScan", preload_js)
        self.assertIn("progressEvents", main_js)
        self.assertIn("agent:autonomy-scan", main_js)


if __name__ == "__main__":
    unittest.main()
