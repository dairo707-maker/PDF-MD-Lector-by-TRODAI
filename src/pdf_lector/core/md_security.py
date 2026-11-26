"""Markdown HTML sanitization and local resource confinement (nh3 + path_guard)."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from .path_guard import is_unc_or_network_path, resolve_safe_doc_resource


@dataclass(frozen=True)
class MarkdownSanitizeResult:
    """Result of sanitizing Markdown-rendered HTML and confining resources."""

    html: str
    blocked_resources: tuple[str, ...]


ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "br",
        "hr",
        "blockquote",
        "pre",
        "code",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "strong",
        "b",
        "em",
        "i",
        "del",
        "s",
        "u",
        "sub",
        "sup",
        "kbd",
        "mark",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "a",
        "img",
        "div",
        "span",
        "input",
    }
)

CLEAN_CONTENT_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "noscript",
        "template",
        "textarea",
        "title",
    }
)

_DANGEROUS_SCAN_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "iframe",
        "object",
        "embed",
        "svg",
        "meta",
        "form",
        "style",
        "link",
        "base",
        "math",
        "template",
    }
)

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},
    "pre": {"class"},
    "th": {"align"},
    "td": {"align"},
    "h1": {"id"},
    "h2": {"id"},
    "h3": {"id"},
    "h4": {"id"},
    "h5": {"id"},
    "h6": {"id"},
    "input": {"type", "checked", "disabled"},
}

_BLOCKED_SCHEMES = frozenset(
    {"javascript", "vbscript", "data", "file", "smb", "ftp", "ftps"}
)
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_SAFE_LANG_CLASS_RE = re.compile(r"^language-[A-Za-z0-9_+-]+$")
_META_URL_RE = re.compile(r"url\s*=\s*([^\s;\"']+)", flags=re.IGNORECASE)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", flags=re.DOTALL)
_STYLE_DANGEROUS_RE = re.compile(
    r"(?:url|expression|-moz-binding)\s*\(|@import|javascript:|vbscript:|behavior\s*:",
    flags=re.IGNORECASE,
)
_STYLE_URL_RE = re.compile(r"url\s*\(\s*['\"]?([^)'\"\s]+)['\"]?\s*\)", flags=re.IGNORECASE)


def _strip_controls_and_ws(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) >= 0x20 and ch not in " \t\r\n")


def is_safe_anchor(raw_ref: str) -> bool:
    """Return True when raw_ref is a safe intra-document #anchor."""
    ref = (raw_ref or "").strip()
    if not ref.startswith("#") or len(ref) <= 1:
        return False
    if is_unc_or_network_path(ref):
        return False
    if ".." in ref or ":" in ref or "\\" in ref or "/" in ref:
        return False
    return True


def validate_doc_resource_ref(
    raw_ref: str,
    *,
    base_dir: Path | None,
    is_image: bool,
    allow_remote_images: bool = False,
) -> tuple[bool, str, str]:
    """Validate an href or img src reference against local confinement and scheme rules."""
    ref = (raw_ref or "").strip()
    if not ref:
        return False, "", "empty_path"

    if any(ord(ch) < 0x20 for ch in ref):
        return False, "", "invalid_characters"

    if not is_image and is_safe_anchor(ref):
        return True, ref, "ok"

    unquoted = unquote(unquote(ref)).strip()
    compact = _strip_controls_and_ws(unquoted)
    scheme_match = _SCHEME_RE.match(compact)
    scheme = scheme_match.group(1).lower() if scheme_match else ""
    if not scheme:
        parsed = urlparse(compact)
        scheme = (parsed.scheme or "").lower()

    if scheme in ("http", "https"):
        if is_image:
            if allow_remote_images:
                return True, ref, "ok"
            return False, "", "remote_image_blocked"
        return True, ref, "ok"

    if scheme in _BLOCKED_SCHEMES or (len(scheme) > 1 and scheme.isalpha()):
        return False, "", "network_or_url_blocked"

    if is_unc_or_network_path(ref) or is_unc_or_network_path(unquoted) or is_unc_or_network_path(compact):
        return False, "", "network_or_url_blocked"

    # Relative filesystem path: strip optional #fragment after scheme/UNC inspection
    path_part = ref.split("#", 1)[0].strip()
    if not path_part:
        return False, "", "empty_path"

    if base_dir is not None:
        try:
            resolve_safe_doc_resource(base_dir, path_part, check_exists=False)
        except ValueError as exc:
            return False, "", str(exc)
        return True, ref, "ok"

    # Without base_dir, block absolute/drive/traversal refs
    unquoted_part = unquote(unquote(path_part)).strip()
    if unquoted_part.startswith(("/", "\\")) or ":" in unquoted_part:
        return False, "", "traversal_blocked"
    parts = re.split(r"[\\/]+", unquoted_part)
    if any(seg == ".." for seg in parts):
        return False, "", "traversal_blocked"
    return True, ref, "ok"


