# Changelog

All notable changes to **PDF/MD Lector by TRODAI** are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-26

### Fixed
- Fixed native access violation / `RuntimeError` on `ToolCard` click (`widgets.py` `shiboken6.isValid(self)` guard after synchronous view switch).
- Fixed thread-safety crash in `DocumentRagIndex.index_pdf` by replacing `pypdfium2` inside `QThreadPool` workers with pure-Python `pypdf.PdfReader`.

### Added
- **Local AI Hardware Gate & Legal API / Official MCP Connectors (`core/ai_hardware.py`, `ui/ai_panel.py`):** Probes PC RAM (>=16 GB) and GPU VRAM (>=6 GB) before enabling local `ollama` LLMs; when hardware is insufficient, blocks heavy local LLM startup and guides 1-click connection to official BYOK APIs (OpenAI, Gemini, Anthropic) or an Official MCP Server (`mcp_official`) under explicit user consent, DPAPI encryption, and provider terms. `Local BM25` remains 100% offline with 0 GPU requirement.
- **Full Morphological Dictionary, Fuzzy Suggestions & Opt-in Wiktionary (`core/dictionary_engine.py`, `ui/dictionary_panel.py`):** Added ES/EN lemmatization (`fondos -> fondo`, `personalizables -> personalizable`, verb/adjective/plural stems), expanded offline lexicon, `suggest_fuzzy` clickable suggestion chips, compressed `.json.gz` language packs (`pt`, `fr`, `de`, `it`), and opt-in Wikimedia Wiktionary REST API lookup with automatic SQLite caching.
- **Draggable Sticky Notes, Floating Note Cards & Notes Panel (`ui/pdf_canvas.py`, `ui/notes_panel.py`, `core/pdf_annotations.py`):** Sticky notes (`/Text`) can be dragged across the canvas, clicked to open an interactive floating card near their position (6 colors, categories persisted via PDF `/Subj`, Close, Delete with confirmation), and organized/filtered in a dedicated collapsible `NotesPanel`.
- **Autosave, Manual Save & Crash Recovery (`core/autosave.py`, `ui/main_window.py`):** Atomic 30s periodic + 3s debounced snapshots saving open tabs, zoom, rotation, scroll position, unsaved annotations, and Markdown buffers, plus automatic recovery banner after unexpected termination.
- **In-App Direct Updates (`ui/about_updater_dialog.py`, `ui/main_window.py`):** Background GitHub Releases update check (respecting `Solo offline` and 24h cadence), SHA-256 verification, and 1-click *"Instalar y reiniciar ahora"* launcher, alongside `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`.

## [0.1.0] - 2026-11-26

### Added
- **Dual PDF & Markdown Core (M1, M3):**
  - Mixed `.pdf` and `.md`/`.markdown` tabbed viewer (`ViewerView`) with 3-mode rail (`Thumbnails | TOC & Bookmarks | Both`) and 3-mode Markdown tab (`Reading | Split | Editor`).
  - Safe Markdown rendering via `markdown-it-py` + `nh3` strict sanitizer + `QTextBrowser` (zero `QtWebEngine*`), native `MD <-> PDF` conversion, and `#/##/###` TOC extraction.
  - Native ISO 32000 PDF annotations (`/Highlight` 6 colors, `/Text` sticky notes, `/Ink` stylus/touch strokes with `QTabletEvent` palm rejection and 1-click Touch Ink mode, `/FreeText`/`/Popup` threaded replies with default author `Anonimo`).
  - Optional, closable, detachable Kindle-style offline SQLite dictionary (`DictionaryPanel` + `DictionaryEngine`) with ES/EN base packs and `.json` glossary import.
- **Modular 44-Tool PDF Suite & Dual Invoices (M2):**
  - Unified `ToolRegistry` across 8 categories (44 unique `tool_id`s) with configurable `dpi` (`72/150/200/300/600`/custom `36..1200`), `quality` (`10..100`), and `source_lang` (`auto`, `es`, `en`, `pt`, `fr`, `de`, `it`, `multi`).
  - Destructive PDF redaction (`core/redaction.py`) and metadata/XMP sanitization (`core/metadata_sanitizer.py`).
  - Local OCR (`WinRT Windows.Media.Ocr` + Tesseract probe + optional RapidOCR), `QScreen` region capture, and WIA COM / `QCamera` scanner import.
  - Dual-profile e-invoicing (`create_visual_invoice`, `create_einvoice`, `validate_einvoice`, `pdf_to_einvoice`) supporting Colombia DIAN UBL 2.1 (XML + NIT + IVA/INC + CUFE + QR) and International Factur-X / ZUGFeRD / XRechnung EN 16931 PDF/A-3.
- **Foundation, Privacy & UX (M1, M4):**
  - `path_guard.py`, Windows DPAPI `secrets_store.py`, `TempManager`, `CrashGuard`, `SingleInstanceLock`, `NotificationCenter`, `TaskController`, `LocalUsageInsights` dynamic Quick Actions, and PII-anonymized `feedback_reporter.py` + Cloudflare Worker (`deploy/feedback-worker/`).
  - Two-phase `<300 ms` startup, Theme Studio (Hub + outer canvas, 3 motion levels, BT.709 Auto-Scrim AA), SHA-256 `updater.py` with `Solo offline` toggle.
- **Integrations, Packaging, $0 Signing & OSS Docs (M5):**
  - Windows 11 `HKCU` integration + CLI flags (`--merge`, `--compress`, `--ocr`, `--serve-api`, `--native-host`), Chrome/Edge MV3 browser extension (`browser-extension/`) + 4-byte LE Native Messaging host, lazy loopback REST/OpenAPI server (`127.0.0.1` + DPAPI Bearer), AI "Ask Document" (BM25 + BYOK + MCP client) + `pdf-md-lector-mcp` stdio server.
  - Windows packaging (`scripts/build_exe.py`), `$0` native PowerShell Authenticode signing (`scripts/sign_local.ps1`), non-admin Inno Setup 6 wizard (`packaging/installer.iss`), GitHub Actions CI with SLSA provenance + SignPath.io Free OSS template (`.github/workflows/build-release.yml`), and full multi-AI audit documentation (`docs/`).

## [0.1.0-alpha.2] - 2026-11-24

### Added
- Validated interactive prototype baseline (`14 passed` in `tests/test_prototype_theme_motion_splash.py`): TRODAI Obsidian Navy / Crisp White palette, `Space Grotesk` + `JetBrains Mono` embedded fonts, `0px` architectural geometry, polygonal SVGs, Theme Studio, and `<300 ms` SplashScreen.
