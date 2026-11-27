"""GitHub Releases updater with 24h cadence, solo-offline toggle, SHA-256 verification, and detached restart."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .win32_bridge import IS_WINDOWS, atomic_write_text, default_app_data_dir

GITHUB_REPO = "dairo707-maker/PDF-MD-Lector-by-TRODAI"
DEFAULT_RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
DEFAULT_CHECK_INTERVAL_HOURS = 24.0
UPDATER_STATE_FILENAME = "updater_state.json"

_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")


@dataclass
class ReleaseInfo:
    version: str
    tag_name: str
    notes: str
    html_url: str
    asset_url: str
    sums_url: str
    update_available: bool = False
    asset_name: str = ""
    asset_size: int = 0


@dataclass
class UpdaterState:
    last_update_check_utc: str | None = None
    skipped_version: str = ""
    offline_only: bool = False


def get_updater_state_path(app_dir: Path | None = None) -> Path:
    root = Path(app_dir) if app_dir is not None else default_app_data_dir()
    return root / UPDATER_STATE_FILENAME


def load_updater_state(path: Path | None = None) -> UpdaterState:
    target = path or get_updater_state_path()
    if not target.exists():
        return UpdaterState()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return UpdaterState()
        last_chk = raw.get("last_update_check_utc")
        return UpdaterState(
            last_update_check_utc=str(last_chk) if last_chk else None,
            skipped_version=str(raw.get("skipped_version") or "").strip(),
            offline_only=bool(raw.get("offline_only", False)),
        )
    except Exception:
        return UpdaterState()


def save_updater_state(state: UpdaterState, path: Path | None = None) -> Path:
    target = path or get_updater_state_path()
    atomic_write_text(target, json.dumps(asdict(state), indent=2, ensure_ascii=False))
    return target


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse semantic version string like 'v0.2.0' or '0.1.0-alpha.2' into integer tuple."""
    clean = (version_str or "").strip().lstrip("vV")
    core = clean.split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for seg in core.split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def should_run_auto_check(
    last_update_check_utc: str | None,
    *,
    offline_only: bool = False,
    interval_hours: float = DEFAULT_CHECK_INTERVAL_HOURS,
) -> bool:
    """Return True if offline_only is False and >= interval_hours elapsed since last check."""
    if offline_only:
        return False
    if not last_update_check_utc or not str(last_update_check_utc).strip():
        return True
    raw = str(last_update_check_utc).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt) >= timedelta(hours=float(interval_hours))
    except Exception:
        return True


