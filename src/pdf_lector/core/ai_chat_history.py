"""Per-document local persistent AI chat history store and exporter (.md / .txt / .json)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path

from .win32_bridge import atomic_write_text, default_app_data_dir


def local_chat_timestamp() -> str:
    """Return local ISO-8601 timestamp with seconds precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_chat_timestamp(raw_ts: str) -> str:
    """Format stored timestamp cleanly as dd/mm/yyyy HH:mm."""
    clean = (raw_ts or "").strip()
    if not clean:
        return datetime.now().strftime("%d/%m/%Y %H:%M")
    try:
        dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return clean[:16]


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    text: str
    timestamp: str = field(default_factory=local_chat_timestamp)
    provider: str = ""
    citations: list[dict[str, object]] = field(default_factory=list)


class AiChatHistoryStore:
    """Persistent per-document local chat history stored under %LOCALAPPDATA%\\TRODAI\\PDFLector\\ai_chats."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._dir = Path(base_dir) if base_dir is not None else (default_app_data_dir() / "ai_chats")
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._dir

    def _doc_key(self, doc_path: str | Path) -> str:
        try:
            normalized = str(Path(doc_path).resolve()).lower()
        except OSError:
            normalized = str(Path(doc_path)).lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    def history_file(self, doc_path: str | Path) -> Path:
        return self._dir / f"{self._doc_key(doc_path)}.json"

    def load_history(self, doc_path: str | Path | None) -> list[ChatMessage]:
        if doc_path is None:
            return []
        fpath = self.history_file(doc_path)
        if not fpath.exists():
            return []
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            rows = raw.get("messages", []) if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                return []
            out: list[ChatMessage] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or "user")
                if role not in ("user", "assistant"):
                    role = "assistant"
                out.append(
                    ChatMessage(
                        role=role,
                        text=str(item.get("text") or ""),
                        timestamp=str(item.get("timestamp") or ""),
                        provider=str(item.get("provider") or ""),
                        citations=list(item.get("citations") or []),
                    )
                )
            return out
        except Exception:
            return []

    def save_history(self, doc_path: str | Path, messages: list[ChatMessage]) -> Path:
        fpath = self.history_file(doc_path)
        payload = {
            "document_path": str(doc_path),
            "document_name": Path(doc_path).name,
            "updated_at": local_chat_timestamp(),
            "messages": [asdict(m) for m in messages],
        }
        atomic_write_text(fpath, json.dumps(payload, indent=2, ensure_ascii=False))
        return fpath

    def append_message(self, doc_path: str | Path, msg: ChatMessage) -> list[ChatMessage]:
        items = self.load_history(doc_path)
        items.append(msg)
        self.save_history(doc_path, items)
        return items

    def clear_history(self, doc_path: str | Path | None) -> bool:
        if doc_path is None:
            return False
        fpath = self.history_file(doc_path)
        if fpath.exists():
            fpath.unlink()
            return True
        return False

    def export_chat(self, doc_path: str | Path, output_path: str | Path, *, fmt: str | None = None) -> Path:
        out = Path(output_path)
        chosen_fmt = (fmt or out.suffix.lstrip(".") or "md").lower()
        messages = self.load_history(doc_path)
        doc_name = Path(doc_path).name

        if chosen_fmt == "json":
            payload = {
                "document": doc_name,
                "document_path": str(doc_path),
                "exported_at": local_chat_timestamp(),
                "messages": [asdict(m) for m in messages],
            }
            atomic_write_text(out, json.dumps(payload, indent=2, ensure_ascii=False))
            return out

        if chosen_fmt == "txt":
            lines = [f"Chat sobre documento: {doc_name}", "=" * 52, ""]
            for m in messages:
                who = "Usuario" if m.role == "user" else f"IA ({m.provider or 'Local'})"
                ts = format_chat_timestamp(m.timestamp)
                lines.append(f"[{ts}] {who}:")
                lines.append(m.text)
                for cit in m.citations:
                    label = cit.get("label") or ""
                    snip = cit.get("snippet") or ""
                    lines.append(f"  - [{label}] {snip}")
                lines.append("")
            atomic_write_text(out, "\n".join(lines))
            return out

        # Default Markdown (.md)
        lines = [f"# Conversacion IA · {doc_name}", ""]
        for m in messages:
            who = "Usuario" if m.role == "user" else f"Asistente IA ({m.provider or 'Local'})"
            ts = format_chat_timestamp(m.timestamp)
            lines.append(f"### {who} · `{ts}`")
            lines.append("")
            lines.append(m.text)
            if m.citations:
                lines.append("")
                lines.append("**Citas:**")
                for cit in m.citations:
                    label = cit.get("label") or ""
                    snip = cit.get("snippet") or ""
                    lines.append(f"- **[{label}]** {snip}")
            lines.append("")
            lines.append("---")
            lines.append("")
        atomic_write_text(out, "\n".join(lines))
        return out
