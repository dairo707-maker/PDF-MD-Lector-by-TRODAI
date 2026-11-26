"""Local BM25 document RAG index and consent-gated BYOK LLM Q&A for PDF and Markdown."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, Callable
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .path_guard import validate_local_markdown_path, validate_local_pdf_path
from .secrets_store import delete_secret, get_secret, set_secret

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

PROVIDER_SECRET_KEYS: dict[str, str] = {
    "openai": "byok_openai_key",
    "openai_compatible": "byok_openai_key",
    "gemini": "byok_gemini_key",
    "anthropic": "byok_anthropic_key",
    "claude": "byok_anthropic_key",
    "ollama": "byok_ollama_key",
}

DEFAULT_ENDPOINTS: dict[str, str] = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "openai_compatible": "http://127.0.0.1:8000/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "claude": "https://api.anthropic.com/v1/messages",
    "ollama": "http://127.0.0.1:11434/api/chat",
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openai_compatible": "local-model",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-3-5-sonnet-latest",
    "claude": "claude-3-5-sonnet-latest",
    "ollama": "llama3.2",
}


@dataclass(frozen=True)
class DocumentChunk:
    """Indexed text chunk from a PDF page or Markdown heading section."""

    chunk_id: str
    source_path: str
    doc_type: str
    text: str
    page_number: int | None = None
    section: str | None = None
    score: float = 0.0


@dataclass(frozen=True)
class RagAnswer:
    """Response from Ask Document including grounded citations and consent metadata."""

    answer_text: str
    citations: list[DocumentChunk] = field(default_factory=list)
    provider: str = "local_bm25"
    consent_blocked: bool = False
    error: str | None = None
    used_local_only: bool = True


def _fold_accents(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in norm if not unicodedata.combining(ch))


def tokenize_bm25(text: str) -> list[str]:
    """Tokenize text into accent-folded lowercase alphanumeric terms."""
    if not text:
        return []
    return _TOKEN_RE.findall(_fold_accents(text))


def _split_chunks(text: str, *, max_chars: int = 1200, overlap: int = 160) -> list[str]:
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    parts: list[str] = []
    start = 0
    step = max(200, max_chars - overlap)
    while start < len(clean):
        segment = clean[start : start + max_chars].strip()
        if segment:
            parts.append(segment)
        if start + max_chars >= len(clean):
            break
        start += step
    return parts


class DocumentRagIndex:
    """In-memory Okapi BM25 retriever indexing local PDF pages and Markdown sections."""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = float(k1)
        self.b = float(b)
        self._chunks: list[DocumentChunk] = []
        self._tf: list[Counter[str]] = []
        self._lengths: list[int] = []
        self._df: Counter[str] = Counter()

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    def clear(self) -> None:
        self._chunks.clear()
        self._tf.clear()
        self._lengths.clear()
        self._df.clear()

    def _add_chunk(self, chunk: DocumentChunk) -> None:
        tokens = tokenize_bm25(f"{chunk.section or ''} {chunk.text}")
        if not tokens:
            return
        counts = Counter(tokens)
        self._chunks.append(chunk)
        self._tf.append(counts)
        self._lengths.append(len(tokens))
        for term in counts:
            self._df[term] += 1

    def index_text(
        self,
        text: str,
        *,
        source_path: str = "<memory>",
        doc_type: str = "text",
        page_number: int | None = None,
        section: str | None = None,
    ) -> int:
        """Index plain text chunks and return count added."""
        added = 0
        for idx, segment in enumerate(_split_chunks(text)):
            cid = f"{source_path}:{page_number or 0}:{section or 'root'}:{idx}"
            self._add_chunk(
                DocumentChunk(
                    chunk_id=cid,
                    source_path=source_path,
                    doc_type=doc_type,
                    text=segment,
                    page_number=page_number,
                    section=section,
                )
            )
            added += 1
        return added

    def index_pdf(self, pdf_path: Path | str) -> int:
        """Validate and index a local PDF file with 1-based page number citations."""
        ok, reason = validate_local_pdf_path(pdf_path, check_exists=True)
        if not ok:
            raise ValueError(f"Invalid PDF path: {reason}")
        resolved = Path(pdf_path).resolve()

        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(resolved))
        added = 0
        try:
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                tp = None
                try:
                    tp = page.get_textpage()
                    page_text = tp.get_text_range() or ""
                finally:
                    if tp is not None:
                        tp.close()
                    page.close()
                if page_text.strip():
                    added += self.index_text(
                        page_text,
                        source_path=str(resolved),
                        doc_type="pdf",
                        page_number=page_idx + 1,
                    )
        finally:
            doc.close()
        return added

    def index_markdown(self, md_path: Path | str) -> int:
        """Validate and index a local Markdown file grouped by # heading sections."""
        ok, reason = validate_local_markdown_path(md_path, check_exists=True)
        if not ok:
            raise ValueError(f"Invalid Markdown path: {reason}")
        resolved = Path(md_path).resolve()
        raw = resolved.read_text(encoding="utf-8", errors="replace")

        current_section = resolved.name
        buf: list[str] = []
        in_fence = False
        added = 0

        def _flush() -> None:
            nonlocal added
            body = "\n".join(buf).strip()
            buf.clear()
            if body:
                added += self.index_text(
                    body,
                    source_path=str(resolved),
                    doc_type="markdown",
                    section=current_section,
                )

        for line in raw.splitlines():
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                buf.append(line)
                continue
            if not in_fence:
                m = _HEADING_RE.match(line)
                if m:
                    _flush()
                    hashes, title = m.group(1), m.group(2).strip()
                    current_section = f"{hashes} {title}"
                    buf.append(title)
                    continue
            buf.append(line)
        _flush()
        return added

    def search(self, query: str, *, top_k: int = 5) -> list[DocumentChunk]:
        """Rank indexed chunks using Okapi BM25 with zero-division safety."""
        n_docs = len(self._chunks)
        if n_docs == 0 or top_k <= 0:
            return []
        q_tokens = tokenize_bm25(query)
        if not q_tokens:
            return []
        total_len = sum(self._lengths)
        if total_len <= 0:
            return []
        avgdl = total_len / float(n_docs)

        scored: list[DocumentChunk] = []
        unique_q = list(dict.fromkeys(q_tokens))
        for idx, chunk in enumerate(self._chunks):
            tf_map = self._tf[idx]
            dl = self._lengths[idx]
            score = 0.0
            for term in unique_q:
                tf = tf_map.get(term, 0)
                if tf <= 0:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(((n_docs - df + 0.5) / (df + 0.5)) + 1.0)
                denom = tf + self.k1 * (1.0 - self.b + self.b * (dl / avgdl))
                if denom > 0.0:
                    score += idf * ((tf * (self.k1 + 1.0)) / denom)
            if score > 0.0:
                scored.append(
                    DocumentChunk(
                        chunk_id=chunk.chunk_id,
                        source_path=chunk.source_path,
                        doc_type=chunk.doc_type,
                        text=chunk.text,
                        page_number=chunk.page_number,
                        section=chunk.section,
                        score=score,
                    )
                )

        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[:top_k]


