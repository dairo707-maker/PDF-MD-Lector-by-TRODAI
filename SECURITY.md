# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `0.1.x` | Yes |

## Privacy & Security Guarantees

PDF/MD Lector by TRODAI is designed as a **100% local, zero-telemetry** desktop application:

- **Path Guard (`src/pdf_lector/core/path_guard.py`):** Blocks UNC paths (`\\...`, `//...`, `\\?\UNC\...`), mapped remote drives, cloud-only placeholders where applicable, NTFS Alternate Data Streams, traversal (`..`), and network/script URI schemes (`http`, `https`, `file`, `smb`, `ftp`, `javascript`, `data`, `vbscript`).
- **Markdown XSS & Tracker Defense (`src/pdf_lector/core/md_security.py`):** Sanitizes rendered HTML using an `nh3` strict tag/attribute allowlist and confines resources inside `QTextBrowser` (`setOpenExternalLinks(False)`), blocking remote images and scripts by default.
- **Credential Encryption (`src/pdf_lector/core/secrets_store.py`):** Encrypts BYOK AI keys, local REST API Bearer tokens, and optional connector credentials at rest via Windows DPAPI (`CryptProtectData`).
- **Destructive Redaction & Metadata Sanitization:** `core/redaction.py` deletes underlying PDF content-stream and Form XObject primitives rather than drawing opaque overlays; `core/metadata_sanitizer.py` removes `/Info`, Catalog `/Metadata` (XMP), `/PieceInfo`, and `/SpiderInfo` with a full PDF rewrite.
- **Loopback API & MCP Boundaries:** The optional local REST server binds strictly to `127.0.0.1` (OFF by default) and requires constant-time (`hmac.compare_digest`) Bearer authentication. The `pdf-md-lector-mcp` stdio server gates all file operations through `path_guard`.
- **Anonymized Support Reports:** `core/feedback_reporter.py` scrubs Windows usernames, hostnames, OneDrive/personal paths, emails, and IP addresses before showing an editable preview, and `deploy/feedback-worker/worker.js` rejects unredacted PII server-side.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead:
1. Use GitHub Private Security Advisories on `github.com/dairo707-maker/PDF-MD-Lector-by-TRODAI` (once published), or contact the maintainer (**Dairo Trocha / TRODAI**) privately with reproduction steps, affected module/version, and impact assessment.
2. You will receive an acknowledgment within 72 hours and coordinated disclosure once a verified fix and regression test are prepared.
