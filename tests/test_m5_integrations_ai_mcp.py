"""Unit tests for M5 Part A: Windows HKCU/CLI, MV3 + Native Messaging, REST API, AI RAG, and MCP."""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from PySide6.QtWidgets import QApplication
import pytest

from tests.e2e.conftest import (  # noqa: F401
    WORKSPACE_ROOT,
    InMemoryWinReg,
    clean_subprocess_env,
    isolated_appdata,
    local_mock_server,
    qapp,
)
from tests.e2e.fixtures import (
    decode_nm_stream,
    encode_nm_frame,
    make_gfm_markdown_bundle,
    make_image_pdf,
    make_multipage_toc_pdf,
    make_scanned_text_pdf,
)


def test_m5_integrations_windows_hkcu_and_cli_helpers(tmp_path: Path, clean_subprocess_env) -> None:
    del clean_subprocess_env
    from pdf_lector.core import windows_integration as wi

    reg = InMemoryWinReg()
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, r"Software\Classes\.pdf\OpenWithProgids") as k:
        reg.SetValueEx(k, "ThirdParty.PDF", 0, reg.REG_SZ, "")

    exe = Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe")
    res = wi.register_windows_integration(exe, include_markdown=True, winreg_backend=reg)
    assert res.success is True
    assert reg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.PDF\shell\merge\command")
    assert reg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.PDF\shell\compress\command")
    assert reg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.PDF\shell\sign\command")
    assert reg.get_value(r"Software\Classes\TRODAI.PDFMDLector.PDF", "AppUserModelID") == "TRODAI.PDFMDLector"
    assert reg.get_value(r"Software\Classes\.md\OpenWithProgids", "TRODAI.PDFMDLector.MD") == ""
    assert reg.get_value(r"Software\Classes\.markdown\OpenWithProgids", "TRODAI.PDFMDLector.MD") == ""

    manifest_path = tmp_path / "com.trodai.pdf_md_lector.json"
    wi.write_native_messaging_manifest(manifest_path, exe)
    assert manifest_path.exists()
    nm_res = wi.register_native_messaging_host(manifest_path, winreg_backend=reg)
    assert nm_res.success is True
    assert reg.get_value(r"Software\Google\Chrome\NativeMessagingHosts\com.trodai.pdf_md_lector") == str(manifest_path)
    assert reg.get_value(r"Software\Microsoft\Edge\NativeMessagingHosts\com.trodai.pdf_md_lector") == str(manifest_path)

    launched: list[str] = []
    assert wi.open_default_apps_settings(launcher=launched.append) is True
    assert launched == ["ms-settings:defaultapps"]

    assert wi.unregister_native_messaging_host(winreg_backend=reg).success is True
    assert not reg.key_exists(r"Software\Google\Chrome\NativeMessagingHosts\com.trodai.pdf_md_lector")

    assert wi.unregister_windows_integration(winreg_backend=reg).success is True
    assert not reg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.PDF")
    assert reg.get_value(r"Software\Classes\.pdf\OpenWithProgids", "ThirdParty.PDF") == ""

    # Headless CLI dispatch helpers
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=2)
    out_merged = tmp_path / "merged.pdf"
    parser = wi.build_cli_parser()
    args_gui = parser.parse_args([])
    assert wi.run_cli_command(args_gui) is None

    args_merge = parser.parse_args(["--merge", str(p1), str(p2), "-o", str(out_merged)])
    assert wi.run_cli_command(args_merge) == 0
    assert out_merged.exists()

    out_comp = tmp_path / "comp.pdf"
    args_comp = parser.parse_args(["--compress", str(out_merged), "-o", str(out_comp), "--quality", "65"])
    assert wi.run_cli_command(args_comp) == 0
    assert out_comp.exists()

    args_bad_unc = parser.parse_args(["--compress", r"\\evil\share\x.pdf", "-o", str(tmp_path / "bad.pdf")])
    assert wi.run_cli_command(args_bad_unc) != 0


