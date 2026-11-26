# Changelog

All notable changes to **PDF/MD Lector by TRODAI** are documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-11-26

### Fixed
- **Dark-Mode Sticky Note Contrast (`theme/palette.py`, `ui/pdf_canvas.py`):** Added an explicit `QTextEdit` palette rule and dark/light card + editor styling (`#F8FAFC` text on `#111C2E` in Dark Mode) so sticky note content is crisp and readable in all themes.
- **Scroll-Locked Highlights (`ui/pdf_canvas.py`):** Locked canvas widget dimensions strictly to PDF point dimensions * zoom (`_sync_canvas_size` ignores `_rendered_pixmap.size()`, `set_rendered_image` no longer resizes the sheet, and `_effective_scale()` aligns coordinates 1:1 with `self.rect()` + `CompositionMode_Multiply`). Highlights no longer shift or re-stick when scrolling.

### Added
- **Freehand Pointer Highlighting (`ui/pdf_canvas.py`, `core/pdf_annotations.py`, `ui/viewer_view.py`):** Added `Resaltado libre` (`free_highlight`) tool with translucent Multiply pointer strokes persisted as ISO 32000 `/Ink` with `/CA` & `/ExtGState`, plus direct box-highlight fallback on scanned/non-selectable PDF pages.
- **Expanded Zoom Range (`10% - 1200%`) (`core/pdf_engine.py`, `ui/pdf_canvas.py`, `ui/viewer_view.py`):** Expanded zoom well beyond 300% (`0.10` to `12.0` / 1200%) with adaptive step sizing and a 4096px PDFium raster safety cap (`128 MB` LRU cache).
- **Always-Visible Resizable Sticky Notes, Subtle Guide Lines & Hide-Notes Toggle (`ui/pdf_canvas.py`, `core/pdf_annotations.py`, `core/autosave.py`, `ui/notes_panel.py`, `ui/viewer_view.py`):**
  - Sticky notes (`148x74` default) display category + wrapped content directly on the page with corner/edge resize handle points (`resize_sticky_note`).
  - Persists `/TRODAIAnchor` coordinates (`anchor_pdf`) and renders a subtle dashed guide line + anchor dot connecting each note to its associated text fragment.
  - Added `Ctrl+Shift+N` + Eye/Eye-Off toolbar & `NotesPanel` toggles (`set_notes_visible`) to hide/show all notes cleanly during reading.
- **Direct Attachment Sharing (`core/share_service.py`, `ui/share_dialog.py`, `assets/icons/share.svg`):** Share the active PDF/MD as a real file attachment via Desktop Mail (`MAPISendMailW` via `mapi32.dll`), WhatsApp, Telegram, Microsoft Teams, Webmail (`CF_HDROP` clipboard + URI launcher), or Windows Explorer.

## [0.3.0] - 2026-11-26

### Changed
- **Borderless Floating Buttons, Centered Icons & Zero Clipping (`theme/palette.py`, `ui/widgets.py`, `ui/main_window.py`, `ui/viewer_view.py`, `ui/dictionary_panel.py`, `ui/ai_panel.py`, `ui/notes_panel.py`):**
  - Replaced heavy square boxes on bar, icon, navigation, and secondary actions (`GhostAction`, `ModeTab`, `TopUtilityBtn`, `AskHeaderBtn`, `NotesHeaderBtn`) with clean floating borderless hover surfaces while keeping solid emphasis strictly on `PrimaryAction` buttons.
  - Centered `16x16` polygonal icons cleanly inside uniform `32x32` containers with `iconOnly="true"` and removed TopBar dropdown arrows (`TopUtilityBtn::menu-indicator`).
  - Added wrapping `FlowContainer`, widened `DictionaryPanel` (`296px`), and shortened labels (`"Preguntar IA"`, `"Consultar Wiktionary"`) so no chip or button text clips.
- **Deduplicated TopBar & Viewer + Installer-Fixed Language (`ui/main_window.py`, `ui/viewer_view.py`, `theme/theme_profile.py`, `app.py`):**
  - Removed duplicate TopBar Studio / AI / Fullscreen buttons, redundant Viewer Open / Settings buttons, and the runtime language switcher dropdown; UI language now follows the Windows Installer selection (`HKCU\Software\TRODAI\PDFLector\Language`).
- **Wiktionary Default Dictionary Without HTTP 501 (`core/dictionary_engine.py`, `ui/dictionary_panel.py`):**
  - Fixed Spanish/multilingual Wiktionary lookups (replacing abandoned `es.wiktionary.org/api/rest_v1` HTTP 501 with Wikimedia `w/api.php` wikitext extraction + `en.wiktionary.org/api/rest_v1` fallback + live `opensearch` suggestions) and enabled automatic background Wiktionary queries by default with SQLite caching (automatically paused when `Solo offline` is active).

### Added
- **In-Document Text Search (`Ctrl+F`) (`core/pdf_engine.py`, `ui/pdf_canvas.py`, `ui/viewer_view.py`):** Collapsible find bar with match counter, `Aa` case-sensitive toggle, Next/Prev navigation, PDFium character-box canvas highlighting, and Markdown text search.
- **System Print Dialog (`Ctrl+P`) (`ui/print_dialog.py`, `assets/icons/printer.svg`):** Queries live `QPrinterInfo.availablePrinters()` and per-printer `supportedPageSizes()`, supporting page range (`Todas`, `Actual`, custom `1-3, 5`), copies, orientation, DPI (`150`/`300`/`600`), color/grayscale, scaling, and Windows native driver properties (`QPrintDialog`).
- **Presentation Mode (`F5` / `Ctrl+L`) + Wireless Clicker Support (`ui/presentation_view.py`, `assets/icons/presentation.svg`):** Distraction-free full-screen PDF slide presenter supporting wireless clickers (`PgDn`/`PgUp`/`Space`/`Return`/`B`/`.`/`W`/`Home`/`End`/mouse click) and an auto-hiding bottom-center hover exit HUD (`Esc`).
- **Conventional Shortcuts & Pointer-Reactive Hub Motion (`ui/shortcuts_dialog.py`, `assets/icons/keyboard.svg`, `theme/motion_engine.py`):** Wired `Ctrl+O/S/W/F/P/0/+/-/R/K/Z` + `F1` shortcut reference dialog, and added cursor-reactive Hub hero backdrop motion (disabled in Viewer reading mode).

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
