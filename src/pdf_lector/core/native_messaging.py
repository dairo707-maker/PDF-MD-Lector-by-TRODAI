"""Chrome/Edge Native Messaging host using strict 4-byte LE JSON framing on stdin/stdout."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
from typing import Any, BinaryIO
from urllib.parse import urlparse

from .. import APP_NAME, read_version
from .path_guard import (
    _has_control_chars,
    _has_ntfs_ads,
    is_unc_or_network_path,
    validate_local_any_doc,
)
from .temp_manager import TempManager

MAX_MESSAGE_BYTES = 1024 * 1024  # 1 MiB Chrome/Edge Native Messaging limit
_ALLOWED_WEB_SCHEMES = {"http", "https"}


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Encode a JSON-serializable response with a 4-byte little-endian length prefix."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def write_frame(stdout_buf: BinaryIO, payload: dict[str, Any]) -> None:
    """Write one Native Messaging frame and flush immediately (never emit stray stdout text)."""
    frame = encode_frame(payload)
    stdout_buf.write(frame)
    stdout_buf.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_output_pdf(raw_output: Any) -> tuple[bool, Path | None, str]:
    if not raw_output or not isinstance(raw_output, str):
        return False, None, "missing_output"
    text = raw_output.strip()
    if not text or _has_control_chars(text) or _has_ntfs_ads(text) or is_unc_or_network_path(text):
        return False, None, "unsafe_output_path"
    out_path = Path(text)
    if out_path.suffix.lower() != ".pdf":
        return False, None, "unsupported_extension"
    if out_path.exists() and out_path.is_dir():
        return False, None, "output_is_directory"
    return True, out_path, "ok"


def _is_safe_webpage_url(url: str) -> bool:
    cleaned = url.strip()
    if not cleaned:
        return True
    if _has_control_chars(cleaned) or is_unc_or_network_path(cleaned):
        # Allow http:// and https:// for web capture context metadata, block everything else.
        parsed = urlparse(cleaned)
        return parsed.scheme.lower() in _ALLOWED_WEB_SCHEMES and bool(parsed.netloc)
    parsed = urlparse(cleaned)
    if parsed.scheme:
        return parsed.scheme.lower() in _ALLOWED_WEB_SCHEMES and bool(parsed.netloc)
    return False


def _handle_open_pdf(msg: dict[str, Any]) -> dict[str, Any]:
    raw_path = msg.get("path", "")
    if not isinstance(raw_path, str):
        return {"ok": False, "action": "open_pdf", "error": "invalid_path_type"}
    ok, reason = validate_local_any_doc(raw_path, check_exists=True)
    if not ok:
        return {"ok": False, "action": "open_pdf", "error": reason}
    resolved = str(Path(raw_path).resolve())

    forwarded = False
    if bool(msg.get("forward_to_instance", False)):
        try:
            from .single_instance import SingleInstanceLock

            lock = SingleInstanceLock()
            forwarded = lock.send_to_running_instance([resolved])
        except Exception:
            forwarded = False

    return {
        "ok": True,
        "action": "open_pdf",
        "path": resolved,
        "forwarded": forwarded,
    }


def _handle_webpage_to_pdf(msg: dict[str, Any]) -> dict[str, Any]:
    raw_url = msg.get("url")
    if raw_url is not None:
        if not isinstance(raw_url, str) or not _is_safe_webpage_url(raw_url):
            return {"ok": False, "action": "webpage_to_pdf", "error": "unsafe_url_scheme"}

    ok_out, out_path, out_err = _validate_output_pdf(msg.get("output"))
    if not ok_out or out_path is None:
        return {"ok": False, "action": "webpage_to_pdf", "error": out_err}

    html_body = msg.get("html")
    if html_body is None:
        text_body = str(msg.get("text") or "Captured Web Page")
        html_body = f"<html><body><p>{text_body}</p></body></html>"
    elif not isinstance(html_body, str):
        return {"ok": False, "action": "webpage_to_pdf", "error": "invalid_html"}

    try:
        dpi = int(msg.get("dpi", 150))
    except (TypeError, ValueError):
        return {"ok": False, "action": "webpage_to_pdf", "error": "invalid_dpi"}
    if dpi < 36 or dpi > 1200:
        return {"ok": False, "action": "webpage_to_pdf", "error": "dpi_out_of_range"}

    tm = TempManager(register_atexit=False)
    try:
        task_dir = tm.create_temp_dir(task_id="nm_web", secure=True)
        tmp_html = task_dir / "capture.html"
        tmp_html.write_text(html_body, encoding="utf-8")

        from .tools import ToolRegistry, validate_local_ext_path

        res = ToolRegistry.get("convert_to_pdf").run(
            [tmp_html],
            out_path,
            {"dpi": dpi, "use_edge_headless": False},
        )
        if not res.success:
            return {"ok": False, "action": "webpage_to_pdf", "error": res.summary}
        return {
            "ok": True,
            "action": "webpage_to_pdf",
            "output": str(out_path),
            "summary": res.summary,
        }
    finally:
        tm.cleanup_all()


def _handle_quick_action(msg: dict[str, Any]) -> dict[str, Any]:
    tool_id = str(msg.get("tool_id") or "").strip()
    if not tool_id:
        return {"ok": False, "action": "quick_action", "error": "missing_tool_id"}

    from .tools import ToolRegistry, validate_local_ext_path

    if not ToolRegistry.has_tool(tool_id):
        return {"ok": False, "action": "quick_action", "error": f"unknown_tool:{tool_id}"}

    raw_inputs = msg.get("inputs", [])
    if not isinstance(raw_inputs, list):
        return {"ok": False, "action": "quick_action", "error": "invalid_inputs"}

    validated_inputs: list[Path] = []
    for item in raw_inputs:
        ok, resolved, reason = validate_local_ext_path(str(item), check_exists=True)
        if not ok or resolved is None:
            return {"ok": False, "action": "quick_action", "error": reason}
        validated_inputs.append(resolved)

    raw_output = msg.get("output")
    if not isinstance(raw_output, str) or not raw_output.strip():
        return {"ok": False, "action": "quick_action", "error": "missing_output"}
    if _has_control_chars(raw_output) or _has_ntfs_ads(raw_output) or is_unc_or_network_path(raw_output):
        return {"ok": False, "action": "quick_action", "error": "unsafe_output_path"}
    out_path = Path(raw_output.strip())

    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    res = ToolRegistry.get(tool_id).run(validated_inputs, out_path, params)
    return {
        "ok": res.success,
        "action": "quick_action",
        "tool_id": tool_id,
        "output_paths": [str(p) for p in res.output_paths],
        "summary": res.summary,
        **({} if res.success else {"error": res.summary}),
    }


def handle_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a single validated Native Messaging JSON object."""
    action = str(msg.get("action") or "").strip()
    if action == "ping":
        return {
            "ok": True,
            "action": "ping",
            "app": APP_NAME,
            "version": read_version(),
        }
    if action in ("open_pdf", "open_doc"):
        return _handle_open_pdf(msg)
    if action in ("webpage_to_pdf", "convert_page_to_pdf"):
        return _handle_webpage_to_pdf(msg)
    if action == "quick_action":
        return _handle_quick_action(msg)
    return {"ok": False, "error": f"unknown_action:{action}"}


