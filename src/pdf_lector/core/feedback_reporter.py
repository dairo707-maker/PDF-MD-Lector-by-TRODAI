"""Strict PII-anonymized in-app feedback & crash reporter (zero silent sends)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
from typing import TYPE_CHECKING, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import uuid

from .. import __version__ as _PKG_VERSION
from .win32_bridge import atomic_write_text, default_app_data_dir

if TYPE_CHECKING:
    from .crash_guard import CrashGuard

GITHUB_REPO = "dairo707-maker/PDF-MD-Lector-by-TRODAI"
ISSUE_BASE_URL = f"https://github.com/{GITHUB_REPO}/issues/new"
MAX_ISSUE_URL_CHARS = 3000
MAX_MAILTO_CHARS = 1800
MAX_PAYLOAD_BYTES = 24_000
DEFAULT_SUPPORT_EMAIL = "soporte@trodai.dev"

KIND_BUG = "bug"
KIND_SUGGESTION = "suggestion"
KIND_CRASH = "crash"
KIND_ERROR = "error"

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_UNC_PREFIX_RE = re.compile(
    r"(?i)"
    r"(?:(\\\\\?\\UNC[\\/])(?!<equipo>)[A-Za-z0-9_.-]+[\\/]|"
    r"(?<![A-Za-z0-9:/\\])(\\\\|//)(?![?.<])[A-Za-z0-9_.-]+[\\/])"
    r"(?!<dato_privado>)"
    r"(?:[^\\/:*?\"'<>|\r\n]+?(?=[\\/])|[^\s\\/:*?\"'<>|\r\n]+)"
)
_WIN_USER_PATH_RE = re.compile(
    r"(?i)((?:[A-Za-z]:[\\/]+|\\)Users[\\/]+)(?!<usuario>)"
    r"(?:[^\\/:*?\"'<>|\r\n]+?(?=[\\/])|[^\s\\/:*?\"'<>|\r\n]+)"
)
_POSIX_USER_PATH_RE = re.compile(
    r"(/(?:Users|home)/)(?!<usuario>)"
    r"(?:[^/:\"'|<>\r\n]+?(?=/)|[^\s/:\"'|<>\r\n]+)"
)
_ONEDRIVE_ORG_RE = re.compile(
    r"(?i)(OneDrive[ \t]*-[ \t]*)(?![ \t]*<organizacion>)([^\s\\/\n\r|\"'<>][^\\/\n\r|\"'<>]*)"
)
_IPV4_RE = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d{1,5})?(?![\d.])"
)
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9:])\[?[0-9A-Fa-f:]{2,}(?:(?:\d{1,3}\.){3}\d{1,3})?"
    r"(?:%[A-Za-z0-9_.-]+)?\]?(?::\d{1,5})?(?![A-Za-z0-9:]|\.\d)"
)
_SAFE_PATH_TOKEN = r"<(?:usuario|organizacion|ruta_usuario|equipo|dominio|dato_privado)>"
_PATH_CHAR = (
    rf"(?:{_SAFE_PATH_TOKEN}|"
    rf"(?!\.\s|,\s|;\s|:\s|"
    rf"\s+(?:[A-Za-z]:[\\/]|\\\\|file://|https?://|/(?:Users|home|var|tmp|etc|opt|mnt|media|root|Volumes|private|Applications|srv|usr)/)|"
    rf"\.[A-Za-z]{{1,8}}(?:\s|[,;)\]}}]|$))[^\"'<>|*?\r\n])"
)
_DOC_PATH_RE = re.compile(
    rf"(?i)(?:"
    rf"file://{_PATH_CHAR}+?|"
    rf"\\\\\?\\UNC\\[A-Za-z0-9_.-]+[\\/]{_PATH_CHAR}+?|"
    rf"\\\\\?\\[A-Za-z]:[\\/]{_PATH_CHAR}+?|"
    rf"\\\\[A-Za-z0-9_.-]+[\\/]{_PATH_CHAR}+?|"
    rf"[A-Za-z]:[\\/]{_PATH_CHAR}+?|"
    rf"(?<![A-Za-z0-9._:/\\-])/(?:Users|home|var|tmp|etc|opt|mnt|media|root|Volumes|private|Applications|srv|usr)/{_PATH_CHAR}+?|"
    rf"(?<![A-Za-z0-9._:/\\-])/(?:{_SAFE_PATH_TOKEN}|[^\s\"'<>])+"
    rf")\.(pdf|md|markdown)\b"
)
_DOC_FILE_RE = re.compile(
    r'(?i)(?<![<\w])([^\s\\/:*?"<>|(\[{\'`][^\s\\/:*?"<>|]*\.(pdf|md|markdown))(?![>\w])'
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mask_ipv6_match(match: re.Match[str]) -> str:
    token = match.group(0)
    if ":" not in token:
        return token

    candidate = token
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1:
        # e.g. host:port - not IPv6
        return token

    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]

    # Reject time strings like 12:34:56 (no :: and only 2 colons)
    if "::" not in candidate and candidate.count(":") < 3:
        return token

    try:
        ipaddress.IPv6Address(candidate)
        return "<ip>"
    except ValueError:
        return token


def _mask_doc_path_match(match: re.Match[str]) -> str:
    ext = match.group(1).lower()
    return f"<documento.{ext}>"


def _mask_doc_file_match(match: re.Match[str]) -> str:
    full = match.group(1)
    ext = match.group(2).lower()
    if full.lower() == f"documento.{ext}":
        return full
    return f"<documento.{ext}>"


def _mask_unc_prefix_match(match: re.Match[str]) -> str:
    unc_prefix = match.group(1)
    if unc_prefix:
        sep = "\\" if "\\" in unc_prefix else "/"
        return f"{unc_prefix}<equipo>{sep}<dato_privado>"
    slash_prefix = match.group(2) or "\\\\"
    sep = "\\" if "\\" in slash_prefix else "/"
    return f"{slash_prefix}<equipo>{sep}<dato_privado>"


def anonymize_text(text: str, *, extra_sensitive: Iterable[str] = ()) -> str:
    """Strictly redact PII, machine names, emails, IPs, and private PDF/MD paths."""
    if not text:
        return ""
    out = str(text)

    # 1. Emails
    out = _EMAIL_RE.sub("<correo>", out)

    # 2. Mask full private .pdf/.md/.markdown paths before user/org token replacement
    out = _DOC_PATH_RE.sub(_mask_doc_path_match, out)

    # 3. Non-document UNC prefixes and user directories on any Windows drive/UNC or POSIX /Users / /home
    out = _UNC_PREFIX_RE.sub(_mask_unc_prefix_match, out)
    out = _WIN_USER_PATH_RE.sub(r"\1<usuario>", out)
    out = _POSIX_USER_PATH_RE.sub(r"\1<usuario>", out)

    # 4. OneDrive organization names
    out = _ONEDRIVE_ORG_RE.sub(r"\1<organizacion>", out)

    # 5. Environment path vars first (longest), then short tokens
    env_path_vars = ("USERPROFILE", "ONEDRIVE", "OneDriveCommercial", "OneDriveConsumer")
    path_vals = [os.environ.get(v, "").strip() for v in env_path_vars]
    for pval in sorted((v for v in path_vals if len(v) >= 4), key=len, reverse=True):
        out = re.sub(re.escape(pval), "<ruta_usuario>", out, flags=re.IGNORECASE)

    token_map = (
        ("USERNAME", "<usuario>"),
        ("COMPUTERNAME", "<equipo>"),
        ("USERDOMAIN", "<dominio>"),
    )
    for var_name, marker in token_map:
        val = os.environ.get(var_name, "").strip()
        if len(val) >= 3:
            out = re.sub(rf"(?i)(?<![\w]){re.escape(val)}(?![\w])", marker, out)

    for item in extra_sensitive:
        sval = str(item).strip()
        if len(sval) >= 3:
            out = re.sub(re.escape(sval), "<dato_privado>", out, flags=re.IGNORECASE)

    # 6. IPv6 (including IPv4-mapped ::ffff:x.x.x.x) BEFORE IPv4; verified with ipaddress.IPv6Address
    out = _IPV6_CANDIDATE_RE.sub(_mask_ipv6_match, out)

    # 7. Strict octet-bounded IPv4 (does not mangle versions like 0.1.0 or builds like 10.0.26100.1742)
    out = _IPV4_RE.sub("<ip>", out)

    # 8. Mask any remaining standalone document paths & filenames
    out = _DOC_PATH_RE.sub(_mask_doc_path_match, out)
    out = _DOC_FILE_RE.sub(_mask_doc_file_match, out)

    return out


@dataclass
class FeedbackReport:
    report_id: str
    kind: str
    title: str
    description: str
    markdown_body: str
    app_version: str
    os_label: str
    created_at: str
    diagnostics_included: bool


def build_feedback_report(
    *,
    kind: str = KIND_BUG,
    title: str = "",
    description: str = "",
    crash_guard: CrashGuard | None = None,
    error_text: str = "",
    include_diagnostics: bool = True,
    auto_restarted: bool = False,
    extra_sensitive: Iterable[str] = (),
    app_version: str = "",
) -> FeedbackReport:
    """Create an editable, PII-anonymized FeedbackReport."""
    extra = list(extra_sensitive)
    clean_kind = kind if kind in (KIND_BUG, KIND_SUGGESTION, KIND_CRASH, KIND_ERROR) else KIND_BUG
    ver = app_version or (crash_guard.version if crash_guard is not None else _PKG_VERSION)
    os_label = f"{platform.system()} {platform.release()}"
    created_at = _utc_now_iso()
    report_id = uuid.uuid4().hex[:10]

    clean_title = anonymize_text(title or f"Reporte ({clean_kind})", extra_sensitive=extra)
    clean_desc = anonymize_text(description or "", extra_sensitive=extra)
    clean_err = anonymize_text(error_text or "", extra_sensitive=extra)

    sections: list[str] = [
        f"## {clean_title}",
        "",
        f"- **Tipo:** `{clean_kind}`",
        f"- **Version:** `{ver}`",
        f"- **Sistema:** `{os_label}`",
        f"- **Fecha:** `{created_at}`",
        f"- **Reinicio automatico:** `{'si' if auto_restarted else 'no'}`",
        "",
        "### Descripcion",
        clean_desc or "(Sin descripcion adicional)",
    ]

    if clean_err:
        sections.extend(["", "### Error", "```text", clean_err, "```"])

    if include_diagnostics and crash_guard is not None:
        diag = anonymize_text(crash_guard.build_diagnostic_text(), extra_sensitive=extra)
        sections.extend(["", "### Diagnostico anonimizado", "```text", diag, "```"])

    body = anonymize_text("\n".join(sections), extra_sensitive=extra)
    return FeedbackReport(
        report_id=report_id,
        kind=clean_kind,
        title=clean_title,
        description=clean_desc,
        markdown_body=body,
        app_version=ver,
        os_label=os_label,
        created_at=created_at,
        diagnostics_included=bool(include_diagnostics and crash_guard is not None),
    )


def build_clipboard_payload(
    report_or_text: FeedbackReport | str,
    *,
    extra_sensitive: Iterable[str] = (),
) -> str:
    """Return strictly anonymized text ready for clipboard copy."""
    raw = (
        report_or_text.markdown_body
        if isinstance(report_or_text, FeedbackReport)
        else str(report_or_text)
    )
    return anonymize_text(raw, extra_sensitive=extra_sensitive)


def save_local_report(
    report: FeedbackReport,
    *,
    reports_dir: Path | None = None,
    app_dir: Path | None = None,
    edited_body: str | None = None,
    extra_sensitive: Iterable[str] = (),
) -> tuple[Path, Path]:
    """Save anonymized .md and .json copies under %LOCALAPPDATA%\\TRODAI\\PDFLector\\reports\\."""
    if reports_dir is not None:
        target_dir = Path(reports_dir)
    elif app_dir is not None:
        target_dir = Path(app_dir) / "reports"
    else:
        target_dir = default_app_data_dir() / "reports"
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_title = anonymize_text(report.title, extra_sensitive=extra_sensitive)
    safe_desc = anonymize_text(report.description, extra_sensitive=extra_sensitive)
    safe_body = anonymize_text(
        edited_body if edited_body is not None else report.markdown_body,
        extra_sensitive=extra_sensitive,
    )

    ts_slug = report.created_at.replace(":", "").replace("-", "")
    stem = f"report_{ts_slug}_{report.report_id}"
    md_path = target_dir / f"{stem}.md"
    json_path = target_dir / f"{stem}.json"

    payload = asdict(report)
    payload["title"] = safe_title
    payload["description"] = safe_desc
    payload["markdown_body"] = safe_body

    atomic_write_text(md_path, safe_body + "\n")
    atomic_write_text(json_path, json.dumps(payload, indent=2, ensure_ascii=False))
    return md_path, json_path


def _truncate_preserving_lines(text: str, max_len: int, suffix: str = "") -> tuple[str, bool]:
    if len(text) <= max_len:
        return text, False
    budget = max(0, max_len - len(suffix) - 20)
    lines = text.splitlines()
    kept: list[str] = []
    used = 0
    for line in lines:
        add_len = len(line) + (1 if kept else 0)
        if used + add_len > budget:
            break
        kept.append(line)
        used += add_len
    trimmed = "\n".join(kept)
    if trimmed.count("```") % 2 == 1:
        trimmed += "\n```"
    if suffix:
        trimmed = f"{trimmed}\n{suffix}" if trimmed else suffix
    return trimmed[:max_len], True


def submit_feedback_endpoint(
    report: FeedbackReport,
    endpoint_url: str,
    *,
    edited_body: str | None = None,
    timeout_s: float = 10.0,
    allow_localhost: bool = False,
    extra_sensitive: Iterable[str] = (),
    transport: Callable[[str, bytes, dict[str, str], float], tuple[int, str]] | None = None,
) -> tuple[bool, str]:
    """Explicitly submit an anonymized report to a configurable HTTPS endpoint (zero embedded tokens)."""
    url = str(endpoint_url).strip()
    if not url:
        return False, "empty_endpoint_url"

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        pass
    elif scheme == "http" and allow_localhost and host in {"127.0.0.1", "localhost", "::1"}:
        pass
    else:
        return False, "insecure_endpoint_blocked"

    safe_title = anonymize_text(report.title, extra_sensitive=extra_sensitive)[:200]
    safe_body = anonymize_text(
        edited_body if edited_body is not None else report.markdown_body,
        extra_sensitive=extra_sensitive,
    )

    payload_dict = {
        "report_id": report.report_id,
        "kind": report.kind,
        "title": safe_title,
        "body": safe_body,
        "version": report.app_version[:40],
        "os": report.os_label[:80],
        "created_at": report.created_at,
    }
    raw_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
    if len(raw_bytes) > MAX_PAYLOAD_BYTES:
        overshoot = len(raw_bytes) - MAX_PAYLOAD_BYTES + 256
        safe_body, _ = _truncate_preserving_lines(safe_body, max(256, len(safe_body) - overshoot))
        payload_dict["body"] = safe_body
        raw_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        if len(raw_bytes) > MAX_PAYLOAD_BYTES:
            return False, "payload_too_large"

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": f"PDF-MD-Lector/{report.app_version}",
    }

    if transport is not None:
        try:
            status, resp_text = transport(url, raw_bytes, headers, float(timeout_s))
        except Exception as exc:
            return False, f"transport_error:{exc}"
        if 200 <= status < 300:
            return True, resp_text or f"http_{status}"
        return False, f"http_{status}:{resp_text[:200]}"

    req = Request(url, data=raw_bytes, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=float(timeout_s)) as resp:
            status = getattr(resp, "status", 200)
            body_txt = resp.read().decode("utf-8", errors="replace")
            if 200 <= status < 300:
                return True, body_txt or f"http_{status}"
            return False, f"http_{status}:{body_txt[:200]}"
    except HTTPError as exc:
        return False, f"http_{exc.code}:{exc.reason}"
    except URLError as exc:
        return False, f"network_error:{exc.reason}"
    except Exception as exc:
        return False, f"submit_error:{exc}"


def build_mailto_uri(
    title: str,
    body: str,
    *,
    recipient: str = DEFAULT_SUPPORT_EMAIL,
    max_chars: int = MAX_MAILTO_CHARS,
    extra_sensitive: Iterable[str] = (),
) -> tuple[str, bool]:
    """Build a length-capped, PII-anonymized mailto: URI."""
    safe_title = anonymize_text(title, extra_sensitive=extra_sensitive)
    safe_body = anonymize_text(body, extra_sensitive=extra_sensitive)
    prefix = f"mailto:{recipient}?subject={quote(safe_title)}&body="
    remaining_encoded = max(64, max_chars - len(prefix))

    # Iteratively fit URL-encoded body within max_chars
    truncated = False
    candidate = safe_body
    note = "\n[Reporte truncado: usa Copiar al portapapeles para adjuntar el detalle completo.]"
    if len(quote(candidate)) > remaining_encoded:
        truncated = True
        lines = candidate.splitlines()
        kept: list[str] = []
        for line in lines:
            test = "\n".join([*kept, line]) + note
            if len(quote(test)) > remaining_encoded:
                break
            kept.append(line)
        candidate = ("\n".join(kept) + note) if kept else note.strip()
        while len(quote(candidate)) > remaining_encoded and len(candidate) > 8:
            candidate = candidate[: int(len(candidate) * 0.8)]

    return f"{prefix}{quote(candidate)}", truncated


def build_github_issue_url(
    title: str,
    body: str,
    *,
    labels: tuple[str, ...] = ("feedback",),
    max_chars: int = MAX_ISSUE_URL_CHARS,
    extra_sensitive: Iterable[str] = (),
) -> tuple[str, bool]:
    """Build a length-capped, PII-anonymized GitHub Issues URL closing open code fences."""
    safe_title = anonymize_text(title, extra_sensitive=extra_sensitive)
    safe_body = anonymize_text(body, extra_sensitive=extra_sensitive)
    label_str = ",".join(labels) if labels else ""
    prefix = f"{ISSUE_BASE_URL}?title={quote(safe_title)}"
    if label_str:
        prefix += f"&labels={quote(label_str)}"
    prefix += "&body="

    remaining_encoded = max(80, max_chars - len(prefix))
    truncated = False
    candidate = safe_body
    if len(quote(candidate)) > remaining_encoded:
        truncated = True
        lines = candidate.splitlines()
        kept: list[str] = []
        for line in lines:
            trial = "\n".join([*kept, line])
            if trial.count("```") % 2 == 1:
                trial += "\n```"
            if len(quote(trial)) > remaining_encoded:
                break
            kept.append(line)
        candidate = "\n".join(kept)
        if candidate.count("```") % 2 == 1:
            candidate += "\n```"
        while len(quote(candidate)) > remaining_encoded and len(candidate) > 8:
            candidate = candidate[: int(len(candidate) * 0.8)]
            if candidate.count("```") % 2 == 1:
                candidate += "\n```"

    return f"{prefix}{quote(candidate)}", truncated
