"""100% local privacy-preserving usage insights & tool recommendations (aggregate counters only)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
import time

from .win32_bridge import atomic_write_text, canonical_lock_key

_BUCKETS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _default_insights_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "TRODAI" / "PDFLector" / "usage_insights.json"


def _atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


_DOC_TYPE_TOOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "invoice": (
        ("validate_einvoice", "rec.reason.invoice"),
        ("create_einvoice", "rec.reason.invoice"),
        ("pdf_to_einvoice", "rec.reason.invoice"),
    ),
    "contract": (
        ("sign_pdf", "rec.reason.contract"),
        ("protect_pdf", "rec.reason.contract"),
        ("compare_pdf", "rec.reason.contract"),
    ),
    "scan": (
        ("pdf_ocr", "rec.reason.scan"),
        ("compress_pdf", "rec.reason.scan"),
        ("repair_pdf", "rec.reason.scan"),
    ),
    "report": (
        ("edit_bookmarks", "rec.reason.report"),
        ("add_page_numbers", "rec.reason.report"),
        ("compress_pdf", "rec.reason.report"),
    ),
}


def classify_document_text(sample_text: str, *, page_count: int = 1, toc_count: int = 0) -> str:
    """Classify document profile locally without persisting any text content."""
    lower = (sample_text or "").lower()
    if any(kw in lower for kw in ("factura", "invoice", "iva", "cufe", "nit", "subtotal", "factur-x", "ubl")):
        return "invoice"
    if any(kw in lower for kw in ("contrato", "contract", "clausula", "clause", "acuerdo", "firma", "signature")):
        return "contract"
    if len(lower.strip()) < 45 and page_count >= 1:
        return "scan"
    if toc_count >= 2 or page_count >= 5:
        return "report"
    return "general"


@dataclass
class _InsightsState:
    enabled: bool = True
    tool_counts: dict[str, int] = field(default_factory=dict)
    doc_type_counts: dict[str, int] = field(default_factory=dict)
    search_topic_counts: dict[str, int] = field(default_factory=dict)
    dict_lang_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class _InsightsBucket:
    lock: threading.RLock
    state: _InsightsState
    loaded: bool = False
    last_stat: tuple[int, int, int, int] | None = None


_BUCKETS: dict[str, _InsightsBucket] = {}


def _bucket_for_path(path: Path) -> _InsightsBucket:
    key = canonical_lock_key(path)
    with _BUCKETS_GUARD:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            lock = _PATH_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _PATH_LOCKS[key] = lock
            bucket = _InsightsBucket(lock=lock, state=_InsightsState())
            _BUCKETS[key] = bucket
        else:
            _PATH_LOCKS[key] = bucket.lock
        return bucket


def _lock_for_path(path: Path) -> threading.RLock:
    return _bucket_for_path(path).lock


def _apply_state_in_place(target: _InsightsState, source: _InsightsState) -> None:
    target.enabled = source.enabled
    target.tool_counts.clear()
    target.tool_counts.update(source.tool_counts)
    target.doc_type_counts.clear()
    target.doc_type_counts.update(source.doc_type_counts)
    target.search_topic_counts.clear()
    target.search_topic_counts.update(source.search_topic_counts)
    target.dict_lang_counts.clear()
    target.dict_lang_counts.update(source.dict_lang_counts)


class LocalUsageInsights:
    """Tracks only aggregate counters locally to suggest relevant tools and languages."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._path = Path(store_path) if store_path else _default_insights_path()
        self._bucket = _bucket_for_path(self._path)
        self._lock = self._bucket.lock
        self._state = self._bucket.state
        with self._lock:
            self._sync_from_disk_locked()

    def _load(self) -> _InsightsState:
        if not self._path.exists():
            return _InsightsState()
        raw_text: str | None = None
        for attempt in range(5):
            try:
                raw_text = self._path.read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                return _InsightsState()
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.03 * (attempt + 1))
            except OSError:
                return _InsightsState()
        if raw_text is None:
            return _InsightsState()
        try:
            raw = json.loads(raw_text)
            if not isinstance(raw, dict):
                return _InsightsState()
            return _InsightsState(
                enabled=bool(raw.get("enabled", True)),
                tool_counts={str(k): int(v) for k, v in dict(raw.get("tool_counts", {})).items()},
                doc_type_counts={str(k): int(v) for k, v in dict(raw.get("doc_type_counts", {})).items()},
                search_topic_counts={str(k): int(v) for k, v in dict(raw.get("search_topic_counts", {})).items()},
                dict_lang_counts={str(k): int(v) for k, v in dict(raw.get("dict_lang_counts", {})).items()},
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return _InsightsState()

    def _sync_from_disk_locked(self) -> None:
        bucket = self._bucket
        try:
            st = self._path.stat()
        except FileNotFoundError:
            bucket.state.enabled = True
            bucket.state.tool_counts.clear()
            bucket.state.doc_type_counts.clear()
            bucket.state.search_topic_counts.clear()
            bucket.state.dict_lang_counts.clear()
            bucket.last_stat = None
            bucket.loaded = True
            return
        except PermissionError:
            raise
        except OSError:
            return

        sig = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
        if not bucket.loaded or bucket.last_stat != sig:
            loaded = self._load()
            _apply_state_in_place(bucket.state, loaded)
            bucket.last_stat = sig
            bucket.loaded = True

    def _save(self) -> None:
        with self._lock:
            payload = {
                "enabled": self._state.enabled,
                "tool_counts": dict(self._state.tool_counts),
                "doc_type_counts": dict(self._state.doc_type_counts),
                "search_topic_counts": dict(self._state.search_topic_counts),
                "dict_lang_counts": dict(self._state.dict_lang_counts),
            }
            try:
                _atomic_write_json(self._path, payload)
            except Exception:
                self._bucket.loaded = False
                self._bucket.last_stat = None
                raise
            try:
                st = self._path.stat()
                self._bucket.last_stat = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
            except OSError:
                self._bucket.last_stat = None
            self._bucket.loaded = True

    @property
    def enabled(self) -> bool:
        with self._lock:
            self._sync_from_disk_locked()
            return self._state.enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._sync_from_disk_locked()
            self._state.enabled = bool(enabled)
            self._save()

    def clear_history(self) -> None:
        with self._lock:
            self._sync_from_disk_locked()
            self._state.tool_counts.clear()
            self._state.doc_type_counts.clear()
            self._state.search_topic_counts.clear()
            self._state.dict_lang_counts.clear()
            self._save()

    def record_tool_use(self, tool_id: str) -> None:
        if not tool_id:
            return
        with self._lock:
            self._sync_from_disk_locked()
            if not self._state.enabled:
                return
            self._state.tool_counts[tool_id] = self._state.tool_counts.get(tool_id, 0) + 1
            self._save()

    def record_search(self, query: str) -> None:
        q = (query or "").strip().lower()
        if len(q) < 3:
            return
        # Map keyword search into coarse privacy-safe topic buckets (no raw query stored)
        topic_map = {
            "invoice": ("factur", "dian", "ubl", "invoice"),
            "ocr": ("ocr", "escan", "scan"),
            "sign": ("firm", "sign", "pki", "cert"),
            "compress": ("compr", "optim", "web", "peso"),
            "security": ("tach", "redact", "cifr", "metadat", "desbloq", "proteg"),
        }
        for topic, prefixes in topic_map.items():
            if any(p in q for p in prefixes):
                with self._lock:
                    self._sync_from_disk_locked()
                    if not self._state.enabled:
                        return
                    self._state.search_topic_counts[topic] = self._state.search_topic_counts.get(topic, 0) + 1
                    self._save()
                return

    def record_document_opened(self, sample_text: str, *, page_count: int = 1, toc_count: int = 0) -> str:
        doc_type = classify_document_text(sample_text, page_count=page_count, toc_count=toc_count)
        if doc_type != "general":
            with self._lock:
                self._sync_from_disk_locked()
                if self._state.enabled:
                    self._state.doc_type_counts[doc_type] = self._state.doc_type_counts.get(doc_type, 0) + 1
                    self._save()
        return doc_type

    def record_dictionary_lookup(self, lang: str) -> None:
        if not lang:
            return
        code = lang.strip().lower()[:8]
        with self._lock:
            self._sync_from_disk_locked()
            if not self._state.enabled:
                return
            self._state.dict_lang_counts[code] = self._state.dict_lang_counts.get(code, 0) + 1
            self._save()

    def get_recommended_tools(self, *, limit: int = 4) -> list[tuple[str, str]]:
        """Return [(tool_id, reason_key)] ranked from local tool use, doc types, and search topics."""
        with self._lock:
            self._sync_from_disk_locked()
            if not self._state.enabled:
                return []
            tool_counts = dict(self._state.tool_counts)
            doc_type_counts = dict(self._state.doc_type_counts)
            search_topic_counts = dict(self._state.search_topic_counts)

        scores: dict[str, tuple[int, str]] = {}

        def _boost(tool_id: str, pts: int, reason_key: str) -> None:
            prev_pts, prev_reason = scores.get(tool_id, (0, reason_key))
            new_pts = prev_pts + pts
            chosen_reason = reason_key if pts >= prev_pts else prev_reason
            scores[tool_id] = (new_pts, chosen_reason)

        for tool_id, cnt in tool_counts.items():
            _boost(tool_id, cnt * 3, "rec.reason.frequent")

        for doc_type, cnt in doc_type_counts.items():
            for tool_id, reason_key in _DOC_TYPE_TOOLS.get(doc_type, ()):
                _boost(tool_id, cnt * 2, reason_key)

        topic_tools = {
            "invoice": ("validate_einvoice", "rec.reason.search"),
            "ocr": ("pdf_ocr", "rec.reason.search"),
            "sign": ("sign_pdf", "rec.reason.search"),
            "compress": ("compress_pdf", "rec.reason.search"),
            "security": ("redact_pdf", "rec.reason.search"),
        }
        for topic, cnt in search_topic_counts.items():
            if topic in topic_tools:
                tid, rkey = topic_tools[topic]
                _boost(tid, cnt * 2, rkey)

        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        out = [(tid, data[1]) for tid, data in ranked[:limit]]
        if not out:
            # Cold-start sensible defaults when history is still empty
            out = [
                ("sign_pdf", "rec.reason.starter"),
                ("merge_pdf", "rec.reason.starter"),
                ("compress_pdf", "rec.reason.starter"),
                ("pdf_ocr", "rec.reason.starter"),
            ][:limit]
        return out

    def get_suggested_dictionary_lang(self) -> str | None:
        with self._lock:
            self._sync_from_disk_locked()
            if not self._state.enabled or not self._state.dict_lang_counts:
                return None
            return max(self._state.dict_lang_counts.items(), key=lambda kv: kv[1])[0]

    def get_dynamic_quick_actions(
        self,
        default_ids: list[str],
        limit: int = 6,
        *,
        valid_ids: Collection[str] | None = None,
    ) -> list[str]:
        """Return top `limit` quick-action tool IDs adapted from aggregate tool_counts."""
        if limit <= 0:
            return []
        allowed = set(valid_ids) if valid_ids is not None else None
        clean_defaults = list(
            dict.fromkeys(
                str(tid)
                for tid in default_ids
                if tid and (allowed is None or str(tid) in allowed)
            )
        )
        with self._lock:
            self._sync_from_disk_locked()
            if not self._state.enabled or not self._state.tool_counts:
                return clean_defaults[:limit]
            tool_counts = dict(self._state.tool_counts)

        default_pos = {tid: idx for idx, tid in enumerate(clean_defaults)}
        ranked = [
            tid
            for tid, _cnt in sorted(
                (
                    (tid, cnt)
                    for tid, cnt in tool_counts.items()
                    if cnt > 0 and (allowed is None or tid in allowed)
                ),
                key=lambda kv: (-kv[1], default_pos.get(kv[0], len(clean_defaults)), kv[0]),
            )
        ]

        selected: list[str] = []
        seen: set[str] = set()
        for tid in (*ranked, *clean_defaults):
            if tid not in seen:
                seen.add(tid)
                selected.append(tid)
                if len(selected) >= limit:
                    break
        return selected
