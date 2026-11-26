# Security & Privacy Architecture — PDF/MD Lector by TRODAI

## 1. Privacy Principles

1. **100% Local by Default:** Reading, editing, annotating, converting, redacting, sanitizing, OCR (WinRT/Tesseract/RapidOCR), dictionary lookups, and BM25 document Q&A execute completely offline.
2. **Zero Hidden Telemetry:** `LocalUsageInsights` (`core/usage_insights.py`) stores only aggregate tool/category counters in `%LOCALAPPDATA%\TRODAI\PDFLector\usage_insights.json` and never records document paths, filenames, search queries, or text content.
3. **Neutral Default Annotation Identity:** Native ISO 32000 PDF comments (`core/pdf_annotations.py`) default `/T` author to `"Anónimo"` and never auto-expose the Windows OS username without explicit configuration.
4. **Explicit Consent Gates:** Update checks/downloads (`Solo offline` toggle in `updater.py`), dictionary pack downloads, cloud BYOK LLM calls, external MCP servers, online e-invoice connectors, and support report submissions require explicit user action/consent.

---

## 2. Local Disk & Path Confinement (`src/pdf_lector/core/path_guard.py`)

Every user-supplied document, resource, glossary, and Theme Studio banner path passes `path_guard`:

- **Blocked Network / UNC / Device Paths:** `\\server\share`, `//server/share`, `\\?\UNC\...`, `\\.\PhysicalDrive0`, and mapped network drives (`win32_bridge.is_remote_drive`).
- **Blocked URI Schemes:** `http`, `https`, `ftp`, `ftps`, `smb`, `file`, `data`, `javascript`, `vbscript` (including URL-encoded and double-encoded variants).
- **Blocked Filesystem Traps:** `..` directory traversal outside `base_dir` (`resolve_safe_doc_resource`), NTFS Alternate Data Streams (`:stream`), ASCII control characters / NUL bytes (`\x00`), reparse points/symlinks, and cloud-only OneDrive placeholders where disallowed (`is_cloud_only_file`).
- **Media Size Cap:** Theme Studio banner assets are capped at `25 MB` (`MAX_MEDIA_BYTES`).

---

## 3. Markdown XSS & Tracker Protection (`src/pdf_lector/core/md_security.py`)

- Markdown is parsed with `markdown-it-py` and sanitized with `nh3` using a strict `ALLOWED_TAGS` set and attribute allowlist.
- Dangerous tags (`<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<svg>`, `<math>`, `<meta>`, `<textarea>`) and inline event handlers (`onerror=`, `onload=`, `onclick=`) are stripped.
- Links with `javascript:`, `vbscript:`, `data:`, UNC paths, or traversal are blocked and recorded in `blocked_resources`.
- Remote `http://` and `https://` images are blocked by default (`allow_remote_images=False`) to prevent tracking pixels.
- `QTextBrowser` runs with `setOpenExternalLinks(False)` and an overridden `loadResource()` confined via `resolve_safe_doc_resource()`.

---

## 4. Credential Protection (`src/pdf_lector/core/secrets_store.py`)

- Sensitive values (BYOK API keys, Local REST Bearer tokens, optional online connector tokens) are encrypted with Windows **DPAPI** (`crypt32.dll` `CryptProtectData` / `CryptUnprotectData` bound to the current Windows user) and stored in `%LOCALAPPDATA%\TRODAI\PDFLector\secrets.json`.
- Plaintext secrets are never written to `secrets.json`, crash logs, or support reports, and corrupt/tampered ciphertext blobs fail safely without crashing.

---

## 5. Destructive Redaction & Metadata Sanitization

- **Content-Stream Redaction (`src/pdf_lector/core/redaction.py`):** Removes underlying text operators, glyphs, vector paths, and image objects inside target rectangles—including nested **Form XObjects**—using `pikepdf` + `pypdfium2` geometry rather than covering text with opaque rectangles.
- **Metadata & XMP Sanitization (`src/pdf_lector/core/metadata_sanitizer.py`):** Strips `/Info` keys, Catalog/page/stream `/Metadata` (XMP), `/PieceInfo`, `/SpiderInfo`, `/Measure`, `/VP`, and optional `/StructTreeRoot`/embedded files, followed by a full non-incremental PDF rewrite so deleted objects cannot be recovered from prior PDF revisions.

---

## 6. Anonymized Support Reports & Cloudflare Worker

- `src/pdf_lector/core/feedback_reporter.py` scrubs Windows/Unix usernames (`C:\Users\<usuario>`), OneDrive organization names (`OneDrive - <organizacion>`), UNC paths, local `.pdf`/`.md`/`.markdown` paths, email addresses, IPv4/IPv6 addresses, and caps payloads (`MAX_PAYLOAD_BYTES`) while scrubbing both head and tail of oversized traces.
- The user always reviews an editable full preview before saving locally (`.md`/`.json`), copying to clipboard, opening `mailto:`, or submitting over HTTPS (`http://` remote endpoints are blocked).
- `deploy/feedback-worker/worker.js` enforces a second server-side PII check (`hasUnredactedPii`) returning HTTP `422` if unredacted patterns appear, and holds the GitHub token strictly as an encrypted Cloudflare Worker secret (`GITHUB_TOKEN`), never in source code.

---

## 7. Temporary File Hygiene & Child Process Isolation

- `TempManager` (`core/temp_manager.py`) isolates task directories, wipes sensitive temporary payloads prior to unlink (`secure=True`), and removes session directories on task completion and exit.
- Subprocess invocations strip `_MEIPASS*` and `PYTHONHOME` variables to prevent DLL/environment hijacking from frozen PyInstaller bundles.
