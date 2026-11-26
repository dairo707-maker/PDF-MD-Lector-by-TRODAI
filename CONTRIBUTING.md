# Contributing to PDF/MD Lector by TRODAI

Thank you for helping improve **PDF/MD Lector** (`PDF/MD Lector · by TRODAI`, MIT License).

## Development Environment (Windows 11 / OneDrive Safe)

Because many Windows 11 workspaces reside inside OneDrive where NTFS hardlinks fail, always use `--link-mode=copy` and `--no-sync`:

```powershell
uv sync --extra dev --link-mode=copy
$env:QT_QPA_PLATFORM = "offscreen"
uv run --no-sync pytest -v
```

To launch the GUI locally:

```powershell
uv run --no-sync python -m pdf_lector.app
```

## Non-Negotiable Engineering Standards

1. **Local-First Privacy & Zero Hidden Telemetry:**
   - Never add automatic outbound network requests on startup or document open.
   - Any feature that talks to an external endpoint (updates, language pack downloads, BYOK cloud LLMs, external MCP servers, support report submission, online invoice connectors) must require explicit user action or a dedicated consent toggle.
2. **Path Safety (`src/pdf_lector/core/path_guard.py`):**
   - All local PDF, Markdown (`.md`/`.markdown`), glossary (`.json`), and banner media paths must pass `path_guard` validation (blocking UNC paths, mapped remote drives, NTFS ADS, `..` traversal, and URL schemes).
3. **Markdown Sanitization (`src/pdf_lector/core/md_security.py`):**
   - Render Markdown via `markdown-it-py` + `nh3` strict allowlist + `QTextBrowser` (`setOpenExternalLinks(False)`). Never re-introduce `QtWebEngine*`.
4. **TRODAI Visual & SVG Discipline:**
   - **Zero Unicode emojis** in source code, UI strings, scripts, workflows, or tests.
   - **100% polygonal SVGs** (`M/L/Z`, `<polygon>`, `<polyline>`, `<rect>`, `stroke-linecap="square"`, `stroke-linejoin="miter"`, `stroke-miterlimit="4"`). Never use `<circle>`, `<ellipse>`, or `C/S/Q/T/A` path commands.
   - `Space Grotesk` weights `300/400/500/700` only (never `600`).
   - Global `border-radius: 0px` (`6px` sole exception on `QWidget#TopBar`); 100% opaque `StatusTag` (no `rgba(`); uniform `232x104` `ToolCard`.
5. **PySide6 / Shiboken6 Safety:**
   - Never call `super().__lt__()` in Qt overrides.
   - Never destroy child widgets synchronously inside `clicked` slots (defer with `QTimer.singleShot(0, ...)` + `w.hide(); w.deleteLater()`, and verify `shiboken6.isValid(...)`).
   - Use `Qt.QueuedConnection` across worker/UI signals, `CoInitializeEx` on COM threads, and strip `_MEIPASS*` before spawning child processes.
6. **Test Isolation:**
   - Never mutate real `HKCU` registry or `Cert:\CurrentUser\Root` in automated tests. Use `fake_winreg` (`InMemoryWinReg`), `isolated_appdata`, `local_mock_server`, or `-DryRun`.
