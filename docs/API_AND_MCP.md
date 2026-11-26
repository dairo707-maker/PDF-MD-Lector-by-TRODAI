# CLI, Local REST API, Native Messaging & MCP Reference

## 1. Command-Line Interface (`pdf-md-lector` / `pdf-lector`)

Entry points declared in `pyproject.toml`:
- `pdf-md-lector` -> `pdf_lector.app:main`
- `pdf-lector` -> `pdf_lector.app:main`
- `pdf-md-lector-mcp` -> `pdf_lector.mcp_server:main`

### Supported Flags & Arguments

| Flag / Argument | Behavior |
|---|---|
| `[path]` | Open local `.pdf`, `.md`, or `.markdown` file in `ViewerView` (validated via `path_guard`; forwarded via `SingleInstanceLock` if already running). |
| `--merge <in1> <in2> ... -o <out.pdf>` | Headless PDF merge via `ToolRegistry.get("merge_pdf")`. |
| `--compress <in.pdf> -o <out.pdf> [--quality N] [--dpi N]` | Headless PDF compression via `ToolRegistry.get("compress_pdf")`. |
| `--ocr <in.pdf> -o <out.pdf> [--dpi N] [--lang CODE]` | Headless PDF OCR via `ToolRegistry.get("pdf_ocr")`. |
| `--serve-api [--host 127.0.0.1] [--port PORT]` | Start the lazy local FastAPI/Uvicorn REST server (loopback only). |
| `--native-host` | Run the 4-byte little-endian Chrome/Edge Native Messaging stdio host. |
| `--no-splash` | Skip startup splash screen. |
| `--fast-boot` | Skip minimum splash display delay. |
| `--screenshot-dir <dir>` | Render verification screenshots and exit cleanly. |

---

## 2. Lazy Local REST & OpenAPI 3.1 Server (`src/pdf_lector/api/`)

- **Default State:** **OFF** by default. `fastapi` and `uvicorn` are never imported during Phase-1 GUI startup.
- **Bind Restriction:** Strictly loopback (`127.0.0.1` / `localhost`). Non-loopback bind addresses (e.g., `0.0.0.0`) raise `ValueError`.
- **Authentication:** Mandatory `Authorization: Bearer <token>` header validated with constant-time `hmac.compare_digest` against the DPAPI-encrypted `local_api_token` in `SecretsStore` (`%LOCALAPPDATA%\TRODAI\PDFLector\secrets.json`).

### Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | Bearer | Returns service status and version (`0.1.0`). |
| `GET` | `/openapi.json` | Public/Local | OpenAPI 3.1 JSON schema. |
| `GET` | `/tools` | Bearer | Lists registered `ToolRegistry` IDs and `ToolParamSpec` schemas. |
| `POST` | `/tools/{tool_id}` | Bearer | Executes a registered tool with JSON body `{"inputs": [...], "output": "...", "params": {...}}`. |

All `inputs` and `output` paths are enforced by `core/path_guard.py` (UNC paths, URL schemes, and `..` traversal return HTTP `400`/`403`/`422`). Unknown `tool_id` returns `404`. Invalid parameters (e.g., out-of-range `dpi`) return `400`/`422`.

---

## 3. Browser Extension (MV3) & Native Messaging (`com.trodai.pdf_md_lector`)

- **Extension Directory:** `browser-extension/` (`manifest.json` MV3 with `"nativeMessaging"` permission, `background.js`, `popup.html`, `popup.js`, polygonal icons, zero external CDN/HTTPS scripts).
- **Host Registry Keys (`HKCU`):**
  - `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.trodai.pdf_md_lector`
  - `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.trodai.pdf_md_lector`
- **Framing Protocol (`core/native_messaging.py`):**
  - Input/output frames use standard Chromium 4-byte little-endian unsigned length prefix (`<I`) followed by UTF-8 JSON payload on `stdin`/`stdout` (zero debug prints to `stdout`).
- **Supported Actions:**
  - `{"action": "ping"}`
  - `{"action": "open_pdf", "path": "..."}`
  - `{"action": "webpage_to_pdf", "html": "...", "output": "...", "dpi": 150}` (sanitized with `nh3`, rendered locally)
  - `{"action": "quick_action", "tool_id": "compress_pdf", "inputs": [...], "output": "..."}`

---

## 4. MCP Server (`pdf-md-lector-mcp`) & Client (`core/mcp_client.py`)

- **Transport:** JSON-RPC 2.0 over `stdio` (newline-delimited JSON).
- **Lifecycle Methods:**
  - `initialize` -> returns protocol version, server info, and capabilities.
  - `tools/list` -> returns available local document tools (e.g., `read_pdf_text`, `read_markdown`, `search_document`, `run_pdf_tool`).
  - `tools/call` -> executes tool after validating all paths via `path_guard`.
- **Error Codes:** Standard JSON-RPC 2.0 codes (`-32700` Parse error, `-32600` Invalid Request, `-32601` Method not found, `-32602` Invalid params / unsafe path blocked).
- **MCP Client (`McpStdioClient`):** Connects to user-configured stdio MCP servers only under explicit user consent.

---

## 5. AI "Ask Document" (`core/ai_rag.py` + `ui/ai_panel.py`)

- **Offline Default (BM25 / TF-IDF):** `DocumentRagIndex` indexes local `.pdf` pages and `.md` sections locally and returns grounded citations (`page_number` for PDFs, `section` heading for Markdown). Handles empty/scanned documents cleanly without `ZeroDivisionError`.
- **Opt-In Cloud BYOK:** Supports OpenAI-compatible, Gemini, Claude, and local Ollama endpoints with API keys stored via DPAPI (`SecretsStore`). Outbound requests are strictly blocked (`consent_blocked=True`) unless `consent_granted=True`. Error paths never echo or leak API keys.
