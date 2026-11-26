# Contributing to PDF/MD Lector by TRODAI

Thank you for contributing to **PDF/MD Lector by TRODAI** (MIT Open Source).

## 1. Environment Setup (Windows 11 / OneDrive Safe)

Always use `uv` with copy link mode inside OneDrive directories (never run global `pip install`):

```powershell
uv sync --extra dev --link-mode=copy
$env:QT_QPA_PLATFORM = "offscreen"
uv run --no-sync pytest -v
```

## 2. Architecture, Security & UI Invariants

All pull requests must preserve these non-negotiable guarantees:

- **Security & Path Guard:** Every local document, glossary, font, or media path must pass validation in `src/pdf_lector/core/path_guard.py`.
- **HTML & Markdown Safety:** All Markdown previews must pass `src/pdf_lector/core/md_security.py` (`nh3` strict allowlist inside `QTextBrowser`). Never introduce `QtWebEngine*`.
- **Shiboken6 / PySide6 Memory Safety:**
  - Never call `super().__lt__()` in Qt widget or item subclasses.
  - Guard delayed/async UI callbacks and `mousePressEvent` post-emit paths with `shiboken6.isValid(...)`, and defer dynamic widget deletion via `QTimer.singleShot(0, ...)`.
  - Use `Qt.QueuedConnection` for worker signals and pure-Python `pypdf.PdfReader` inside background threads.
- **Visual System (TRODAI):**
  - 100% polygonal SVGs (`M/L/Z`, `<polygon>`, `<polyline>`, `<rect>`) with zero `<circle>`, `<ellipse>`, or curve commands.
  - Zero Unicode emojis in source, scripts, templates, or HTML.
  - Global `0px` architectural border-radius (`6px` top bar exception only), `Space Grotesk` (`300/400/500/700`, never `600`), and `100%` opaque `StatusTag` badges.
- **Test Isolation:** Tests must never touch real `HKCU`, `Cert:\CurrentUser\Root`, or real external network hosts (`fake_winreg`, `isolated_appdata`, and `127.0.0.1:0` only).

## 3. Submitting Changes

1. Create a focused feature/fix branch from `main`.
2. Add or update unit/E2E tests in `tests/` covering your changes.
3. Run the full suite (`uv run --no-sync pytest`) and verify `0` failures.
4. Update `CHANGELOG.md` and `docs/HANDOFF_STATUS.md` when adding user-facing changes.
