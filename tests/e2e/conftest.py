"""Shared fixtures and collection-safe lazy-import harness for tests/e2e/."""

from __future__ import annotations

from functools import partial
import http.server
import importlib
import json
import os
from pathlib import Path
import socketserver
import sys
import threading
from typing import Any, Callable

# Enforce offscreen Qt rendering before any PySide6 widget/app import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

import pytest
from PySide6.QtWidgets import QApplication

from pdf_lector.core.bookmarks_store import BookmarksStore
from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.i18n import HUB_CATEGORIES
from pdf_lector.core.usage_insights import LocalUsageInsights
from pdf_lector.theme.palette import register_embedded_fonts
from pdf_lector.theme.theme_profile import get_preset
from pdf_lector.ui.main_window import MainWindow


def _collect_unique_tool_ids() -> tuple[str, ...]:
    seen: list[str] = []
    for cat in HUB_CATEGORIES:
        for tool in cat.tools:
            if tool.tool_id not in seen:
                seen.append(tool.tool_id)
    return tuple(seen)


ALL_TOOL_IDS: tuple[str, ...] = _collect_unique_tool_ids()


def pytest_configure(config: pytest.Config) -> None:
    """Register custom E2E markers so --strict-markers passes cleanly."""
    config.addinivalue_line("markers", "fixtures_smoke: Immediate baseline self-tests for synthetic fixtures.")
    config.addinivalue_line("markers", "tier1: Tier 1 feature coverage tests (FA-01..FA-25).")
    config.addinivalue_line("markers", "tier2: Tier 2 boundary, corner, negative, and security tests.")
    config.addinivalue_line("markers", "tier3: Tier 3 cross-feature pairwise combinatorial tests.")
    config.addinivalue_line("markers", "tier4: Tier 4 realistic end-to-end application workloads.")


def load_attr(module_path: str, attr_name: str | None = None) -> Any:
    """Lazily import a module or attribute inside test bodies without masking missing modules."""
    mod = importlib.import_module(module_path)
    return getattr(mod, attr_name) if attr_name else mod


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Provide a session-scoped offscreen QApplication with TRODAI embedded fonts."""
    app = QApplication.instance() or QApplication(["pytest-pdf-lector-e2e"])
    register_embedded_fonts(app)
    return app


@pytest.fixture()
def isolated_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate LOCALAPPDATA, APPDATA, and TRODAI_TEST_ROOT into tmp_path."""
    root = tmp_path / "localappdata"
    root.mkdir(parents=True, exist_ok=True)
    roaming = tmp_path / "appdata"
    roaming.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("TRODAI_TEST_ROOT", str(root))
    return root


@pytest.fixture()
def clean_subprocess_env(isolated_appdata: Path, tmp_path: Path) -> dict[str, str]:
    """Return a clean subprocess environment isolated from real user AppData and PyInstaller _MEIPASS."""
    env = dict(os.environ)
    for key in list(env.keys()):
        if key.startswith("_MEIPASS") or key == "PYTHONHOME":
            env.pop(key, None)
    temp_dir = tmp_path / "proc_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["LOCALAPPDATA"] = str(isolated_appdata)
    env["APPDATA"] = str(isolated_appdata.parent / "appdata")
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    src_path = str(WORKSPACE_ROOT / "src")
    existing_pp = env.get("PYTHONPATH", "")
    if existing_pp:
        parts = existing_pp.split(os.pathsep)
        if src_path not in parts:
            env["PYTHONPATH"] = os.pathsep.join([src_path, existing_pp])
    else:
        env["PYTHONPATH"] = src_path
    return env


@pytest.fixture()
def make_isolated_window(qapp: QApplication, tmp_path: Path, isolated_appdata: Path) -> Callable[..., MainWindow]:
    """Factory creating MainWindow instances backed by isolated tmp_path stores."""
    created: list[MainWindow] = []

    def _factory(**overrides: Any) -> MainWindow:
        profile = overrides.pop("profile", get_preset("trodai_corporate"))
        store_dir = isolated_appdata / "TRODAI" / "PDFLector"
        store_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "persist_profile": False,
            "sample_pdf_dir": tmp_path,
            "insights_store": LocalUsageInsights(store_dir / "usage_insights.json"),
            "bookmarks_store": BookmarksStore(store_dir / "bookmarks.json"),
            "dictionary_engine": DictionaryEngine(store_dir / "dictionaries"),
        }
        kwargs.update(overrides)
        win = MainWindow(profile, **kwargs)
        created.append(win)
        return win

    yield _factory

    for win in reversed(created):
        try:
            win.close()
        except Exception:
            pass
    qapp.processEvents()


