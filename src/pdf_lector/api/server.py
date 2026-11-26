"""Lazy FastAPI + Uvicorn local REST API server bound strictly to loopback with DPAPI Bearer auth."""

import hmac
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Any

from .. import APP_NAME, read_version
from ..core.path_guard import (
    _has_control_chars,
    _has_ntfs_ads,
    is_unc_or_network_path,
    validate_local_any_doc,
)
from ..core.secrets_store import get_secret, set_secret
from .schemas import (
    AiAskRequest,
    AiAskResponse,
    AiCitationSchema,
    HealthResponse,
    ToolInfo,
    ToolParamSchema,
    ToolRunRequest,
    ToolRunResponse,
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_TOKEN_SECRET_KEY = "local_api_token"


def ensure_api_bearer_token(
    *,
    secret_key: str = DEFAULT_TOKEN_SECRET_KEY,
    base_dir: Path | None = None,
) -> str:
    """Return persisted DPAPI Bearer token, generating and storing one if absent."""
    token = get_secret(secret_key, default=None, base_dir=base_dir)
    if token:
        return token
    generated = secrets.token_urlsafe(32)
    set_secret(secret_key, generated, base_dir=base_dir)
    return generated


def _validate_tool_params(defn: Any, params: dict[str, Any]) -> str | None:
    if "dpi" in params:
        raw_dpi = params["dpi"]
        if isinstance(raw_dpi, bool) or not isinstance(raw_dpi, int):
            return "dpi must be an integer"
        if raw_dpi < 36 or raw_dpi > 1200:
            return "dpi must be between 36 and 1200"

    if "quality" in params:
        raw_q = params["quality"]
        if isinstance(raw_q, bool) or not isinstance(raw_q, int):
            return "quality must be an integer"
        if raw_q < 10 or raw_q > 100:
            return "quality must be between 10 and 100"

    spec_map = {spec.name: spec for spec in getattr(defn, "params", ())}
    for key, val in params.items():
        spec = spec_map.get(key)
        if spec is None:
            continue
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if spec.min_val is not None and val < spec.min_val:
                return f"{key} below minimum ({spec.min_val})"
            if spec.max_val is not None and val > spec.max_val:
                return f"{key} above maximum ({spec.max_val})"
    return None


def create_fastapi_app(
    *,
    token_secret_key: str = DEFAULT_TOKEN_SECRET_KEY,
    base_dir: Path | None = None,
) -> Any:
    """Build the FastAPI application lazily with OpenAPI 3.1 and constant-time Bearer auth."""
    from fastapi import Depends, FastAPI, Header, HTTPException, status

    ensure_api_bearer_token(secret_key=token_secret_key, base_dir=base_dir)

    app = FastAPI(
        title=f"{APP_NAME} Local API",
        version=read_version(),
        description="Local-only loopback REST API for PDF/MD Lector by TRODAI.",
    )
    app.openapi_version = "3.1.0"

    def _require_bearer(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not isinstance(authorization, str):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization scheme",
                headers={"WWW-Authenticate": "Bearer"},
            )
        expected = get_secret(token_secret_key, default=None, base_dir=base_dir)
        if not expected or not hmac.compare_digest(token.strip(), expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token.strip()

    @app.get("/health", response_model=HealthResponse, dependencies=[Depends(_require_bearer)])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", app=APP_NAME, version=read_version())

    @app.get("/tools", response_model=list[ToolInfo], dependencies=[Depends(_require_bearer)])
    def list_tools() -> list[ToolInfo]:
        from ..core.tools import ToolRegistry, validate_local_ext_path

        items: list[ToolInfo] = []
        for defn in ToolRegistry.list_tools():
            items.append(
                ToolInfo(
                    id=defn.id,
                    category=defn.category,
                    params=[
                        ToolParamSchema(
                            name=p.name,
                            kind=p.kind,
                            default=p.default,
                            choices=list(p.choices) if p.choices is not None else None,
                            min_val=p.min_val,
                            max_val=p.max_val,
                        )
                        for p in defn.params
                    ],
                )
            )
        return items

    @app.post("/tools/{tool_id}", response_model=ToolRunResponse, dependencies=[Depends(_require_bearer)])
    def run_tool(tool_id: str, body: ToolRunRequest) -> ToolRunResponse:
        from ..core.tools import ToolRegistry, validate_local_ext_path

        if not ToolRegistry.has_tool(tool_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown tool: {tool_id}")

        defn = ToolRegistry.get(tool_id)
        param_err = _validate_tool_params(defn, body.params)
        if param_err:
            raise HTTPException(
                status_code=getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422),
                detail=param_err,
            )

        validated_inputs: list[Path] = []
        for raw_in in body.inputs:
            if not isinstance(raw_in, str) or not raw_in.strip():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty input path")
            if is_unc_or_network_path(raw_in) or _has_control_chars(raw_in) or _has_ntfs_ads(raw_in):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsafe input path blocked")
            ok, resolved, reason = validate_local_ext_path(raw_in, check_exists=True)
            if not ok or resolved is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid input path: {reason}")
            validated_inputs.append(resolved)

        out_path: Path | None = None
        if body.output is not None:
            raw_out = str(body.output).strip()
            if not raw_out or is_unc_or_network_path(raw_out) or _has_control_chars(raw_out) or _has_ntfs_ads(raw_out):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsafe output path blocked")
            out_path = Path(raw_out)

        res = defn.run(validated_inputs, out_path, params=dict(body.params))
        return ToolRunResponse(
            success=res.success,
            tool_id=tool_id,
            output_paths=[str(p) for p in res.output_paths],
            summary=res.summary,
            details=dict(res.details),
        )

    @app.post("/ai/ask", response_model=AiAskResponse, dependencies=[Depends(_require_bearer)])
    def ai_ask(body: AiAskRequest) -> AiAskResponse:
        from ..core.ai_rag import DocumentRagIndex, ask_document

        ok, reason = validate_local_any_doc(body.path, check_exists=True)
        if not ok:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid document path: {reason}")
        resolved = Path(body.path).resolve()

        idx = DocumentRagIndex()
        if resolved.suffix.lower() == ".pdf":
            idx.index_pdf(resolved)
        else:
            idx.index_markdown(resolved)

        ans = ask_document(
            idx,
            body.question,
            provider=body.provider,
            model=body.model,
            endpoint_url=body.endpoint_url,
            consent_granted=body.consent_granted,
            top_k=body.top_k,
            base_dir=base_dir,
        )
        return AiAskResponse(
            answer_text=ans.answer_text,
            provider=ans.provider,
            consent_blocked=ans.consent_blocked,
            used_local_only=ans.used_local_only,
            error=ans.error,
            citations=[
                AiCitationSchema(
                    source_path=c.source_path,
                    doc_type=c.doc_type,
                    page_number=c.page_number,
                    section=c.section,
                    score=c.score,
                    text=c.text,
                )
                for c in ans.citations
            ],
        )

    return app


class LocalApiServer:
    """Thread-managed loopback FastAPI/Uvicorn server (off by default)."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token_secret_key: str = DEFAULT_TOKEN_SECRET_KEY,
        base_dir: Path | None = None,
    ) -> None:
        clean_host = (host or "").strip()
        if clean_host not in LOOPBACK_HOSTS:
            raise ValueError(f"Non-loopback host rejected for security: {host!r}")
        self.host = clean_host
        self.port = int(port)
        self.token_secret_key = token_secret_key
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self._bound_port: int | None = None
        self._socket: socket.socket | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        if self._bound_port is not None:
            return self._bound_port
        return self.port

    @property
    def base_url(self) -> str:
        host_fmt = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host_fmt}:{self.bound_port}"

    def _create_uvicorn_server(self) -> tuple[Any, socket.socket]:
        import uvicorn

        app = create_fastapi_app(
            token_secret_key=self.token_secret_key,
            base_dir=self.base_dir,
        )

        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(128)
        self._bound_port = int(sock.getsockname()[1])

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self._bound_port,
            log_level="warning",
            access_log=False,
        )

        class _ThreadedUvicornServer(uvicorn.Server):
            def install_signal_handlers(self) -> None:
                return None

        return _ThreadedUvicornServer(config), sock

    def start(self, timeout_s: float = 5.0) -> "LocalApiServer":
        """Start the FastAPI/Uvicorn server in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            return self
        server, sock = self._create_uvicorn_server()
        self._server = server
        self._socket = sock
        self._thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [sock]},
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + timeout_s
        while not getattr(server, "started", False) and time.monotonic() < deadline:
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        return self

    def serve_forever(self) -> None:
        """Run the local API server synchronously (for CLI --serve-api)."""
        server, sock = self._create_uvicorn_server()
        self._server = server
        self._socket = sock
        try:
            server.run(sockets=[sock])
        finally:
            self.stop()

    def stop(self) -> None:
        """Signal Uvicorn shutdown and release the bound socket."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._server = None
