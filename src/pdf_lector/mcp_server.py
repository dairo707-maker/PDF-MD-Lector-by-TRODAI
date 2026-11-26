"""Stdio JSON-RPC 2.0 MCP server (`pdf-md-lector-mcp`) exposing local PDF & Markdown tools."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, TextIO

from . import read_version
from .core.ai_rag import DocumentRagIndex
from .core.markdown_engine import parse_markdown_toc
from .core.path_guard import (
    _has_control_chars,
    _has_ntfs_ads,
    is_unc_or_network_path,
    validate_local_any_doc,
    validate_local_markdown_path,
    validate_local_pdf_path,
)

PROTOCOL_VERSION = "2024-11-05"

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_pdf_text",
        "description": "Extract text from a validated local PDF file (all pages or a 0-based page_index).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "page_index": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_markdown",
        "description": "Read a validated local Markdown (.md/.markdown) file and extract its TOC headings.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_document",
        "description": "Run local BM25 search over a local PDF or Markdown file with page/heading citations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["path", "query"],
        },
    },
    {
        "name": "run_pdf_tool",
        "description": "Execute a registered local PDF tool from ToolRegistry with path_guard validation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string"},
                "inputs": {"type": "array", "items": {"type": "string"}},
                "output": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["tool_id", "output"],
        },
    },
]


def _jsonrpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_read_pdf_text(args: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
    raw_path = args.get("path", "")
    if not isinstance(raw_path, str):
        return None, (-32602, "Invalid path argument")
    ok, reason = validate_local_pdf_path(raw_path, check_exists=True)
    if not ok:
        return None, (-32602, f"Unsafe or invalid PDF path: {reason}")

    import pypdfium2 as pdfium

    resolved = Path(raw_path).resolve()
    doc = pdfium.PdfDocument(str(resolved))
    try:
        page_count = len(doc)
        page_idx = args.get("page_index")
        texts: list[dict[str, Any]] = []
        if page_idx is not None:
            idx = int(page_idx)
            if idx < 0 or idx >= page_count:
                return None, (-32602, f"page_index {idx} out of range (0..{page_count - 1})")
            indices = [idx]
        else:
            indices = list(range(page_count))

        for idx in indices:
            page = doc[idx]
            tp = None
            try:
                tp = page.get_textpage()
                txt = tp.get_text_range() or ""
            finally:
                if tp is not None:
                    tp.close()
                page.close()
            texts.append({"page_number": idx + 1, "text": txt})
    finally:
        doc.close()

    combined = "\n\n".join(f"[p. {item['page_number']}]\n{item['text']}" for item in texts)
    return (
        {
            "content": [{"type": "text", "text": combined}],
            "path": str(resolved),
            "page_count": page_count,
            "pages": texts,
        },
        None,
    )


def _tool_read_markdown(args: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
    raw_path = args.get("path", "")
    if not isinstance(raw_path, str):
        return None, (-32602, "Invalid path argument")
    ok, reason = validate_local_markdown_path(raw_path, check_exists=True)
    if not ok:
        return None, (-32602, f"Unsafe or invalid Markdown path: {reason}")

    resolved = Path(raw_path).resolve()
    text = resolved.read_text(encoding="utf-8", errors="replace")
    toc = [
        {"level": item.level, "title": item.title, "line": item.line_number, "anchor": item.anchor}
        for item in parse_markdown_toc(text)
    ]
    return (
        {
            "content": [{"type": "text", "text": text}],
            "path": str(resolved),
            "toc": toc,
        },
        None,
    )


def _tool_search_document(args: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
    raw_path = args.get("path", "")
    query = str(args.get("query") or "").strip()
    if not isinstance(raw_path, str) or not query:
        return None, (-32602, "Both 'path' and 'query' are required")
    ok, reason = validate_local_any_doc(raw_path, check_exists=True)
    if not ok:
        return None, (-32602, f"Unsafe or invalid document path: {reason}")

    resolved = Path(raw_path).resolve()
    top_k = max(1, min(20, int(args.get("top_k", 5))))
    idx = DocumentRagIndex()
    if resolved.suffix.lower() == ".pdf":
        idx.index_pdf(resolved)
    else:
        idx.index_markdown(resolved)
    hits = idx.search(query, top_k=top_k)

    serialized = [
        {
            "page_number": h.page_number,
            "section": h.section,
            "score": round(h.score, 4),
            "text": h.text,
        }
        for h in hits
    ]
    summary = "\n".join(
        f"[p. {h['page_number']}] {h['text'][:200]}"
        if h["page_number"] is not None
        else f"[{h['section']}] {h['text'][:200]}"
        for h in serialized
    )
    return (
        {
            "content": [{"type": "text", "text": summary}],
            "hits": serialized,
        },
        None,
    )


def _tool_run_pdf_tool(args: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[int, str] | None]:
    tool_id = str(args.get("tool_id") or "").strip()
    if not tool_id:
        return None, (-32602, "Missing tool_id")

    from .core.tools import ToolRegistry, validate_local_ext_path

    if not ToolRegistry.has_tool(tool_id):
        return None, (-32602, f"Unknown tool_id: {tool_id}")

    raw_inputs = args.get("inputs") or []
    if not isinstance(raw_inputs, list):
        return None, (-32602, "inputs must be a list")

    in_paths: list[Path] = []
    for item in raw_inputs:
        ok, resolved, reason = validate_local_ext_path(str(item), check_exists=True)
        if not ok or resolved is None:
            return None, (-32602, f"Unsafe or invalid input path: {reason}")
        in_paths.append(resolved)

    raw_output = args.get("output", "")
    if not isinstance(raw_output, str) or not raw_output.strip():
        return None, (-32602, "Missing output path")
    if _has_control_chars(raw_output) or _has_ntfs_ads(raw_output) or is_unc_or_network_path(raw_output):
        return None, (-32602, "Unsafe output path")

    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    res = ToolRegistry.get(tool_id).run(in_paths, Path(raw_output.strip()), params)
    return (
        {
            "content": [{"type": "text", "text": res.summary}],
            "success": res.success,
            "output_paths": [str(p) for p in res.output_paths],
            "summary": res.summary,
            "details": res.details,
        },
        None,
    )


def handle_jsonrpc_request(req: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single parsed JSON-RPC 2.0 request object (returns None for notifications)."""
    if not isinstance(req, dict):
        return _jsonrpc_err(None, -32600, "Invalid Request")

    req_id = req.get("id")
    method = req.get("method")
    if not isinstance(method, str) or not method.strip():
        return _jsonrpc_err(req_id, -32600, "Invalid Request: missing method")

    params = req.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _jsonrpc_err(req_id, -32602, "Invalid params")

    if method == "notifications/initialized":
        return None

    if method == "initialize":
        return _jsonrpc_ok(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pdf-md-lector-mcp", "version": read_version()},
            },
        )

    if method == "ping":
        return _jsonrpc_ok(req_id, {})

    if method == "tools/list":
        return _jsonrpc_ok(req_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        name = str(params.get("name") or "").strip()
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            return _jsonrpc_err(req_id, -32602, "Tool arguments must be an object")

        dispatch = {
            "read_pdf_text": _tool_read_pdf_text,
            "read_markdown": _tool_read_markdown,
            "search_document": _tool_search_document,
            "run_pdf_tool": _tool_run_pdf_tool,
        }
        handler = dispatch.get(name)
        if handler is None:
            return _jsonrpc_err(req_id, -32602, f"Unknown tool: {name}")

        try:
            result, err = handler(arguments)
        except Exception as exc:
            return _jsonrpc_err(req_id, -32603, f"Tool execution error: {exc}")
        if err is not None:
            return _jsonrpc_err(req_id, err[0], err[1])
        return _jsonrpc_ok(req_id, result)

    return _jsonrpc_err(req_id, -32601, f"Method not found: {method}")


def run_mcp_stdio(stdin_stream: TextIO | None = None, stdout_stream: TextIO | None = None) -> int:
    """Read newline-delimited JSON-RPC 2.0 requests from stdin and emit responses to stdout."""
    in_s = stdin_stream if stdin_stream is not None else sys.stdin
    out_s = stdout_stream if stdout_stream is not None else sys.stdout

    for raw_line in in_s:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            out_s.write(json.dumps(_jsonrpc_err(None, -32700, "Parse error"), ensure_ascii=False) + "\n")
            out_s.flush()
            continue

        resp = handle_jsonrpc_request(req)
        if resp is not None:
            out_s.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out_s.flush()

    return 0


def main() -> None:
    raise SystemExit(run_mcp_stdio())


if __name__ == "__main__":
    main()
