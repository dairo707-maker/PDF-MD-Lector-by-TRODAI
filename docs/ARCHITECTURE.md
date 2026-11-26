# Architecture — PDF/MD Lector by TRODAI (`v0.1.0`)

## 1. System Overview

**PDF/MD Lector** (`TRODAI.PDFMDLector`) is a local-first, privacy-focused Windows 11 desktop application combining:
- A dual **PDF + Markdown (`.md` / `.markdown`)** tabbed reader/editor with ISO 32000 native annotations, stylus/touch ink, and an optional closable/detachable offline SQLite dictionary.
- A modular **44-tool PDF suite** (`ToolRegistry`) across 8 categories, including WinRT/Tesseract/RapidOCR, `QScreen`/WIA/`QCamera` capture, destructive redaction, metadata sanitization, and dual-profile e-invoicing (Colombia DIAN UBL 2.1 + Factur-X EN 16931).
- Local-first automation surfaces: CLI headless mode, per-user Windows 11 `HKCU` integration, Chrome/Edge MV3 Native Messaging, lazy loopback REST/OpenAPI 3.1 server (`127.0.0.1`), BM25/BYOK "Ask Document" panel, and `pdf-md-lector-mcp` stdio server.

---

## 2. Layered Module Map

```text
+-------------------------------------------------------------------------+
| Entry & Lifecycle: src/pdf_lector/app.py                                |
|   Phase 1 (<300ms): ThemeProfile -> SplashScreen -> SingleInstanceLock  |
|                     -> CrashGuard (sys.excepthook + faulthandler)       |
|   Phase 2: register_embedded_fonts -> QSS -> MainWindow -> Lazy Engines |
+-------------------------------------------------------------------------+
        |                        |                        |
        v                        v                        v
+-------------------+  +-------------------+  +---------------------------+
| UI Layer (ui/)    |  | Core Engines      |  | Integrations & Packaging  |
| - MainWindow      |  | - pdf_engine.py   |  | - windows_integration.py  |
| - HubView         |  | - pdf_annotations |  | - native_messaging.py     |
| - ToolWorkspace   |  | - markdown_engine |  | - browser-extension/      |
| - ViewerView      |  | - md_security.py  |  | - api/server.py (lazy)    |
| - PdfCanvas       |  | - dictionary_eng  |  | - mcp_server.py (stdio)   |
| - MdTabPage       |  | - ocr_engine.py   |  | - ai_rag.py / mcp_client  |
| - DictionaryPanel |  | - capture_engine  |  | - scripts/build_exe.py    |
| - ThemeStudio     |  | - invoice_engine  |  | - scripts/sign_local.ps1  |
| - AiPanel         |  | - redaction.py    |  | - packaging/installer.iss |
+-------------------+  | - metadata_sanit. |  +---------------------------+
                       | - updater.py      |
                       +-------------------+
                                 |
                                 v
+-------------------------------------------------------------------------+
| Security & Storage Primitives (core/)                                   |
| - path_guard.py (UNC/URL/traversal/ADS/cloud-only blocking)             |
| - win32_bridge.py (DPAPI CryptProtectData, DWM, AppUserModelID)         |
| - secrets_store.py (%LOCALAPPDATA%\TRODAI\PDFLector\secrets.json)       |
| - temp_manager.py (session cleanup + secure overwrite unlink)           |
| - feedback_reporter.py (PII scrubber + deploy/feedback-worker/)         |
+-------------------------------------------------------------------------+
```

---

## 3. Architecture Decision Records (ADRs)

### ADR-001: Two-Phase Boot & Lazy Engine Loading (`< 300 ms` Target)
- **Context:** Importing `pikepdf`, `pyHanko`, `fastapi`, `uvicorn`, `openpyxl`, `python-docx`, `markdown-it-py`, `nh3`, and registering embedded TTF fonts upfront causes slow startup (> 600 ms).
- **Decision:** `app.py` executes Phase 1 immediately (load `ThemeProfile`, show `SplashScreen`, attach `SingleInstanceLock` + `CrashGuard`) before importing heavy backends or calling `QFontDatabase.addApplicationFont`. All tool modules, OCR, invoice, API, and AI/MCP engines are imported on demand.
- **Consequences:** Rapid perceived launch, clean headless CLI startup without loading GUI/API stacks, and strict isolation verified by `test_fa01_t2_04_phase1_boot_excludes_heavy_engines`.

