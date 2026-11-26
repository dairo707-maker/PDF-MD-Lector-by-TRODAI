"""M5 Part B Tests: Windows Packaging, $0 Local Signing, Inno Setup 6, CI & Multi-AI Docs."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re
import subprocess
import sys

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parent.parent

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _load_build_exe_module():
    spec = importlib.util.spec_from_file_location("build_exe", ROOT / "scripts" / "build_exe.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_m5_packaging_build_exe_args_excludes_and_dry_run(tmp_path: Path) -> None:
    build_mod = _load_build_exe_module()
    assert build_mod.read_version() == "0.1.0"

    ico = build_mod.ensure_app_icon()
    assert ico.exists() and ico.name == "pdf_lector.ico"

    args = build_mod.build_pyinstaller_args(
        entry_script=tmp_path / "entry.py",
        ico_path=ico,
        work_dir=tmp_path / "work",
        dist_dir=tmp_path / "dist",
    )
    joined = " ".join(args)
    assert "--noconsole" in args
    assert "--windowed" in args
    assert "PDF-MD-Lector-Portable" in args
    assert "PySide6.QtWebEngineCore" in joined
    assert "PySide6.QtWebEngineWidgets" in joined
    assert "PySide6.QtWebEngineQuick" in joined
    assert "PySide6.QtMultimedia" not in joined
    assert "assets/fonts" in joined
    assert "assets/icons" in joined

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_exe.py"), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert "PDF-MD-Lector-Portable" in proc.stdout


def test_m5_packaging_wizard_bmps_and_deterministic_sha256sums(tmp_path: Path) -> None:
    build_mod = _load_build_exe_module()

    large_bmp, small_bmp = build_mod.generate_installer_wizard_images(output_dir=tmp_path / "bmp")
    assert large_bmp.exists() and small_bmp.exists()
    with Image.open(large_bmp) as im_lg:
        assert im_lg.size == (164, 314)
        assert im_lg.format == "BMP"
    with Image.open(small_bmp) as im_sm:
        assert im_sm.size == (55, 58)
        assert im_sm.format == "BMP"

    cleaned = build_mod.clean_child_env({"_MEIPASS": "C:\\tmp", "_MEIPASS2": "C:\\tmp2", "PYTHONHOME": "C:\\py", "KEEP": "1"})
    assert "_MEIPASS" not in cleaned
    assert "_MEIPASS2" not in cleaned
    assert "PYTHONHOME" not in cleaned
    assert cleaned["KEEP"] == "1"

    f_b = tmp_path / "PDF-MD-Lector-Setup.exe"
    f_a = tmp_path / "PDF-MD-Lector-Portable.exe"
    f_b.write_bytes(b"setup-payload-deterministic")
    f_a.write_bytes(b"portable-payload-deterministic")
    out_sums = tmp_path / "SHA256SUMS.txt"
    build_mod.write_sha256_manifest([f_b, f_a, tmp_path / "missing.exe"], output_path=out_sums)

    raw_bytes = out_sums.read_bytes()
    assert b"\r\n" not in raw_bytes
    lines = raw_bytes.decode("utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("  PDF-MD-Lector-Portable.exe")
    assert lines[1].endswith("  PDF-MD-Lector-Setup.exe")
    assert lines[0].split("  ")[0] == hashlib.sha256(f_a.read_bytes()).hexdigest()
    assert lines[1].split("  ")[0] == hashlib.sha256(f_b.read_bytes()).hexdigest()


def test_m5_packaging_sign_local_ps1_ast_and_dry_run_isolation() -> None:
    ps1 = ROOT / "scripts" / "sign_local.ps1"
    assert ps1.exists()
    ps1_text = ps1.read_text(encoding="utf-8")
    assert "New-SelfSignedCertificate" in ps1_text
    assert "Set-AuthenticodeSignature" in ps1_text
    assert "CN=TRODAI Local Code Signing (PDF/MD Lector)" in ps1_text
    assert not EMOJI_PATTERN.search(ps1_text)

    ast_cmd = (
        "$errors = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
    )
    ast_proc = subprocess.run(["powershell", "-NoProfile", "-Command", ast_cmd], capture_output=True, text=True, timeout=20)
    assert ast_proc.returncode == 0, ast_proc.stderr

    snap_cmd = (
        "Get-ChildItem Cert:\\CurrentUser\\Root, Cert:\\CurrentUser\\My | "
        "Select-Object -ExpandProperty Thumbprint | Sort-Object"
    )
    before = subprocess.run(["powershell", "-NoProfile", "-Command", snap_cmd], capture_output=True, text=True, timeout=20)
    for extra in ([], ["-InstallTrust"], ["-UninstallTrust"]):
        run = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), *extra, "-DryRun"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert run.returncode == 0, run.stderr
        assert "[DRY-RUN]" in run.stdout
    after = subprocess.run(["powershell", "-NoProfile", "-Command", snap_cmd], capture_output=True, text=True, timeout=20)
    assert before.stdout == after.stdout


def test_m5_packaging_inno_setup_iss_contract() -> None:
    iss = ROOT / "packaging" / "installer.iss"
    assert iss.exists()
    txt = iss.read_text(encoding="utf-8")
    assert "AppId=TRODAI.PDFMDLector" in txt
    assert 'AppUserModelID: "{#MyAppUserModelID}"' in txt or "TRODAI.PDFMDLector" in txt
    assert "PrivilegesRequired=lowest" in txt
    assert "PrivilegesRequiredOverridesAllowed=dialog" in txt
    assert "OutputBaseFilename=PDF-MD-Lector-Setup" in txt
    assert "Spanish.isl" in txt
    assert "Default.isl" in txt
    assert "Portuguese.isl" in txt
    assert "SignTool=" in txt
    assert "[UninstallDelete]" in txt
    assert "PrivilegesRequired=admin" not in txt
    assert "C:\\Users\\" not in txt
    assert not EMOJI_PATTERN.search(txt)


def test_m5_packaging_ci_workflow_contract() -> None:
    wf = ROOT / ".github" / "workflows" / "build-release.yml"
    assert wf.exists()
    txt = wf.read_text(encoding="utf-8")
    for required in (
        "uv sync --extra dev",
        "pytest",
        "scripts/build_exe.py",
        "SHA256SUMS.txt",
        "actions/attest-build-provenance@v2",
        "id-token: write",
        "attestations: write",
        "innosetup",
        "signpath",
    ):
        assert required.lower() in txt.lower(), f"Missing CI token: {required}"
    assert not EMOJI_PATTERN.search(txt)


def test_m5_packaging_gitignore_oss_and_multi_ai_docs() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".venv/", "build/", "dist/", "__pycache__/", ".pytest_cache/", "*.pyc", "*.log", "secrets.json"):
        assert pattern in gitignore, f"Missing .gitignore rule: {pattern}"

    license_txt = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_txt
    assert "TRODAI" in license_txt

    claude_txt = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "docs/HANDOFF_STATUS.md" in claude_txt
    assert "## 1." in claude_txt and "## 2." in claude_txt and "## 3." in claude_txt and "## 4." in claude_txt

    expected_docs = {
        "README.md": ("PDF/MD Lector", "TRODAI", "MIT"),
        "CHANGELOG.md": ("0.1.0", "ToolRegistry"),
        "CONTRIBUTING.md": ("--link-mode=copy", "path_guard", "Shiboken6"),
        "SECURITY.md": ("DPAPI", "nh3", "path_guard"),
        "AGENTS.md": ("QT_QPA_PLATFORM", "Space Grotesk", "HANDOFF_STATUS.md"),
        "docs/ARCHITECTURE.md": ("ADR-001", "ADR-005", "ToolRegistry"),
        "docs/API_AND_MCP.md": ("127.0.0.1", "pdf-md-lector-mcp", "--serve-api"),
        "docs/SECURITY_AND_PRIVACY.md": ("CryptProtectData", "resolve_safe_doc_resource", "redaction.py"),
        "docs/CODE_SIGNING_AND_INSTALL.md": ("sign_local.ps1", "PrivilegesRequired=lowest", "attest-build-provenance"),
        "docs/AUDIT_HANDOFF.md": ("HANDOFF_STATUS.md", "Invariants", "Verification Commands"),
        ".github/ISSUE_TEMPLATE/bug_report.yml": ("Anonymized Logs", "Windows Version"),
        ".github/ISSUE_TEMPLATE/feature_request.yml": ("Functional Area", "Local-First"),
        ".github/PULL_REQUEST_TEMPLATE.md": ("uv run --no-sync pytest", "polygonal"),
    }
    for rel, needles in expected_docs.items():
        p = ROOT / rel
        assert p.exists() and p.stat().st_size > 0, f"Missing or empty: {rel}"
        content = p.read_text(encoding="utf-8")
        for needle in needles:
            assert needle in content, f"{rel} missing {needle!r}"

    # Zero Unicode emojis in scripts, workflow, packaging, and this test file
    for rel in (
        "scripts/build_exe.py",
        "scripts/sign_local.ps1",
        "packaging/installer.iss",
        ".github/workflows/build-release.yml",
        "tests/test_m5_packaging_docs.py",
    ):
        assert not EMOJI_PATTERN.search((ROOT / rel).read_text(encoding="utf-8")), f"Emoji found in {rel}"
