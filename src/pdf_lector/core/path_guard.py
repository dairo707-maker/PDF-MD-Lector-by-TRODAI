"""Strict local-disk path validation protecting user privacy and preventing NTLM/network leaks."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from .win32_bridge import is_cloud_only_file, is_remote_drive

ALLOWED_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_PDF_EXTENSIONS = {".pdf"}
ALLOWED_MD_EXTENSIONS = {".md", ".markdown"}
ALLOWED_ANY_DOC_EXTENSIONS = ALLOWED_PDF_EXTENSIONS | ALLOWED_MD_EXTENSIONS
ALLOWED_GLOSSARY_EXTENSIONS = {".json"}
MAX_MEDIA_BYTES = 25 * 1024 * 1024  # 25 MB safety limit for custom banners

_BLOCKED_URL_SCHEMES = {
    "http",
    "https",
    "ftp",
    "ftps",
    "smb",
    "file",
    "data",
    "javascript",
    "vbscript",
}


def _strip_ascii_controls(value: str) -> str:
    return "".join(ch for ch in value if ord(ch) >= 0x20)


def _has_control_chars(value: str) -> bool:
    return "\x00" in value or any(ord(ch) < 0x20 for ch in value)


def _has_ntfs_ads(text: str) -> bool:
    # Allow Windows drive letter colon at index 1 (e.g. C:\file.md), block any subsequent colon.
    if len(text) >= 2 and text[0].isalpha() and text[1] == ":":
        return ":" in text[2:]
    return ":" in text


def is_unc_or_network_path(raw: str | Path) -> bool:
    """Return True when input looks like a UNC path, device path, URL, or remote mapped drive."""
    text = str(raw).strip()
    if not text:
        return False

    decoded = unquote(unquote(text))
    for candidate in (text, decoded):
        norm = candidate.replace("/", "\\").lower()
        if norm.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
            return True

        cleaned = _strip_ascii_controls(candidate).strip()
        if "://" in cleaned:
            return True

        parsed = urlparse(cleaned)
        if parsed.scheme:
            scheme_lower = parsed.scheme.lower()
            if scheme_lower in _BLOCKED_URL_SCHEMES or len(parsed.scheme) > 1:
                return True

    try:
        if is_remote_drive(Path(text)):
            return True
    except Exception:
        return True

    return False


def _validate_local_file(
    raw: str | Path,
    allowed_extensions: set[str],
    *,
    check_exists: bool = True,
    max_bytes: int | None = None,
) -> tuple[bool, str]:
    text = str(raw).strip()
    if not text:
        return False, "empty_path"
    if _has_control_chars(text):
        return False, "invalid_characters"
    if is_unc_or_network_path(text):
        return False, "network_or_url_blocked"

    p = Path(text)
    if p.drive and not p.is_absolute():
        return False, "invalid_characters"
    if _has_ntfs_ads(text):
        return False, "invalid_characters"

    if p.suffix.lower() not in allowed_extensions:
        return False, "unsupported_extension"

    if check_exists:
        try:
            resolved = p.resolve(strict=True)
        except OSError:
            return False, "file_not_found"
        if not resolved.is_file():
            return False, "not_a_file"
        if is_unc_or_network_path(resolved):
            return False, "network_or_url_blocked"
        if is_cloud_only_file(resolved):
            return False, "cloud_only_blocked"
        if max_bytes is not None:
            try:
                if resolved.stat().st_size > max_bytes:
                    return False, "file_too_large"
            except OSError:
                return False, "stat_failed"

    return True, "ok"


def validate_local_media_path(raw: str | Path, *, check_exists: bool = True) -> tuple[bool, str]:
    """Validate that a banner image/GIF/WebP lives safely on local disk with an allowed extension."""
    return _validate_local_file(
        raw,
        ALLOWED_MEDIA_EXTENSIONS,
        check_exists=check_exists,
        max_bytes=MAX_MEDIA_BYTES,
    )


def validate_local_pdf_path(raw: str | Path, *, check_exists: bool = True) -> tuple[bool, str]:
    """Validate that a PDF path resides on local disk (blocks UNC/network shares and URLs)."""
    return _validate_local_file(
        raw,
        ALLOWED_PDF_EXTENSIONS,
        check_exists=check_exists,
    )


def validate_local_markdown_path(raw: str | Path, *, check_exists: bool = True) -> tuple[bool, str]:
    """Validate that a Markdown (.md/.markdown) path resides safely on local disk."""
    return _validate_local_file(
        raw,
        ALLOWED_MD_EXTENSIONS,
        check_exists=check_exists,
    )


def validate_local_any_doc(raw: str | Path, *, check_exists: bool = True) -> tuple[bool, str]:
    """Validate that a PDF or Markdown document path resides safely on local disk."""
    return _validate_local_file(
        raw,
        ALLOWED_ANY_DOC_EXTENSIONS,
        check_exists=check_exists,
    )


def validate_local_glossary_path(raw: str | Path, *, check_exists: bool = True) -> tuple[bool, str]:
    """Validate that a local JSON glossary file path resides safely on local disk."""
    return _validate_local_file(
        raw,
        ALLOWED_GLOSSARY_EXTENSIONS,
        check_exists=check_exists,
    )


def resolve_safe_doc_resource(base_dir: Path, raw_ref: str, *, check_exists: bool = False) -> Path:
    """Resolve a relative local document asset/link strictly confined within `base_dir`.

    Expects a filesystem path/reference (callers should strip URL fragments `#section`
    only after scheme inspection if needed). Raises ValueError with a reason code on
    any unsafe path, UNC share, URL scheme, drive-relative ref, NTFS ADS, or traversal.
    """
    base_str = str(base_dir).strip()
    if not base_str or is_unc_or_network_path(base_dir):
        raise ValueError("invalid_base_dir")

    try:
        base_resolved = Path(base_dir).resolve(strict=False)
    except OSError as exc:
        raise ValueError("invalid_base_dir") from exc

    if is_unc_or_network_path(base_resolved):
        raise ValueError("invalid_base_dir")

    ref = str(raw_ref).strip()
    if not ref:
        raise ValueError("empty_path")
    if _has_control_chars(ref):
        raise ValueError("invalid_characters")

    unquoted = unquote(unquote(ref)).strip()
    if not unquoted:
        raise ValueError("empty_path")
    if _has_control_chars(unquoted):
        raise ValueError("invalid_characters")

    if is_unc_or_network_path(ref) or is_unc_or_network_path(unquoted):
        raise ValueError("network_or_url_blocked")

    for item in (ref, unquoted):
        if item.startswith(("/", "\\")):
            raise ValueError("traversal_blocked")
        p_item = Path(item)
        if p_item.is_absolute() or p_item.drive:
            raise ValueError("traversal_blocked")
        if ":" in item:
            raise ValueError("invalid_characters")

    try:
        candidate = (base_resolved / unquoted).resolve(strict=False)
    except OSError as exc:
        raise ValueError("invalid_characters") from exc

    if candidate == base_resolved or not candidate.is_relative_to(base_resolved):
        raise ValueError("traversal_blocked")

    if is_unc_or_network_path(candidate):
        raise ValueError("network_or_url_blocked")

    if check_exists:
        if not candidate.exists():
            raise ValueError("file_not_found")
        if not candidate.is_file():
            raise ValueError("not_a_file")

    if candidate.exists() and is_cloud_only_file(candidate):
        raise ValueError("cloud_only_blocked")

    return candidate
