# AGENTS.md — Multi-AI Engineering Rules & Onboarding

This repository (`PDF/MD Lector by TRODAI`, `v0.1.0`) is designed for seamless collaboration across AI coding agents (Claude Code, Codex, Gemini, Cursor) and human maintainers.

Before modifying any file, read:
1. [`CLAUDE.md`](CLAUDE.md) — Core identity, OneDrive commands, and non-negotiable rules.
2. [`docs/HANDOFF_STATUS.md`](docs/HANDOFF_STATUS.md) — Live milestone checkpoint (`M1`–`M6` and `E2E`).
3. [`docs/AUDIT_HANDOFF.md`](docs/AUDIT_HANDOFF.md) — Contract matrix and verification commands.

---

## 1. Environment & Test Execution (Windows 11 / OneDrive)

This workspace lives inside **OneDrive**, where NTFS hardlinks fail. Always run:

```powershell
uv sync --extra dev --link-mode=copy
$env:QT_QPA_PLATFORM = "offscreen"
uv run --no-sync pytest -v
```

Never run global `pip install`. All runtime and dev dependencies are pinned in `pyproject.toml` and `uv.lock`.

---

## 2. Visual, Brand & SVG Rules (TRODAI System)

- **Product / Company separation:**
  - App name: `PDF/MD Lector` (`PDF/MD Lector · by TRODAI`).
  - App icon (`assets/icons/pdf_lector_logo.svg`, `assets/icons/pdf_lector.ico`): 100% opaque solid colors, codex-shield emblem.
  - Company seal (`assets/icons/trodai_sentinel.svg`): strictly `by TRODAI` wordmark/seal; never highlight `"AI"` separately.
- **Zero Unicode emojis:** Never insert Unicode emoji characters in `.py`, `.ps1`, `.iss`, `.yml`, `.js`, `.html`, or test files.
- **100% polygonal SVGs:** Only `M/L/Z` path commands, `<polygon>`, `<polyline>`, `<rect>`, with `stroke-linecap="square"`, `stroke-linejoin="miter"`, `stroke-miterlimit="4"`. Never use `<circle>`, `<ellipse>`, or `C/S/Q/T/A` curves.
- **Typography & Geometry:**
  - `Space Grotesk` (`300`, `400`, `500`, `700` only; **never `600`**) + `JetBrains Mono`.
  - Global `border-radius: 0px` (`6px` sole exception on `QWidget#TopBar`); DWM `DWMWCP_DONOTROUND`.
  - Zero selection color accents in navigation or Theme Studio; 100% opaque `StatusTag` badges (no `rgba(`); uniform `232x104` `ToolCard` sizing.

---

## 3. PySide6 / Shiboken6 Safety Invariants

1. **Never** call `super().__lt__()` in Qt widget/item overrides.
2. **Never** destroy child widgets synchronously inside `clicked` slots — defer via `QTimer.singleShot(0, ...)` + `w.hide(); w.deleteLater()`, and guard async callbacks with `shiboken6.isValid(...)`.
3. Use `Qt.QueuedConnection` for cross-thread signals from workers to UI.
4. Call `CoInitializeEx` / `CoUninitialize` on COM worker threads (WIA / Office probes).
5. Strip `_MEIPASS*` and `PYTHONHOME` environment variables before spawning child processes.

---

## 4. Security, Privacy & Isolation Boundaries

- **Never** touch the real Windows `HKCU` registry or `Cert:\CurrentUser\Root` store in automated tests; use `fake_winreg` (`InMemoryWinReg`), `isolated_appdata`, `local_mock_server` (`127.0.0.1:0`), and `-DryRun`.
- **Never** make real outbound network calls in tests.
- **Never** create a remote GitHub repository or execute `git push` without explicit user approval after final verification.
- All local document/resource/glossary/banner inputs must pass `src/pdf_lector/core/path_guard.py`.
- All Markdown HTML previews must pass `src/pdf_lector/core/md_security.py` (`nh3` strict allowlist) and `QTextBrowser` (`setOpenExternalLinks(False)`).
- Never embed `QtWebEngine*` in source or PyInstaller bundles.

---

## 5. Handoff Protocol

When closing a milestone or finishing a session, update [`docs/HANDOFF_STATUS.md`](docs/HANDOFF_STATUS.md) with:
- Exact milestone status (`COMPLETED` / `IN_PROGRESS` / `BLOCKED`)
- Exact files created or modified
- Exact commands and observed test counts/timings (never extrapolate or round).
