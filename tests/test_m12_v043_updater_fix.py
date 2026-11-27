"""v0.4.3 (M12) regression tests for hidden detached Windows updater script and /wait ordering."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time

import pytest

from pdf_lector.core.updater import build_detached_update_script, launch_detached_update


def test_m12_detached_update_script_waits_without_timeout_or_console_flash(tmp_path: Path) -> None:
    inst = tmp_path / "PDF-MD-Lector-Setup.exe"
    relaunch_user = Path(r"C:\Users\tester\AppData\Local\Programs\TRODAI\PDF-MD-Lector\PDF-MD-Lector.exe")
    log_file = tmp_path / "setup_update.log"

    script_user = build_detached_update_script(
        installer_path=inst,
        relaunch_exe=relaunch_user,
        wait_pid=99999,
        log_path=log_file,
    )
    assert 'set "_MEIPASS="' in script_user
    assert 'set "PYTHONHOME="' in script_user
    assert "timeout /t" not in script_user
    assert "ping -n 2 127.0.0.1 >nul" in script_user
    assert "if %tries% GEQ 20 goto force_close" in script_user
    assert f'start "" /wait "{inst}"' in script_user
    assert "/SILENT /SP- /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS" in script_user
    assert "/CURRENTUSER" in script_user
    assert f'/DIR="{relaunch_user.parent}"' in script_user
    assert f'/LOG="{log_file}"' in script_user
    assert script_user.index('start "" /wait') < script_user.index(f'start "" "{relaunch_user}"')

    # System Program Files path omits /CURRENTUSER
    relaunch_pf = Path(r"C:\Program Files\TRODAI\PDF-MD-Lector\PDF-MD-Lector.exe")
    script_pf = build_detached_update_script(
        installer_path=inst,
        relaunch_exe=relaunch_pf,
        wait_pid=99999,
    )
    assert "/CURRENTUSER" not in script_pf
    assert f'/DIR="{relaunch_pf.parent}"' in script_pf


def test_m12_launch_detached_update_uses_create_no_window_and_waits_before_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inst = tmp_path / "PDF-MD-Lector-Setup.exe"
    inst.write_bytes(b"MZ-MOCK")
    captured: dict[str, object] = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    script_path = launch_detached_update(
        installer_path=inst,
        relaunch_exe=tmp_path / "PDF-MD-Lector.exe",
        script_dir=tmp_path / "upd_dir",
    )
    assert script_path.exists()
    kw = captured["kwargs"]
    flags = int(kw["creationflags"])  # type: ignore[index]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert not (flags & subprocess.DETACHED_PROCESS)
    assert kw["stdin"] == subprocess.DEVNULL  # type: ignore[index]
    assert kw["stdout"] == subprocess.DEVNULL  # type: ignore[index]
    assert kw["stderr"] == subprocess.DEVNULL  # type: ignore[index]
    monkeypatch.undo()

    # Real cmd.exe /c integration proving start "" /wait waits until slow installer finishes before relaunch
    marker = tmp_path / "installed_version.txt"
    result_file = tmp_path / "relaunched_read.txt"
    slow_installer = tmp_path / "slow_installer.bat"
    slow_installer.write_text(
        "@echo off\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        f'echo 0.4.3>"{marker}"\r\n',
        encoding="utf-8",
    )
    relauncher = tmp_path / "fake_relauncher.bat"
    relauncher.write_text(
        "@echo off\r\n"
        f'if exist "{marker}" (copy /y "{marker}" "{result_file}" >nul) else (echo MISSING>"{result_file}")\r\n',
        encoding="utf-8",
    )
    runner = tmp_path / "test_order.bat"
    runner.write_text(
        "@echo off\r\n"
        f'start "" /wait cmd /c "{slow_installer}"\r\n'
        f'start "" /wait cmd /c "{relauncher}"\r\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["cmd.exe", "/c", str(runner)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=True,
    )
    assert proc.returncode == 0
    for _ in range(30):
        if result_file.exists():
            break
        time.sleep(0.1)
    assert result_file.exists()
    assert result_file.read_text(encoding="utf-8").strip() == "0.4.3"