class _FakeRegKey:
    def __init__(self, hive: int, subkey: str, backend: InMemoryWinReg) -> None:
        self.hive = hive
        self.subkey = subkey
        self.backend = backend
        self.closed = False

    def __enter__(self) -> _FakeRegKey:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.closed = True


class InMemoryWinReg:
    """Drop-in in-memory winreg replacement isolating all HKCU registry mutations."""

    HKEY_CURRENT_USER = 0x80000001
    HKEY_LOCAL_MACHINE = 0x80000002
    KEY_READ = 0x20019
    KEY_WRITE = 0x20006
    KEY_ALL_ACCESS = 0xF003F
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_DWORD = 4

    def __init__(self) -> None:
        self._store: dict[tuple[int, str], dict[str, tuple[Any, int]]] = {}
        self.fail_writes_with: type[BaseException] | None = None

    @staticmethod
    def _norm(subkey: str) -> str:
        return subkey.strip("\\").lower()

    def _ensure_chain(self, hive: int, subkey: str) -> str:
        norm = self._norm(subkey)
        parts = [p for p in norm.split("\\") if p]
        cur = ""
        for part in parts:
            cur = f"{cur}\\{part}" if cur else part
            self._store.setdefault((hive, cur), {})
        if not parts:
            self._store.setdefault((hive, ""), {})
        return norm

    def CreateKey(self, hive: Any, subkey: str) -> _FakeRegKey:
        return self.CreateKeyEx(hive, subkey)

    def CreateKeyEx(
        self,
        hive: Any,
        subkey: str,
        reserved: int = 0,
        access: int = KEY_ALL_ACCESS,
    ) -> _FakeRegKey:
        del reserved, access
        if self.fail_writes_with is not None:
            raise self.fail_writes_with("Injected registry write error")
        root_hive = hive.hive if isinstance(hive, _FakeRegKey) else int(hive)
        full = f"{hive.subkey}\\{subkey}" if isinstance(hive, _FakeRegKey) and hive.subkey else subkey
        norm = self._ensure_chain(root_hive, full)
        return _FakeRegKey(root_hive, norm, self)

    def OpenKey(
        self,
        hive: Any,
        subkey: str,
        reserved: int = 0,
        access: int = KEY_READ,
    ) -> _FakeRegKey:
        return self.OpenKeyEx(hive, subkey, reserved=reserved, access=access)

    def OpenKeyEx(
        self,
        hive: Any,
        subkey: str,
        reserved: int = 0,
        access: int = KEY_READ,
    ) -> _FakeRegKey:
        del reserved, access
        root_hive = hive.hive if isinstance(hive, _FakeRegKey) else int(hive)
        full = f"{hive.subkey}\\{subkey}" if isinstance(hive, _FakeRegKey) and hive.subkey else subkey
        norm = self._norm(full)
        if (root_hive, norm) not in self._store:
            raise FileNotFoundError(norm)
        return _FakeRegKey(root_hive, norm, self)

    def SetValueEx(
        self,
        key: _FakeRegKey,
        value_name: str,
        reserved: int,
        reg_type: int,
        value: Any,
    ) -> None:
        del reserved
        if self.fail_writes_with is not None:
            raise self.fail_writes_with("Injected registry write error")
        self._store.setdefault((key.hive, key.subkey), {})[value_name] = (value, reg_type)

    def QueryValueEx(self, key: _FakeRegKey, value_name: str) -> tuple[Any, int]:
        bucket = self._store.get((key.hive, key.subkey))
        if bucket is None or value_name not in bucket:
            raise FileNotFoundError(value_name)
        return bucket[value_name]

    def DeleteValue(self, key: _FakeRegKey, value_name: str) -> None:
        if self.fail_writes_with is not None:
            raise self.fail_writes_with("Injected registry delete error")
        bucket = self._store.get((key.hive, key.subkey), {})
        if value_name not in bucket:
            raise FileNotFoundError(value_name)
        del bucket[value_name]

    def DeleteKey(self, hive: Any, subkey: str) -> None:
        if self.fail_writes_with is not None:
            raise self.fail_writes_with("Injected registry delete error")
        root_hive = hive.hive if isinstance(hive, _FakeRegKey) else int(hive)
        full = f"{hive.subkey}\\{subkey}" if isinstance(hive, _FakeRegKey) and hive.subkey else subkey
        norm = self._norm(full)
        if (root_hive, norm) not in self._store:
            raise FileNotFoundError(norm)
        prefix = f"{norm}\\"
        for k_hive, k_sub in list(self._store.keys()):
            if k_hive == root_hive and k_sub.startswith(prefix):
                raise OSError("Cannot delete key with subkeys")
        del self._store[(root_hive, norm)]

    def DeleteKeyEx(self, hive: Any, subkey: str) -> None:
        self.DeleteKey(hive, subkey)

    def EnumKey(self, key: _FakeRegKey, index: int) -> str:
        prefix = f"{key.subkey}\\" if key.subkey else ""
        children = sorted(
            {
                sub[len(prefix) :].split("\\", 1)[0]
                for (h, sub) in self._store
                if h == key.hive and sub.startswith(prefix) and sub != key.subkey
            }
        )
        if index < 0 or index >= len(children):
            raise OSError("No more data is available")
        return children[index]

    def EnumValue(self, key: _FakeRegKey, index: int) -> tuple[str, Any, int]:
        items = list(self._store.get((key.hive, key.subkey), {}).items())
        if index < 0 or index >= len(items):
            raise OSError("No more data is available")
        name, (val, rtype) = items[index]
        return name, val, rtype

    def CloseKey(self, key: _FakeRegKey) -> None:
        key.closed = True

    def key_exists(self, subkey: str, hive: int = HKEY_CURRENT_USER) -> bool:
        return (hive, self._norm(subkey)) in self._store

    def get_value(self, subkey: str, value_name: str = "", hive: int = HKEY_CURRENT_USER) -> Any:
        bucket = self._store.get((hive, self._norm(subkey)), {})
        if value_name not in bucket:
            return None
        return bucket[value_name][0]

    def snapshot(self) -> dict[tuple[int, str], dict[str, tuple[Any, int]]]:
        return {k: dict(v) for k, v in self._store.items()}