def test_m5_integrations_native_messaging_and_extension_icons(tmp_path: Path) -> None:
    from pdf_lector.core import native_messaging as nm

    ext_dir = WORKSPACE_ROOT / "browser-extension"
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert "nativeMessaging" in manifest["permissions"]
    assert "https://" not in (ext_dir / "popup.html").read_text(encoding="utf-8").lower()
    for fname in ("background.js", "popup.html", "popup.js"):
        assert (ext_dir / fname).exists()

    curve_re = re.compile(r"[CSQTacsqta]")
    for sz in (16, 48, 128):
        svg_path = ext_dir / "icons" / f"icon{sz}.svg"
        png_path = ext_dir / "icons" / f"icon{sz}.png"
        assert svg_path.exists() and png_path.exists()
        root = ET.parse(svg_path).getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            assert tag not in ("circle", "ellipse")
            if tag == "path":
                assert not curve_re.search(elem.attrib.get("d", ""))

    # In-memory framing tests
    pdf_p = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=1)
    img_pdf = make_image_pdf(tmp_path / "img.pdf")
    web_out = tmp_path / "web.pdf"
    comp_out = tmp_path / "comp.pdf"

    stream_in = (
        encode_nm_frame({"action": "ping"})
        + encode_nm_frame({"action": "open_pdf", "path": str(pdf_p)})
        + encode_nm_frame({"action": "webpage_to_pdf", "html": "<h1>Test</h1>", "output": str(web_out), "dpi": 150})
        + encode_nm_frame({"action": "quick_action", "tool_id": "compress_pdf", "inputs": [str(img_pdf)], "output": str(comp_out)})
        + encode_nm_frame({"action": "open_pdf", "path": r"\\10.0.0.1\share\x.pdf"})
        + encode_nm_frame({"action": "unknown_cmd"})
    )
    out_buf = io.BytesIO()
    rc = nm.run_stdio_loop(io.BytesIO(stream_in), out_buf)
    assert rc == 0
    replies = decode_nm_stream(out_buf.getvalue())
    assert len(replies) == 6
    assert replies[0]["ok"] is True
    assert replies[1]["ok"] is True
    assert replies[2]["ok"] is True and web_out.exists()
    assert replies[3]["ok"] is True and comp_out.exists()
    assert replies[4]["ok"] is False
    assert replies[5]["ok"] is False

    # Oversized >1MB payload rejected cleanly
    big_in = io.BytesIO(struct.pack("<I", 2 * 1024 * 1024) + b"{}")
    big_out = io.BytesIO()
    assert nm.run_stdio_loop(big_in, big_out) == 0
    assert decode_nm_stream(big_out.getvalue())[0]["ok"] is False