### ADR-002: Safe Markdown via `markdown-it-py` + `nh3` + `QTextBrowser` (Zero `QtWebEngine`)
- **Context:** Embedding `QtWebEngine*` inflates PyInstaller executables by > 120 MB and introduces Chromium attack surface and subprocess sandboxing complexity.
- **Decision:** Render GFM Markdown with `markdown-it-py`, sanitize HTML via `nh3` with a strict tag/attribute allowlist (`core/md_security.py`), and display inside `QTextBrowser` (`setOpenExternalLinks(False)` + overridden `loadResource()` gated by `path_guard.resolve_safe_doc_resource`). Exclude `PySide6.QtWebEngine*` in `scripts/build_exe.py`.
- **Consequences:** Compact single-file portable binary, deterministic offline XSS/tracker defense, and native `MD <-> PDF` round-trip via `reportlab`/`pikepdf`/`pdfplumber`.

### ADR-003: Headless `ToolRegistry` with Unified DPI / Quality / Language Specs
- **Context:** The 44 tools across 8 categories must be callable identically from the GUI (`ToolWorkspace`), CLI, local REST API, and Browser Native Messaging.
- **Decision:** Centralize all tools in `ToolRegistry` (`src/pdf_lector/core/tools/__init__.py`) returning structured `ToolResult(success, output_paths, summary, details)` and declaring typed `ToolParamSpec` entries (`dpi` presets `72..600` + custom `36..1200`, `quality` `10..100`, `source_lang` `auto|es|en|pt|fr|de|it|multi`).
- **Consequences:** Zero business logic duplication in UI layers and uniform input/output `path_guard` validation across every execution surface.

### ADR-004: Local-First Zero Telemetry, DPAPI Secrets & Consent Gates
- **Context:** Users handle confidential contracts, tax invoices, and personal documents.
- **Decision:** All network-capable features (update checks, dictionary language packs, BYOK cloud LLMs, external MCP servers, online invoice extraction, anonymous support submission) are either OFF by default or gated by explicit user consent / `Solo offline` toggle. Local credentials are protected with Windows DPAPI (`CryptProtectData`).
- **Consequences:** Safe offline default with auditable, opt-in external boundaries.

### ADR-005: `$0` Free Code Signing & Per-User Installation
- **Context:** Commercial EV code signing certificates cost hundreds of USD/year, creating a barrier for independent MIT OSS software.
- **Decision:** Provide a dual `$0` strategy: (1) per-user non-admin Inno Setup 6 installer (`PrivilegesRequired=lowest`, `PrivilegesRequiredOverridesAllowed=dialog`), (2) native PowerShell Authenticode signing (`scripts/sign_local.ps1` using `New-SelfSignedCertificate` + `Set-AuthenticodeSignature`, with explicit `-InstallTrust`/`-UninstallTrust`), and (3) GitHub Actions SLSA Build Provenance (`actions/attest-build-provenance@v2`) + `SHA256SUMS.txt` + SignPath.io Free OSS CI template.
- **Consequences:** Zero licensing cost, zero admin elevation requirement, and cryptographic verifiability.

### ADR-006: Decoupled Multi-Surface Automation (REST / Native Messaging / MCP)
- **Context:** External editors, AI assistants, and browsers benefit from interacting with local PDF/Markdown workflows without compromising desktop security.
- **Decision:** Keep the REST API (`127.0.0.1` + DPAPI Bearer) OFF by default, use 4-byte LE `stdio` framing for Chromium/Edge Native Messaging (`com.trodai.pdf_md_lector`), and expose `pdf-md-lector-mcp` over `stdio` JSON-RPC 2.0 gated by `path_guard`.
- **Consequences:** Local interoperability with strict OS-user boundary enforcement.
