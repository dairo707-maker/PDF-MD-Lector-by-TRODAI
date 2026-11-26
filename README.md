# PDF/MD Lector · by TRODAI (`v0.1.0`)

**PDF/MD Lector** is an open-source (MIT), **100% local and privacy-first** dual **PDF and Markdown (`.md` / `.markdown`)** reader, editor, and modular 44-tool PDF suite for **Windows 11**.

Built with Python 3.12+/PySide6, `pypdfium2`, `pikepdf`, `pyHanko`, `markdown-it-py`, and `nh3`, under the **TRODAI** architectural design system (*Obsidian Navy / Crisp White*, `Space Grotesk` + `JetBrains Mono`, `0px` geometry, and 100% polygonal SVGs).

---

## Core Capabilities

- **Mixed PDF + Markdown Viewer & Editor:**
  - Open `.pdf`, `.md`, and `.markdown` tabs side by side with a 3-mode left rail (`Thumbnails | TOC & Bookmarks | Both`).
  - 3-mode Markdown workspace (`Reading | Split | Editor`), formatting toolbar, live `#/##/###` TOC, and `nh3`-sanitized preview (`QTextBrowser`, zero `QtWebEngine*`).
  - Native `Markdown <-> PDF` conversion with bookmarks and configurable DPI/language.
- **ISO 32000 PDF Annotations & Stylus/Touch Ink:**
  - `/Highlight` (6 solid colors), `/Text` sticky notes, `/Ink` strokes (`QTabletEvent` stylus pressure + palm rejection, plus 1-click Touch Ink mode preserving 2-finger pan/zoom), and `/FreeText`/`/Popup` threaded comments (default author `Anonimo`).
  - Non-destructive backup Save, Save As, and Flatten PDF.
- **44 Modular PDF Tools across 8 Categories:**
  - **Dynamic Quick Actions:** 6 default tools (`sign_pdf`, `merge_pdf`, `delete_pages`, `unlock_pdf`, `convert_from_pdf`, `compress_pdf`) automatically re-ranked by `LocalUsageInsights` (and reset on history clear).
  - **Organize & Edit (20)**, **Convert & OCR (5)** (`WinRT Windows.Media.Ocr`, Tesseract probe, optional RapidOCR), **Create & Capture** (`QScreen` region capture, WIA scanner, `QCamera`), **Dual-Profile Invoices** (Colombia DIAN UBL 2.1 XML+CUFE+QR and International Factur-X/ZUGFeRD/XRechnung EN 16931 PDF/A-3), **Optimize & Repair**, **Security & Privacy** (destructive stream/XObject redaction, full XMP/`/Info`/`/PieceInfo` sanitization, AES-256, `pyHanko` PKCS#12), and **View & Compare**.
  - Configurable output resolution (`dpi`: `72/150/200/300/600`/custom `36..1200`), `quality` (`10..100`), and `source_lang` (`auto`, `es`, `en`, `pt`, `fr`, `de`, `it`, `multi`).
- **Offline Kindle-Style Dictionary:**
  - Closed by default; opens on contextual lookup (`Buscar en diccionario`) or button click, and can be detached into a floating window, re-docked, or closed. Includes ES/EN SQLite packs and local `.json` glossary import.
- **Integrations & Local AI:**
  - Windows 11 per-user `HKCU` integration (`TRODAI.PDFMDLector.PDF` / optional `TRODAI.PDFMDLector.MD`), CLI flags (`--merge`, `--compress`, `--ocr`, `--serve-api`, `--native-host`), Chrome/Edge Manifest V3 extension + 4-byte LE Native Messaging (`com.trodai.pdf_md_lector`).
  - Lazy loopback REST/OpenAPI 3.1 server (`127.0.0.1`, OFF by default, DPAPI Bearer token).
  - AI "Ask Document" (local BM25 with page/heading citations + opt-in BYOK OpenAI/Gemini/Claude/Ollama + MCP client) and `pdf-md-lector-mcp` stdio JSON-RPC 2.0 server.
- **$0 Free Code Signing & Reproducible Packaging:**
  - Portable executable (`PDF-MD-Lector-Portable.exe`), non-admin Inno Setup 6 wizard (`PDF-MD-Lector-Setup.exe`, `PrivilegesRequired=lowest`), native PowerShell Authenticode signing (`scripts/sign_local.ps1`), `SHA256SUMS.txt`, SLSA Build Provenance (`actions/attest-build-provenance@v2`), and SignPath.io Free OSS CI template.

---

## Quick Start (Windows 11 / PowerShell)

```powershell
# Inside OneDrive or standard NTFS folders, sync dependencies with copy link mode:
uv sync --extra dev --link-mode=copy

# Launch the GUI
uv run --no-sync python -m pdf_lector.app

# Run the automated test suite (offscreen Qt)
$env:QT_QPA_PLATFORM = "offscreen"
uv run --no-sync pytest -v
```

### CLI Examples

```powershell
uv run --no-sync python -m pdf_lector.app document.pdf
uv run --no-sync python -m pdf_lector.app notes.md
uv run --no-sync python -m pdf_lector.app --merge a.pdf b.pdf -o merged.pdf
uv run --no-sync python -m pdf_lector.app --compress input.pdf -o compact.pdf --quality 60
uv run --no-sync python -m pdf_lector.app --ocr scan.pdf -o searchable.pdf --dpi 300 --lang es
```

---

## Building & Signing (`$0` Workflow)

```powershell
# 1. Build Portable EXE, Inno Setup 6 Installer, and dist\SHA256SUMS.txt
uv run --no-sync python scripts/build_exe.py

# 2. Preview local Authenticode signing (safe dry-run, touches zero certificate stores)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sign_local.ps1 -DryRun

# 3. Sign local build artifacts with a per-user self-signed Code Signing cert
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sign_local.ps1
```

See [docs/CODE_SIGNING_AND_INSTALL.md](docs/CODE_SIGNING_AND_INSTALL.md) for `-InstallTrust` / `-UninstallTrust`, `SHA256SUMS.txt` verification, SLSA attestation checks, and SignPath.io Free OSS setup.

---

## Documentation Index

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — System design, 2-phase boot, and Architecture Decision Records (ADRs)
- [docs/API_AND_MCP.md](docs/API_AND_MCP.md) — CLI, Loopback REST/OpenAPI 3.1, Browser MV3 Native Messaging, MCP stdio, and AI RAG
- [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md) — Threat model, `path_guard`, DPAPI, `nh3`, redaction, and PII anonymization
- [docs/CODE_SIGNING_AND_INSTALL.md](docs/CODE_SIGNING_AND_INSTALL.md) — `$0` code signing, Inno Setup 6 (`PrivilegesRequired=lowest`), and SLSA verification
- [docs/AUDIT_HANDOFF.md](docs/AUDIT_HANDOFF.md) — Multi-AI and external security auditor handoff guide
- [docs/HANDOFF_STATUS.md](docs/HANDOFF_STATUS.md) — Live milestone checkpoint log
- [CLAUDE.md](CLAUDE.md) & [AGENTS.md](AGENTS.md) — Multi-AI context and strict coding rules
- [CONTRIBUTING.md](CONTRIBUTING.md) & [SECURITY.md](SECURITY.md) — OSS contribution and vulnerability disclosure guidelines

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Dairo Trocha (TRODAI).