def save_byok_key(provider: str, api_key: str, *, base_dir: Path | None = None) -> None:
    """Encrypt and save a BYOK provider API key using Windows DPAPI."""
    key_name = PROVIDER_SECRET_KEYS.get(provider.lower().strip())
    if not key_name:
        raise ValueError(f"Unsupported provider: {provider}")
    set_secret(key_name, api_key, base_dir=base_dir)


def load_byok_key(provider: str, *, base_dir: Path | None = None) -> str | None:
    """Retrieve a DPAPI-protected BYOK provider API key."""
    key_name = PROVIDER_SECRET_KEYS.get(provider.lower().strip())
    if not key_name:
        return None
    return get_secret(key_name, default=None, base_dir=base_dir)


def delete_byok_key(provider: str, *, base_dir: Path | None = None) -> None:
    """Delete a stored BYOK provider API key."""
    key_name = PROVIDER_SECRET_KEYS.get(provider.lower().strip())
    if key_name:
        delete_secret(key_name, base_dir=base_dir)


def _format_citation_tag(chunk: DocumentChunk) -> str:
    if chunk.page_number is not None:
        return f"[p. {chunk.page_number}]"
    if chunk.section:
        return f"[{chunk.section}]"
    return f"[{Path(chunk.source_path).name}]"


def _scrub_secret(text: str, secret: str | None) -> str:
    cleaned = text
    if secret:
        cleaned = cleaned.replace(secret, "[REDACTED_KEY]")
    cleaned = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "[REDACTED_KEY]", cleaned)
    cleaned = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}", "Bearer [REDACTED_KEY]", cleaned)
    return cleaned


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.hostname in ("127.0.0.1", "localhost", "::1")


def _default_transport(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, str]:
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc.reason)
        return int(exc.code), detail
    except URLError as exc:
        raise OSError(str(exc.reason)) from exc


def _build_local_answer(question: str, hits: list[DocumentChunk]) -> str:
    if not hits:
        return f"No se encontraron fragmentos relevantes para: {question}"
    lines = ["Resumen local (BM25) con citas exactas:"]
    for hit in hits:
        tag = _format_citation_tag(hit)
        excerpt = " ".join(hit.text.split())[:280]
        lines.append(f"- {tag} {excerpt}")
    return "\n".join(lines)