@pytest.fixture()
def fake_winreg() -> InMemoryWinReg:
    """Provide an isolated in-memory winreg mock (never touches real HKCU)."""
    return InMemoryWinReg()


class LocalMockServer:
    """Thread-safe loopback HTTP server (127.0.0.1:0) recording requests and serving mocked routes."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, bytes, str]] = {}
        self.requests_log: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd: _ReusableServer | None = None
        self._thread: threading.Thread | None = None

    def set_route(
        self,
        method: str,
        path: str,
        status: int,
        body: bytes | str | dict[str, Any] | list[Any],
        content_type: str = "application/json",
    ) -> None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
        with self._lock:
            self.routes[(method.upper(), path)] = (status, payload, content_type)

    def start(self) -> LocalMockServer:
        handler = partial(_MockRequestHandler, server_state=self)
        self._httpd = _ReusableServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def record_request(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.requests_log.append(entry)

    def lookup_route(self, method: str, path: str) -> tuple[int, bytes, str] | None:
        with self._lock:
            return self.routes.get((method.upper(), path))


class _ReusableServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _MockRequestHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, *args: Any, server_state: LocalMockServer, **kwargs: Any) -> None:
        self._state = server_state
        super().__init__(*args, **kwargs)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        path = self.path.split("?", 1)[0]
        self._state.record_request(
            {
                "method": method,
                "path": path,
                "full_path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        hit = self._state.lookup_route(method, path)
        if hit is None:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        status, payload, ctype = hit
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        del format, args
        return


@pytest.fixture()
def local_mock_server() -> LocalMockServer:
    """Start an isolated 127.0.0.1:0 loopback mock HTTP server."""
    srv = LocalMockServer().start()
    try:
        yield srv
    finally:
        srv.stop()
