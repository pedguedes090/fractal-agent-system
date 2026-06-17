# Hệ Thống Agent

Ứng dụng desktop Electron chỉ là lớp điều khiển tối giản. Phần chính của dự án là **Python engine dùng LangGraph + OpenHands SDK thật**: nhận task, đọc repo, lập kế hoạch, phản biện, tạo worker task spec, cho một worker OpenHands duy nhất sửa file, review tự động, rework có giới hạn và báo cáo lại.

## Chạy ứng dụng

```powershell
npm install
npm start
```

Python engine dùng `.venv` Python 3.12 và các dependency trong `pyproject.toml`:

```powershell
python -m uv venv --python 3.12 .venv
python -m uv pip install --python .venv\Scripts\python.exe -e .
```

Mặc định app dùng OpenAI-compatible endpoint:

- Server: `http://localhost:20128/v1`
- Model: `gemini/gemini-3.1-flash-lite`

Hai giá trị này có thể đổi và lưu ngay trong giao diện.

## Pipeline Chính

```text
User Task
  -> Preflight / Repo Snapshot
  -> Read-only Intake Committee
       - User Intent
       - Ambiguity & Edge Cases
       - Trusted Repo Context
  -> Intake Synthesizer
       - Problem Statement
       - Repro / Constraints / Risk Class
  -> Read-only Planning Committee
       - Minimal Plan
       - Robust Plan
       - Test-first Plan
  -> Critique Layer
       - Risk
       - Test Coverage
       - Security / Regression
  -> Plan Arbiter
       - Final Plan
       - Acceptance Criteria
       - Worker Task Spec
  -> Human Gate for High-risk Tasks
  -> Single OpenHands SDK Coding Worker
  -> Automated Review Stack
       - Full Tests / Verification Commands
       - Diff Review
       - Security / Regression Review
  -> Bounded Rework Loop
  -> Reporter
```

## Nguyên Tắc

- Các committee trước worker là read-only.
- Chỉ `Single OpenHands SDK Coding Worker` được ghi file.
- Worker xác nhận sử dụng OpenHands SDK để thực hiện các thay đổi mã nguồn.
- Worker chỉ được ghi vào `allowedFiles` trong `workerTaskSpec`.
- Nếu task bị đánh dấu `high` risk, pipeline dừng ở human gate cho tới khi người dùng xác nhận.
- Review stack không chạy dev server như `npm start`; chỉ chạy các lệnh verification an toàn như `npm run check`, `npm test`, `npm run build`, `pytest`, `go test`.
- Các yêu cầu chỉ đọc như “đọc”, “giải thích”, “tóm tắt”, “trả lời” sẽ không ghi file.

## Streaming Và OpenHands

- Trong lúc chạy, progress của LangGraph và event của OpenHands được stream vào một message tạm trong khung chat.
- Stage `task_intent` cho biết hệ thống đã nhận task thành `read-only`, `modify`, `create_project` hay `command` trước khi để LLM committee suy luận tiếp.
- Stage `codegraph_context` cho biết pipeline có dùng được semantic code context từ CodeGraph hay không.
- Event OpenHands được rút gọn thành các dòng dễ đọc như `terminal: npm run build`, `file_editor: edit src/App.jsx`, `task_tracker: ...`.
- Khi pipeline hoàn tất, message stream tạm biến mất và được thay bằng báo cáo cuối đã lưu trong session.
- Worker bật `LLMSummarizingCondenser` để giảm rủi ro tràn context ở các task dài.
- Worker vẫn giữ `tool_concurrency_limit=1` để chỉ có một luồng ghi file.
- Bật `Auto xác nhận` trong giao diện để Human Gate tự pass các tác vụ high-risk.
- Nếu Human Gate dừng tác vụ high-risk, gửi `xác nhận` trong cùng phiên sẽ resume tác vụ gốc thay vì tạo task mới.

## CodeGraph Acceleration

Dự án cài `@colbymchenry/codegraph` như dependency local để tăng tốc pha repo context/planning. Pipeline tự tạo project index bằng `codegraph init` khi workspace chưa có `.codegraph/`, nhưng không chạy installer global và không tự sửa config Codex/Claude/Cursor trên máy.

Cách hoạt động:

- Nếu workspace chưa có `.codegraph/`, node `codegraph_context` tự chạy `codegraph init .` một lần.
- Sau đó node `codegraph_context` gọi `codegraph explore` để lấy source liên quan, relationship map và blast radius cho task hiện tại.
- CodeGraph context được đưa vào Intake Synthesizer, Planning Committee, read-only reporter và OpenHands worker như **code data**, không phải repo instruction.
- Sau khi OpenHands sửa file, review stack gọi `codegraph affected --json` để gợi ý test liên quan tới changed files.
- Nếu CodeGraph init/query lỗi, pipeline bỏ qua CodeGraph và vẫn chạy bình thường.
- Telemetry được tắt bằng `CODEGRAPH_TELEMETRY=0` khi app spawn Python engine.

Bật thủ công cho một workspace nếu muốn chuẩn bị trước:

```powershell
codegraph init
```

Xem trạng thái:

```powershell
codegraph status
```

## Cấu Hình Plugin / MCP / Skill

OpenHands SDK có thể load plugin trực tiếp từ workspace. Tạo file `.openhands/plugins.json` trong repo đang mở:

```json
{
  "plugins": [
    "github:owner/repo",
    {
      "source": "./local-openhands-plugin",
      "ref": "main",
      "repo_path": "plugins/web",
      "enabled": true
    }
  ]
}
```

Plugin có thể đóng gói skills, hooks, MCP config, agent và commands. Với MCP trực tiếp, tạo `.openhands/mcp.json` hoặc `.mcp.json` theo cấu trúc MCP config mà OpenHands SDK nhận vào `Agent(mcp_config=...)`.

Khuyến nghị thực tế:

- Đặt `AGENTS.md` ở root repo để mô tả convention, lệnh test/build, vùng cấm sửa, checklist review.
- Dùng plugin/skill cho tri thức lặp lại theo domain, ví dụ React/Vite, Python packaging, test policy, security checklist.
- Dùng MCP khi cần tool thật như docs nội bộ, issue tracker, database schema read-only, browser automation, hoặc package registry.
- Không bật MCP/plugin nặng mặc định cho mọi repo; chỉ bật theo workspace để giữ tốc độ và giảm nhiễu context.

## Cấu Trúc

- `engine/agent_engine/graph.py`: LangGraph orchestration, fan-out/fan-in reducers, human gate, OpenHands worker node, automated review stack và bounded rework loop.
- `engine/agent_engine/openhands_worker.py`: OpenHands SDK single-writer adapter dùng `LLM`, `Agent`, `Conversation`, `TerminalTool`, `FileEditorTool`, `TaskTrackerTool`.
- `engine/agent_engine/llm_client.py`: OpenAI-compatible client cho các committee read-only.
- `src/main/pythonEngine.js`: Electron main process gọi Python engine qua JSONL.
- `src/main/agentPipeline.js`: pipeline JS cũ, giữ lại để tham chiếu nhưng main app hiện dùng Python engine.
- `src/main/workspace.js`: đọc/ghi workspace và allowlist lệnh verification.
- `src/main/llmClient.js`: client `/chat/completions`, hỗ trợ JSON thường và stream `data:`.
- `src/main/main.js`: Electron main process và IPC.
- `src/main/sessionStore.js`: lưu phiên chat.
- `src/main/settingsStore.js`: lưu server/model.
- `src/renderer/`: giao diện chat tối giản.