def _build_provider_request(
    provider_norm: str,
    endpoint: str,
    model_name: str,
    question: str,
    hits: list[DocumentChunk],
    resolved_key: str | None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    context_lines = [f"{_format_citation_tag(c)} {c.text}" for c in hits]
    context_block = "\n\n".join(context_lines) if context_lines else "(Sin fragmentos relevantes)"
    system_prompt = (
        "Responde de forma precisa citando unicamente el contexto proporcionado. "
        "Incluye las referencias exactas [p. N] o [Seccion] en tu respuesta."
    )
    user_prompt = f"Contexto:\n{context_block}\n\nPregunta: {question}"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    url = endpoint

    if provider_norm in ("openai", "openai_compatible"):
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
    elif provider_norm in ("anthropic", "claude"):
        if resolved_key:
            headers["x-api-key"] = resolved_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": model_name,
            "max_tokens": 1024,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
    elif provider_norm == "gemini":
        if resolved_key:
            headers["x-goog-api-key"] = resolved_key
        payload = {
            "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
        }
    elif provider_norm == "ollama":
        if resolved_key:
            headers["Authorization"] = f"Bearer {resolved_key}"
        payload = {
            "model": model_name,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    else:
        raise ValueError(f"Unsupported provider: {provider_norm}")

    return url, payload, headers


def _extract_provider_text(provider_norm: str, data: dict[str, Any]) -> str:
    if provider_norm in ("openai", "openai_compatible"):
        choices = data.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            return str(msg.get("content") or "")
    elif provider_norm in ("anthropic", "claude"):
        content = data.get("content") or []
        if content and isinstance(content[0], dict):
            return str(content[0].get("text") or "")
    elif provider_norm == "gemini":
        cands = data.get("candidates") or []
        if cands and isinstance(cands[0], dict):
            parts = (cands[0].get("content") or {}).get("parts") or []
            if parts and isinstance(parts[0], dict):
                return str(parts[0].get("text") or "")
    elif provider_norm == "ollama":
        msg = data.get("message") or {}
        if isinstance(msg, dict) and msg.get("content"):
            return str(msg.get("content"))
        if data.get("response"):
            return str(data.get("response"))
    return ""


def ask_document(
    index: DocumentRagIndex,
    question: str,
    *,
    provider: str = "local_bm25",
    model: str = "",
    endpoint_url: str | None = None,
    api_key: str | None = None,
    consent_granted: bool = False,
    top_k: int = 4,
    base_dir: Path | None = None,
    timeout_s: float = 15.0,
    transport: Callable[[str, bytes, dict[str, str], float], tuple[int, str]] | None = None,
) -> RagAnswer:
    """Answer a question over indexed documents using local BM25 or consent-gated BYOK LLM."""
    provider_norm = (provider or "local_bm25").lower().strip()
    hits = index.search(question, top_k=top_k)

    if provider_norm in ("local", "local_bm25", "bm25"):
        return RagAnswer(
            answer_text=_build_local_answer(question, hits),
            citations=hits,
            provider="local_bm25",
            consent_blocked=False,
            used_local_only=True,
        )

    if provider_norm not in PROVIDER_SECRET_KEYS:
        return RagAnswer(
            answer_text=f"Proveedor no soportado: {provider_norm}",
            citations=hits,
            provider=provider_norm,
            error="unsupported_provider",
            used_local_only=True,
        )

    endpoint = (endpoint_url or DEFAULT_ENDPOINTS[provider_norm]).strip()
    parsed = urlparse(endpoint)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return RagAnswer(
            answer_text="Endpoint invalido.",
            citations=hits,
            provider=provider_norm,
            error="invalid_endpoint",
            used_local_only=True,
        )

    requires_consent = provider_norm != "ollama" or not _is_loopback_url(endpoint)
    if requires_consent and not consent_granted:
        return RagAnswer(
            answer_text="Llamada externa bloqueada: se requiere consentimiento explicito del usuario.",
            citations=hits,
            provider=provider_norm,
            consent_blocked=True,
            error="consent_required",
            used_local_only=True,
        )

    resolved_key = api_key if api_key is not None else load_byok_key(provider_norm, base_dir=base_dir)
    model_name = (model or DEFAULT_MODELS.get(provider_norm, "local-model")).strip()

    try:
        url, payload, headers = _build_provider_request(
            provider_norm, endpoint, model_name, question, hits, resolved_key
        )
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        send_fn = transport or _default_transport
        status, raw_resp = send_fn(url, body_bytes, headers, timeout_s)
    except Exception as exc:
        safe_err = _scrub_secret(str(exc), resolved_key)
        return RagAnswer(
            answer_text=f"Error al consultar proveedor ({provider_norm}).",
            citations=hits,
            provider=provider_norm,
            error=safe_err,
            used_local_only=False,
        )

    if status < 200 or status >= 300:
        safe_err = _scrub_secret(f"HTTP {status}: {raw_resp[:240]}", resolved_key)
        return RagAnswer(
            answer_text=f"Error HTTP {status} del proveedor {provider_norm}.",
            citations=hits,
            provider=provider_norm,
            error=safe_err,
            used_local_only=False,
        )

    try:
        data = json.loads(raw_resp)
    except json.JSONDecodeError as exc:
        return RagAnswer(
            answer_text="Respuesta invalida del proveedor.",
            citations=hits,
            provider=provider_norm,
            error=_scrub_secret(str(exc), resolved_key),
            used_local_only=False,
        )

    answer = _scrub_secret(_extract_provider_text(provider_norm, data), resolved_key)
    if not answer.strip():
        answer = _build_local_answer(question, hits)
    return RagAnswer(
        answer_text=answer,
        citations=hits,
        provider=provider_norm,
        consent_blocked=False,
        used_local_only=False,
    )
