"""Local stdio JSON-RPC 2.0 MCP client for pdf-md-lector-mcp and compatible servers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any


class McpClientError(RuntimeError):
    """Raised when an MCP JSON-RPC call fails or returns an error object."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _clean_subprocess_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env.keys()):
        if key.startswith("_MEIPASS") or key == "PYTHONHOME":
            env.pop(key, None)
    if overrides:
        env.update(dict(overrides))
    return env


class McpStdioClient:
    """Thread-safe newline-delimited JSON-RPC 2.0 client over subprocess stdio."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        if not command:
            raise ValueError("MCP server command cannot be empty")
        self._command = [str(part) for part in command]
        self._env = _clean_subprocess_env(env)
        self._cwd = str(cwd) if cwd is not None else None
        self._timeout_s = float(timeout_s)
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=self._env,
            cwd=self._cwd,
            creationflags=flags,
        )
        self._reader_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._reader_thread.start()

    def _read_stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._lines.put(None)
            return
        try:
            for line in proc.stdout:
                stripped = line.strip()
                if stripped:
                    self._lines.put(stripped)
        finally:
            self._lines.put(None)

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a JSON-RPC 2.0 request and wait for the matching response ID."""
        with self._lock:
            self._ensure_started()
            assert self._proc is not None and self._proc.stdin is not None
            self._next_id += 1
            req_id = self._next_id
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params if params is not None else {},
            }
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

            while True:
                try:
                    raw = self._lines.get(timeout=self._timeout_s)
                except queue.Empty as exc:
                    raise TimeoutError(f"MCP server timed out waiting for {method}") from exc
                if raw is None:
                    raise McpClientError("MCP server closed stdout unexpectedly")
                resp = json.loads(raw)
                if resp.get("id") != req_id:
                    continue
                if "error" in resp:
                    err = resp["error"]
                    raise McpClientError(
                        str(err.get("message", "MCP error")),
                        code=err.get("code"),
                    )
                return resp.get("result")

    def initialize(
        self,
        *,
        client_name: str = "pdf-md-lector-ui",
        client_version: str = "0.1.0",
    ) -> dict[str, Any]:
        """Perform MCP initialize handshake."""
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": client_name, "version": client_version},
            },
        )
        return dict(result or {})

    def list_tools(self) -> list[dict[str, Any]]:
        """List tools advertised by the MCP server."""
        result = self.request("tools/list", {})
        return list((result or {}).get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke an MCP tool by name with JSON arguments."""
        result = self.request(
            "tools/call",
            {"name": name, "arguments": arguments if arguments is not None else {}},
        )
        return dict(result or {})

    def close(self) -> None:
        """Close stdin and terminate the MCP server process cleanly."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None

    def __enter__(self) -> McpStdioClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