def _validate_http_url(url: str) -> str:
    clean = (url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme.lower() not in ("https", "http") or not parsed.netloc:
        raise ValueError(f"Unsupported update URL: {url!r}")
    return clean


def _curl_fetch_bytes(url: str, timeout_s: float) -> bytes:
    proc = subprocess.run(
        ["curl.exe", "-sSL", "--fail", "--max-time", str(max(2, int(round(timeout_s)))), url],
        capture_output=True,
        timeout=timeout_s + 5.0,
        check=True,
    )
    return proc.stdout


def _http_get_bytes(url: str, *, timeout_s: float = 10.0, accept: str = "*/*") -> bytes:
    clean = _validate_http_url(url)
    req = Request(
        clean,
        headers={
            "Accept": accept,
            "User-Agent": "PDF-MD-Lector-Updater/0.1.0",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=float(timeout_s)) as resp:
            return resp.read()
    except HTTPError:
        raise
    except URLError:
        if IS_WINDOWS and clean.lower().startswith("https://"):
            return _curl_fetch_bytes(clean, timeout_s)
        raise


def _select_release_assets(assets: list[dict[str, Any]]) -> tuple[str, str, int, str]:
    sums_url = ""
    setup_candidate: tuple[str, str, int] | None = None
    portable_candidate: tuple[str, str, int] | None = None
    any_exe: tuple[str, str, int] | None = None

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        url = str(asset.get("browser_download_url") or "").strip()
        size = int(asset.get("size") or 0)
        low = name.lower()
        if low == "sha256sums.txt":
            sums_url = url
        elif low.endswith(".exe"):
            if "setup" in low and setup_candidate is None:
                setup_candidate = (url, name, size)
            elif "portable" in low and portable_candidate is None:
                portable_candidate = (url, name, size)
            elif any_exe is None:
                any_exe = (url, name, size)

    chosen = setup_candidate or portable_candidate or any_exe or ("", "", 0)
    return chosen[0], chosen[1], chosen[2], sums_url


def check_for_updates(
    current_version: str,
    *,
    api_url: str = DEFAULT_RELEASES_API_URL,
    offline_only: bool = False,
    timeout_s: float = 10.0,
    fetcher: Callable[[str, float], bytes] | None = None,
) -> ReleaseInfo | None:
    """Query GitHub Releases (or test endpoint) for the latest version; returns None on offline/404/error."""
    if offline_only:
        return None
    try:
        raw_bytes = (
            fetcher(api_url, timeout_s)
            if fetcher is not None
            else _http_get_bytes(api_url, timeout_s=timeout_s, accept="application/vnd.github+json")
        )
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        tag = str(data.get("tag_name") or "").strip()
        if not tag:
            return None
        version = tag.lstrip("vV").strip()
        assets = data.get("assets") or []
        asset_url, asset_name, asset_size, sums_url = _select_release_assets(
            assets if isinstance(assets, list) else []
        )
        return ReleaseInfo(
            version=version,
            tag_name=tag,
            notes=str(data.get("body") or ""),
            html_url=str(data.get("html_url") or ""),
            asset_url=asset_url,
            sums_url=sums_url,
            update_available=is_newer_version(version, current_version),
            asset_name=asset_name,
            asset_size=asset_size,
        )
    except Exception:
        return None


def parse_sha256sums(sums_text: str) -> dict[str, str]:
    """Parse SHA256SUMS.txt lines into {filename: lowercase_sha256}."""
    mapping: dict[str, str] = {}
    for line in (sums_text or "").splitlines():
        m = _SHA256_LINE_RE.match(line)
        if m:
            digest = m.group(1).lower()
            fname = Path(m.group(2).strip()).name
            mapping[fname] = digest
            mapping[fname.lower()] = digest
    return mapping


def verify_sha256(file_path: Path, expected_sha256: str) -> bool:
    """Stream-verify SHA-256 checksum of file_path."""
    hasher = hashlib.sha256()
    with Path(file_path).open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower() == expected_sha256.strip().lower()


def verify_authenticode_signature(file_path: Path) -> tuple[bool, str]:
    """Inspect Windows Authenticode status when available (never modifies cert stores)."""
    if not IS_WINDOWS:
        return True, "non_windows"
    try:
        escaped = str(file_path).replace("'", "''")
        cmd = f"(Get-AuthenticodeSignature -LiteralPath '{escaped}').Status"
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status = (proc.stdout or "").strip() or "Unknown"
        return status == "Valid", status
    except Exception as exc:
        return False, f"error:{exc}"


def download_verified_update(
    *,
    asset_url: str,
    sums_url: str,
    dest_dir: Path,
    filename: str | None = None,
    offline_only: bool = False,
    progress_cb: Callable[[int], None] | None = None,
    timeout_s: float = 30.0,
    require_authenticode: bool = False,
) -> Path:
    """Download update binary with streaming % progress and mandatory SHA256SUMS.txt verification."""
    if offline_only:
        raise RuntimeError("Updates disabled while offline_only is active")
    if not sums_url or not str(sums_url).strip():
        raise ValueError("Missing sha256 sums_url: refusing unverified update")

    clean_asset_url = _validate_http_url(asset_url)
    clean_sums_url = _validate_http_url(sums_url)

    sums_bytes = _http_get_bytes(clean_sums_url, timeout_s=timeout_s, accept="text/plain")
    sums_map = parse_sha256sums(sums_bytes.decode("utf-8", errors="replace"))
    if not sums_map:
        raise ValueError("Invalid or empty SHA256SUMS.txt")

    target_name = filename or Path(urlparse(clean_asset_url).path).name or "PDF-MD-Lector-Setup.exe"
    expected_sha = sums_map.get(target_name) or sums_map.get(target_name.lower())
    if not expected_sha:
        unique_all = set(sums_map.values())
        if len(unique_all) == 1:
            expected_sha = next(iter(unique_all))
        elif target_name.lower().endswith(".exe"):
            t_low = target_name.lower()
            matches = {
                digest
                for key, digest in sums_map.items()
                if key.lower().endswith(t_low)
                or ("setup" in t_low and "setup" in key.lower())
                or ("portable" in t_low and "portable" in key.lower())
            }
            if len(matches) == 1:
                expected_sha = next(iter(matches))
    if not expected_sha:
        raise ValueError(f"No sha256 entry found in SHA256SUMS.txt for {target_name}")

    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    final_path = dest_root / target_name
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target_name}.", suffix=".part", dir=str(dest_root))
    os.close(fd)
    part_path = Path(tmp_name)

    hasher = hashlib.sha256()
    try:
        req = Request(
            clean_asset_url,
            headers={"User-Agent": "PDF-MD-Lector-Updater/0.1.0"},
            method="GET",
        )
        try:
            with urlopen(req, timeout=float(timeout_s)) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                last_pct = -1
                with part_path.open("wb") as out_f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if progress_cb is not None and total > 0:
                            pct = min(99, int((downloaded * 100) / total))
                            if pct > last_pct:
                                last_pct = pct
                                progress_cb(pct)
        except HTTPError:
            raise
        except URLError:
            if IS_WINDOWS and clean_asset_url.lower().startswith("https://"):
                blob = _curl_fetch_bytes(clean_asset_url, timeout_s)
                part_path.write_bytes(blob)
                hasher.update(blob)
            else:
                raise

        actual_sha = hasher.hexdigest().lower()
        if actual_sha != expected_sha.lower():
            part_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise ValueError(f"sha256 mismatch for {target_name}: expected {expected_sha}, got {actual_sha}")

        if require_authenticode:
            ok_sig, sig_status = verify_authenticode_signature(part_path)
            if not ok_sig:
                part_path.unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
                raise ValueError(f"authenticode verification failed: {sig_status}")

        os.replace(part_path, final_path)
        if progress_cb is not None:
            progress_cb(100)
        return final_path
    except Exception:
        part_path.unlink(missing_ok=True)
        raise


def clean_meipass_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Strip PyInstaller _MEIPASS* and PYTHONHOME variables before detached child spawns."""
    base = dict(os.environ if env is None else env)
    for key in list(base.keys()):
        if key.startswith("_MEIPASS") or key == "PYTHONHOME":
            base.pop(key, None)
    return base


def build_detached_update_script(
    *,
    installer_path: Path,
    relaunch_exe: Path | None = None,
    wait_pid: int | None = None,
    log_path: Path | None = None,
) -> str:
    """Build detached Windows batch script clearing _MEIPASS* before running the verified installer."""
    inst = str(Path(installer_path))
    pid = int(wait_pid or os.getpid())
    inno_flags: list[str] = [
        "/SILENT",
        "/SP-",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/FORCECLOSEAPPLICATIONS",
    ]
    if relaunch_exe is not None:
        r_path = Path(relaunch_exe)
        install_dir = r_path.parent
        if "program files" not in str(install_dir).lower():
            inno_flags.append("/CURRENTUSER")
        if r_path.name.lower() == "pdf-md-lector.exe":
            inno_flags.append(f'/DIR="{install_dir}"')
    if log_path is not None:
        inno_flags.append(f'/LOG="{Path(log_path)}"')
    inno_arg_str = " ".join(inno_flags)

    lines = [
        "@echo off",
        "setlocal",
        'set "_MEIPASS="',
        'set "_MEIPASS2="',
        'set "PYTHONHOME="',
        "set /a tries=0",
        "ping -n 2 127.0.0.1 >nul",
        ":wait_loop",
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
        "if not errorlevel 1 (",
        "  set /a tries+=1",
        "  if %tries% GEQ 20 goto force_close",
        "  ping -n 2 127.0.0.1 >nul",
        "  goto wait_loop",
        ")",
        "goto run_installer",
        ":force_close",
        f"taskkill /F /T /PID {pid} >nul 2>&1",
        "ping -n 2 127.0.0.1 >nul",
        ":run_installer",
        f'start "" /wait "{inst}" {inno_arg_str}',
        "ping -n 2 127.0.0.1 >nul",
    ]
    if relaunch_exe is not None:
        lines.append(f'start "" "{Path(relaunch_exe)}"')
    lines.append("endlocal")
    return "\r\n".join(lines) + "\r\n"


def launch_detached_update(
    *,
    installer_path: Path,
    relaunch_exe: Path | None = None,
    has_unsaved_docs: bool = False,
    has_active_tasks: bool = False,
    script_dir: Path | None = None,
) -> Path:
    """Write and spawn detached hidden update script after verifying no unsaved docs or active tasks exist."""
    if has_unsaved_docs:
        raise RuntimeError("Cannot restart for update while documents have unsaved changes")
    if has_active_tasks:
        raise RuntimeError("Cannot restart for update while background tasks are active")
    inst = Path(installer_path)
    if not inst.exists():
        raise FileNotFoundError(f"Verified update installer missing: {inst}")

    target_dir = Path(script_dir) if script_dir is not None else Path(tempfile.gettempdir()) / "TRODAI_Updater"
    target_dir.mkdir(parents=True, exist_ok=True)
    script_path = target_dir / "trodai_apply_update.bat"
    log_path = target_dir / "setup_update.log"
    script_path.write_text(
        build_detached_update_script(
            installer_path=inst,
            relaunch_exe=relaunch_exe,
            log_path=log_path,
        ),
        encoding="utf-8",
    )

    child_env = clean_meipass_env()
    flags = 0
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["cmd.exe", "/c", str(script_path)],
        env=child_env,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return script_path