class _DangerousHtmlScanner(HTMLParser):
    """Pre-scan raw rendered HTML to capture dangerous tags, handlers, and blocked URIs."""

    def __init__(self, *, base_dir: Path | None, allow_remote_images: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._base_dir = base_dir
        self._allow_remote_images = allow_remote_images
        self.blocked: list[str] = []
        self._seen: set[str] = set()

    def _record(self, token: str) -> None:
        clean = (token or "").strip()
        if clean and clean not in self._seen:
            self._seen.add(clean)
            self.blocked.append(clean)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in _DANGEROUS_SCAN_TAGS:
            self._record(f"tag:{tag_lower}")

        for attr_name, attr_val in attrs:
            attr_lower = (attr_name or "").lower()
            val = attr_val or ""
            if attr_lower.startswith("on"):
                self._record(f"attr:{attr_lower}")
                continue
            if attr_lower == "style":
                compact_css = _strip_controls_and_ws(_CSS_COMMENT_RE.sub("", val))
                if _STYLE_DANGEROUS_RE.search(compact_css) or _STYLE_DANGEROUS_RE.search(val):
                    self._record("attr:style")
                    for m_url in _STYLE_URL_RE.finditer(val):
                        inner_ref = m_url.group(1).strip()
                        allowed, _, _ = validate_doc_resource_ref(
                            inner_ref,
                            base_dir=self._base_dir,
                            is_image=True,
                            allow_remote_images=self._allow_remote_images,
                        )
                        if not allowed:
                            self._record(inner_ref)
                continue
            if attr_lower in ("href", "src", "data", "action", "formaction", "poster", "xlink:href"):
                is_img = tag_lower == "img" or attr_lower in ("src", "data", "poster")
                allowed, _, _ = validate_doc_resource_ref(
                    val,
                    base_dir=self._base_dir,
                    is_image=is_img,
                    allow_remote_images=self._allow_remote_images,
                )
                if not allowed:
                    self._record(val or f"{tag_lower}:{attr_lower}")
            elif tag_lower == "meta" and attr_lower == "content":
                m = _META_URL_RE.search(val)
                if m:
                    self._record(m.group(1))


def sanitize_markdown_html(
    raw_html: str,
    *,
    base_dir: Path | None = None,
    allow_remote_images: bool = False,
) -> MarkdownSanitizeResult:
    """Sanitize Markdown-rendered HTML strictly via nh3 and path_guard."""
    import nh3

    html_in = raw_html or ""
    scanner = _DangerousHtmlScanner(
        base_dir=base_dir,
        allow_remote_images=allow_remote_images,
    )
    try:
        scanner.feed(html_in)
        scanner.close()
    except Exception:
        pass

    blocked = list(scanner.blocked)
    seen = set(blocked)

    def _record_blocked(item: str) -> None:
        clean = (item or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            blocked.append(clean)

    def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
        tag_l = tag.lower()
        attr_l = attr.lower()
        if (tag_l == "a" and attr_l == "href") or (tag_l == "img" and attr_l == "src"):
            allowed, norm_ref, _ = validate_doc_resource_ref(
                value,
                base_dir=base_dir,
                is_image=(tag_l == "img"),
                allow_remote_images=allow_remote_images,
            )
            if not allowed:
                _record_blocked(value)
                return None
            return norm_ref
        if tag_l == "input" and attr_l == "type":
            return "checkbox" if value.strip().lower() == "checkbox" else None
        if tag_l in ("h1", "h2", "h3", "h4", "h5", "h6") and attr_l == "id":
            cleaned_id = _SAFE_ID_RE.sub("", value)
            return cleaned_id or None
        if tag_l in ("code", "pre") and attr_l == "class":
            tokens = [tok for tok in value.split() if _SAFE_LANG_CLASS_RE.fullmatch(tok)]
            return " ".join(tokens) if tokens else None
        return value

    cleaned = nh3.clean(
        html_in,
        tags=set(ALLOWED_TAGS),
        clean_content_tags=set(CLEAN_CONTENT_TAGS),
        attributes={k: set(v) for k, v in ALLOWED_ATTRIBUTES.items()},
        attribute_filter=_attribute_filter,
        strip_comments=True,
        link_rel="noopener noreferrer",
        url_schemes={"http", "https"},
    )
    return MarkdownSanitizeResult(html=cleaned, blocked_resources=tuple(blocked))