def test_m5_integrations_rest_api_loopback_bearer_and_ai_route(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    from pdf_lector.api import server as api_srv
    from pdf_lector.core import secrets_store

    with pytest.raises(ValueError):
        api_srv.LocalApiServer(host="0.0.0.0", port=0)

    secrets_store.set_secret("local_api_token", "m5-unit-token")
    pdf_p = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=4)
    comp_out = tmp_path / "api_comp.pdf"

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0).start()
    try:
        with urlopen(f"{srv.base_url}/openapi.json", timeout=5) as resp:
            schema = json.loads(resp.read().decode("utf-8"))
            assert schema["openapi"].startswith("3.1")

        for hdr in (None, "Basic dGVzdA==", "Bearer wrong", "Bearer "):
            req = Request(f"{srv.base_url}/health", headers={"Authorization": hdr} if hdr else {})
            with pytest.raises(HTTPError) as exc:
                urlopen(req, timeout=5)
            assert exc.value.code == 401

        req_tools = Request(f"{srv.base_url}/tools", headers={"Authorization": "Bearer m5-unit-token"})
        with urlopen(req_tools, timeout=5) as resp:
            tools = json.loads(resp.read().decode("utf-8"))
            assert len(tools) >= 44

        req_exec = Request(
            f"{srv.base_url}/tools/compress_pdf",
            data=json.dumps({"inputs": [str(pdf_p)], "output": str(comp_out), "params": {"dpi": 150, "quality": 70}}).encode("utf-8"),
            headers={"Authorization": "Bearer m5-unit-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req_exec, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["success"] is True and comp_out.exists()

        req_ai = Request(
            f"{srv.base_url}/ai/ask",
            data=json.dumps({"path": str(pdf_p), "question": "NIT 900123456 DIAN", "provider": "local_bm25"}).encode("utf-8"),
            headers={"Authorization": "Bearer m5-unit-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req_ai, timeout=10) as resp:
            ai_data = json.loads(resp.read().decode("utf-8"))
            assert ai_data["used_local_only"] is True
            assert ai_data["citations"][0]["page_number"] == 3
    finally:
        srv.stop()


def test_m5_integrations_ai_rag_mcp_and_ui_panel(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
    clean_subprocess_env,
    qapp: QApplication,
) -> None:
    del isolated_appdata
    from pdf_lector.core import ai_rag
    from pdf_lector.core.mcp_client import McpStdioClient
    from pdf_lector.ui.ai_panel import AskDocumentPanel

    pdf_p = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=4)
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    scan_p = make_scanned_text_pdf(tmp_path / "scan.pdf")

    # Empty / scanned doc no ZeroDivisionError
    empty_idx = ai_rag.DocumentRagIndex()
    empty_idx.index_pdf(scan_p)
    assert empty_idx.search("anything") == []

    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(pdf_p)
    idx.index_markdown(md_p)

    local_ans = ai_rag.ask_document(idx, "NIT 900123456 CUFE", provider="local_bm25")
    assert local_ans.used_local_only is True
    assert "p. 3" in local_ans.answer_text

    # Consent gate + DPAPI secret + zero leak on HTTP error
    ai_rag.save_byok_key("openai_compatible", "sk-secret-never-leak-999")
    assert ai_rag.load_byok_key("openai_compatible") == "sk-secret-never-leak-999"
    blocked = ai_rag.ask_document(
        idx,
        "Test",
        provider="openai_compatible",
        endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions",
        consent_granted=False,
    )
    assert blocked.consent_blocked is True
    assert local_mock_server.requests_log == []

    local_mock_server.set_route("POST", "/v1/chat/completions", 401, {"error": "Bad key sk-secret-never-leak-999"})
    err_ans = ai_rag.ask_document(
        idx,
        "Test",
        provider="openai_compatible",
        endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions",
        consent_granted=True,
    )
    assert "sk-secret-never-leak-999" not in err_ans.answer_text
    assert "sk-secret-never-leak-999" not in (err_ans.error or "")

    # MCP stdio client + server
    with McpStdioClient([sys.executable, "-m", "pdf_lector.mcp_server"], env=clean_subprocess_env) as client:
        init_res = client.initialize()
        assert init_res["serverInfo"]["name"] == "pdf-md-lector-mcp"
        tools = client.list_tools()
        assert {t["name"] for t in tools} >= {"read_pdf_text", "read_markdown", "search_document", "run_pdf_tool"}
        md_res = client.call_tool("read_markdown", {"path": str(md_p)})
        assert "Titulo Principal" in json.dumps(md_res)

    # PySide6 AskDocumentPanel
    panel = AskDocumentPanel(lambda: (pdf_p, ""), mode="dark", lang="es")
    try:
        panel.show()
        qapp.processEvents()
        assert panel.isVisible()
        assert panel.consent_checkbox.isChecked() is False
        panel.apply_theme("light")
        panel.retranslate("en")
        assert panel.title_lbl.text() == "Ask AI"
        panel.question_edit.setText("DIAN UBL 2.1")
        panel.ask_current_question()
        qapp.processEvents()
    finally:
        panel.close()
        qapp.processEvents()
