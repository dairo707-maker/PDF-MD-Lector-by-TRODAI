# Multi-AI & Security Auditor Handoff Guide

This document gives external security auditors and successor AI agents (Claude Code, Codex, Gemini) a concise map of contracts, verification commands, and invariant checks across **PDF/MD Lector by TRODAI (`v0.1.0`)**.

> For live milestone completion status and exact historical pytest runs, always read [`docs/HANDOFF_STATUS.md`](HANDOFF_STATUS.md) first.

---

## 1. Repository Ownership & Scope Map

| Area | Primary Files | Key Invariants to Audit |
|---|---|---|
| Entry & Lifecycle | `src/pdf_lector/__init__.py`, `app.py`, `core/crash_guard.py`, `core/single_instance.py`, `core/temp_manager.py` | Phase-1 `<300ms`, zero heavy imports in Phase 1, clean temp wipe, `_MEIPASS*` env cleanup. |
| Security & Privacy | `core/path_guard.py`, `core/win32_bridge.py`, `core/secrets_store.py`, `core/redaction.py`, `core/metadata_sanitizer.py`, `core/feedback_reporter.py`, `deploy/feedback-worker/worker.js` | UNC/URL/traversal/cloud-only blocking, DPAPI at-rest encryption, destructive stream/XObject redaction, full XMP/`/Info` purge, strict PII scrubber, HTTPS-only remote feedback. |
| PDF Tools, OCR, Capture, Invoices | `core/tools/*.py`, `core/ocr_engine.py`, `core/capture_engine.py`, `core/invoice_engine.py` | All 44 unique `tool_id`s registered, `dpi`/`quality`/`source_lang` BVA limits, COM `CoInitializeEx`, XXE-safe XML validation, online extraction consent gate. |
| Markdown & Annotations & Dict | `core/markdown_engine.py`, `core/md_security.py`, `core/pdf_annotations.py`, `core/dictionary_engine.py`, `ui/{viewer_view,pdf_canvas,md_tab_page,dictionary_panel}.py` | `nh3` XSS allowlist, default remote image blocking, `QTextBrowser` confinement, ISO 32000 default author `Anonimo`, closable/detachable Kindle dictionary. |
| UI / Theme / Updater | `ui/{main_window,splash_screen,widgets,svg_icons,hub_view,tool_workspace,theme_studio,*_dialog}.py`, `core/updater.py` | Zero Unicode emojis, 100% polygonal SVGs, `Space Grotesk` `300/400/500/700` (never `600`), `0px` radius (`6px` TopBar only), opaque `StatusTag`, `232x104` `ToolCard`, Shiboken6 safety, SHA-256 updater + `Solo offline`. |
| Windows, Browser, API, AI, MCP | `core/windows_integration.py`, `core/native_messaging.py`, `browser-extension/`, `api/`, `core/ai_rag.py`, `core/mcp_client.py`, `ui/ai_panel.py`, `mcp_server.py` | Injectable `winreg` backend (never mutates real `HKCU` in tests), 4-byte LE stdio framing, `127.0.0.1` + Bearer `hmac.compare_digest`, BYOK consent gate, `path_guard` in MCP tools. |
| Packaging, $0 Signing, CI, Docs | `scripts/{build_exe.py,sign_local.ps1}`, `packaging/installer.iss`, `.github/workflows/build-release.yml`, `README.md`, `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CLAUDE.md`, `AGENTS.md`, `docs/*.md` | `QtWebEngine*` excluded, `-DryRun` never mutates `Cert:\CurrentUser\Root`, `PrivilegesRequired=lowest`, no hardcoded PATs, deterministic `SHA256SUMS.txt`, SLSA provenance + SignPath OSS template. |

---

## 2. Verification Commands (OneDrive / Windows 11)

```powershell
# 1. Sync environment cleanly
uv sync --extra dev --link-mode=copy

# 2. Run unit & integration tests (offscreen Qt)
$env:QT_QPA_PLATFORM = "offscreen"
uv run --no-sync pytest tests/test_prototype_theme_motion_splash.py tests/test_m1_foundation_security.py tests/test_m2_tools_ocr_invoices.py tests/test_m2b_ocr_capture_invoices.py tests/test_m3_markdown_annotations_dict.py tests/test_m5_packaging_docs.py -v

# 3. Run full E2E opaque-box suite (Tiers 1-4)
$env:QT_QPA_PLATFORM = "offscreen"
uv run --no-sync pytest tests/e2e -v --strict-markers

# 4. Validate PowerShell local signing script (AST + DryRun)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sign_local.ps1 -DryRun

# 5. Dry-run packaging build & full build
uv run --no-sync python scripts/build_exe.py --dry-run
uv run --no-sync python scripts/build_exe.py
```

---

## 3. Critical Auditor Checks

1. **Zero Secret / PII Leakage:** Inspect `deploy/feedback-worker/`, `src/`, and `tests/` for hardcoded GitHub PATs (`ghp_...`, `github_pat_...`), real user paths (`C:\Users\...`), or unredacted PII in `feedback_reporter.py`.
2. **Zero Silent Network Calls:** Verify startup (`app.py`), document open, Markdown preview (`allow_remote_images=False`), AI RAG (`consent_granted=False`), and online e-invoice extraction (`consent_granted=False`) make zero outbound requests without explicit opt-in.
3. **Packaging & Signing Safety:** Confirm `scripts/sign_local.ps1 -InstallTrust -DryRun` produces zero mutations in `Cert:\CurrentUser\Root` and `packaging/installer.iss` contains `PrivilegesRequired=lowest` (never `PrivilegesRequired=admin`).
4. **Git Policy:** Keep repository local on `main` until the project owner explicitly authorizes creating a remote or running `git push`.