def run_stdio_loop(
    stdin_buf: BinaryIO | None = None,
    stdout_buf: BinaryIO | None = None,
) -> int:
    """Process Native Messaging frames from stdin and emit framed JSON responses to stdout."""
    in_stream = stdin_buf if stdin_buf is not None else sys.stdin.buffer
    out_stream = stdout_buf if stdout_buf is not None else sys.stdout.buffer

    while True:
        header = _read_exact(in_stream, 4)
        if len(header) < 4:
            # Clean EOF or truncated length header: exit without stray stdout bytes.
            break

        (length,) = struct.unpack("<I", header)
        if length == 0 or length > MAX_MESSAGE_BYTES:
            write_frame(
                out_stream,
                {
                    "ok": False,
                    "error": "payload_too_large" if length > MAX_MESSAGE_BYTES else "empty_payload",
                    "max_bytes": MAX_MESSAGE_BYTES,
                },
            )
            break

        body = _read_exact(in_stream, length)
        if len(body) < length:
            write_frame(out_stream, {"ok": False, "error": "truncated_payload"})
            break

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            write_frame(out_stream, {"ok": False, "error": "invalid_json"})
            continue

        if not isinstance(payload, dict):
            write_frame(out_stream, {"ok": False, "error": "payload_must_be_object"})
            continue

        try:
            response = handle_message(payload)
        except Exception as exc:
            response = {"ok": False, "error": f"internal_error:{exc.__class__.__name__}"}
        write_frame(out_stream, response)

    return 0


def main() -> None:
    raise SystemExit(run_stdio_loop())


if __name__ == "__main__":
    main()
