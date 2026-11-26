"""Screen capture (QScreen), WIA COM scanner bridge, and QtMultimedia QCamera acquisition."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import ctypes
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any

from PIL import Image

from .ocr_engine import clean_child_env, normalize_source_lang
from .path_guard import is_unc_or_network_path
from .temp_manager import TempManager

COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 signed


class CaptureError(RuntimeError):
    """Raised when screen, scanner, or camera capture fails."""


@dataclass(frozen=True)
class CaptureDeviceInfo:
    """Normalized descriptor for a discovered WIA scanner or QtMultimedia camera."""

    device_id: str
    name: str
    kind: str  # "wia" | "camera"


def _com_initialize() -> bool:
    """Initialize COM apartment on current thread; return True if CoUninitialize is needed."""
    if os.name != "nt":
        return False
    try:
        ole32 = ctypes.OleDLL("ole32")
        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        return hr in (0, 1)  # S_OK or S_FALSE
    except OSError as exc:
        if getattr(exc, "winerror", None) == RPC_E_CHANGED_MODE:
            return False
        return False


def _com_uninitialize(should_uninit: bool) -> None:
    if should_uninit and os.name == "nt":
        try:
            ctypes.OleDLL("ole32").CoUninitialize()
        except OSError:
            pass


def _to_pil_image(obj: Any) -> Image.Image:
    """Convert PIL.Image, QPixmap, QImage, bytes, or local Path into a PIL RGB Image."""
    if isinstance(obj, Image.Image):
        img = obj.copy()
        img.load()
        return img

    if isinstance(obj, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(obj)))
        img.load()
        return img

    if isinstance(obj, (str, Path)):
        if is_unc_or_network_path(obj):
            raise CaptureError("network_or_url_blocked")
        p = Path(obj)
        if not p.exists() or not p.is_file():
            raise CaptureError(f"Captured image file missing: {p}")
        with Image.open(p) as opened:
            opened.load()
            return opened.copy()

    # Lazy PySide6 QPixmap / QImage conversion without hard top-level dependency
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage, QPixmap

    if isinstance(obj, QPixmap):
        if obj.isNull():
            raise CaptureError("Captured QPixmap is null")
        qimg = obj.toImage()
    elif isinstance(obj, QImage):
        if obj.isNull():
            raise CaptureError("Captured QImage is null")
        qimg = obj
    else:
        raise CaptureError(f"Unsupported captured object type: {type(obj).__name__}")

    qbuf = QBuffer()
    qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = qimg.save(qbuf, "PNG")
    raw = bytes(qbuf.data())
    qbuf.close()
    if not ok or not raw:
        raise CaptureError("Failed to encode QImage to PNG buffer")
    with Image.open(io.BytesIO(raw)) as decoded:
        decoded.load()
        return decoded.copy()


def _flatten_alpha(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def save_images_to_pdf(
    images: Sequence[Any],
    output_pdf: Path,
    *,
    dpi: int = 150,
    quality: int = 85,
    title: str = "PDF/MD Lector Capture",
    subject: str = "",
) -> Path:
    """Compile one or more images into a local PDF honoring DPI and JPEG quality."""
    if not images:
        raise CaptureError("No images supplied to save_images_to_pdf")
    if is_unc_or_network_path(output_pdf):
        raise ValueError("network_or_url_blocked")

    dpi_val = int(dpi)
    if dpi_val < 36 or dpi_val > 1200:
        raise ValueError(f"dpi out of range 36..1200: {dpi_val}")
    qual_val = max(10, min(100, int(quality)))

    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_mgr = TempManager(register_atexit=False)

    try:
        with temp_mgr.task_scope(secure=False) as tid:
            fd, raw_stage = tempfile.mkstemp(
                prefix=f".{out_path.stem}.{os.getpid()}.",
                suffix=f".cap.tmp{out_path.suffix or '.pdf'}",
                dir=str(out_path.parent),
            )
            os.close(fd)
            stage = Path(raw_stage)
            temp_mgr.register_path(stage, task_id=tid, secure=False)

            canv = canvas.Canvas(str(stage))
            canv.setTitle(title)
            if subject:
                canv.setSubject(subject)

            for item in images:
                pil_img = _flatten_alpha(_to_pil_image(item))
                w_pt = max(1.0, pil_img.width * 72.0 / dpi_val)
                h_pt = max(1.0, pil_img.height * 72.0 / dpi_val)
                if qual_val < 95:
                    q_buf = io.BytesIO()
                    pil_img.save(q_buf, format="JPEG", quality=qual_val, optimize=True)
                    q_buf.seek(0)
                    embed_img = ImageReader(q_buf)
                else:
                    embed_img = ImageReader(pil_img)

                canv.setPageSize((w_pt, h_pt))
                canv.drawImage(embed_img, 0, 0, width=w_pt, height=h_pt)
                canv.showPage()

            canv.save()
            os.replace(stage, out_path)
    finally:
        temp_mgr.cleanup_all()

    return out_path


def parse_capture_region(region: Any) -> tuple[int, int, int, int] | None:
    """Validate optional (x, y, width, height) region; raises ValueError on zero/negative size."""
    if region is None:
        return None
    if isinstance(region, dict):
        vals = (region.get("x", 0), region.get("y", 0), region.get("width", 0), region.get("height", 0))
    elif isinstance(region, (tuple, list)) and len(region) == 4:
        vals = tuple(region)
    else:
        raise ValueError(f"Invalid capture region specification: {region!r}")

    x, y, w, h = (int(vals[0]), int(vals[1]), int(vals[2]), int(vals[3]))
    if w <= 0 or h <= 0:
        raise ValueError(f"Capture region width and height must be > 0, got {(x, y, w, h)}")
    return (x, y, w, h)


def capture_qscreen_region(
    region: tuple[int, int, int, int] | None = None,
    *,
    screen_index: int = 0,
    screen_grab_fn: Callable[[tuple[int, int, int, int] | None], Any] | None = None,
) -> Any:
    """Capture a screen region via injected seam or Qt QScreen.grabWindow."""
    parsed = parse_capture_region(region)
    if screen_grab_fn is not None:
        grabbed = screen_grab_fn(parsed)
        if grabbed is None:
            raise CaptureError("screen_grab_fn returned None")
        return grabbed

    from PySide6.QtGui import QGuiApplication

    screens = QGuiApplication.screens()
    if not screens:
        raise CaptureError("No screens detected by QGuiApplication")
    idx = max(0, min(int(screen_index), len(screens) - 1))
    screen = screens[idx]

    if parsed is None:
        pixmap = screen.grabWindow(0)
    else:
        x, y, w, h = parsed
        pixmap = screen.grabWindow(0, x, y, w, h)

    if pixmap.isNull():
        raise CaptureError("Screen capture returned null QPixmap")
    return pixmap


def capture_region_to_pdf(
    output_pdf: Path,
    *,
    region: Any = None,
    dpi: int = 150,
    quality: int = 85,
    screen_index: int = 0,
    screen_grab_fn: Callable[[tuple[int, int, int, int] | None], Any] | None = None,
) -> Path:
    """Capture screen region and save as a DPI-scaled PDF."""
    grabbed = capture_qscreen_region(
        region=region,
        screen_index=screen_index,
        screen_grab_fn=screen_grab_fn,
    )
    return save_images_to_pdf(
        [grabbed],
        output_pdf,
        dpi=dpi,
        quality=quality,
        title="PDF/MD Lector Screen Capture",
    )


def probe_wia_devices(
    *,
    wia_probe_fn: Callable[[], list[CaptureDeviceInfo]] | None = None,
) -> list[CaptureDeviceInfo]:
    """Probe connected WIA scanners on Windows using a COM-initialized worker thread."""
    if wia_probe_fn is not None:
        return list(wia_probe_fn())
    if os.name != "nt":
        return []

    devices: list[CaptureDeviceInfo] = []

    def _worker() -> None:
        need_uninit = _com_initialize()
        try:
            # Try win32com.client first if present, else PowerShell WIA.DeviceManager
            try:
                import win32com.client  # type: ignore[import-not-found]

                dm = win32com.client.Dispatch("WIA.DeviceManager")
                for info in dm.DeviceInfos:
                    if int(getattr(info, "Type", 0)) == 1:  # ScannerDeviceType
                        did = str(getattr(info, "DeviceID", ""))
                        name = str(info.Properties("Name").Value) if hasattr(info, "Properties") else "WIA Scanner"
                        devices.append(CaptureDeviceInfo(device_id=did, name=name, kind="wia"))
                return
            except Exception:
                pass

            ps_cmd = (
                "$dm = New-Object -ComObject WIA.DeviceManager; "
                "$out = @(); "
                "foreach ($d in $dm.DeviceInfos) { "
                "  if ($d.Type -eq 1) { "
                "    $n = ($d.Properties | Where-Object { $_.Name -eq 'Name' }).Value; "
                "    $out += @{ id = [string]$d.DeviceID; name = [string]$n } "
                "  } "
                "}; "
                "$out | ConvertTo-Json -Compress"
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                env=clean_child_env(),
                creationflags=flags,
                timeout=8,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parsed = json.loads(proc.stdout.strip())
                items = parsed if isinstance(parsed, list) else [parsed]
                for item in items:
                    if isinstance(item, dict) and item.get("id"):
                        devices.append(
                            CaptureDeviceInfo(
                                device_id=str(item["id"]),
                                name=str(item.get("name") or "WIA Scanner"),
                                kind="wia",
                            )
                        )
        except Exception:
            pass
        finally:
            _com_uninitialize(need_uninit)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=10.0)
    return devices


def probe_cameras(
    *,
    camera_probe_fn: Callable[[], list[CaptureDeviceInfo]] | None = None,
) -> list[CaptureDeviceInfo]:
    """Probe available QtMultimedia video camera inputs safely."""
    if camera_probe_fn is not None:
        return list(camera_probe_fn())
    try:
        from PySide6.QtMultimedia import QMediaDevices

        cams = QMediaDevices.videoInputs()
        return [
            CaptureDeviceInfo(
                device_id=bytes(cam.id()).decode("utf-8", errors="replace"),
                name=cam.description() or "Camera",
                kind="camera",
            )
            for cam in cams
        ]
    except Exception:
        return []


def acquire_from_wia(
    *,
    device_id: str = "",
    dpi: int = 200,
    wia_acquire_fn: Callable[[str, int], Any] | None = None,
) -> Image.Image:
    """Acquire a scanned page image via WIA COM in a CoInitializeEx worker thread."""
    if wia_acquire_fn is not None:
        raw = wia_acquire_fn(device_id, dpi)
        if raw is None:
            raise CaptureError("WIA scanner returned no image")
        return _to_pil_image(raw)

    if os.name != "nt":
        raise CaptureError("WIA scanner is only supported on Windows")

    holder: dict[str, Any] = {}

    def _worker() -> None:
        need_uninit = _com_initialize()
        temp_mgr = TempManager(register_atexit=False)
        try:
            devices = probe_wia_devices()
            if not devices:
                holder["error"] = CaptureError("No WIA scanner device found")
                return
            target_id = device_id or devices[0].device_id
            with temp_mgr.task_scope(secure=False) as tid:
                tmp_dir = temp_mgr.create_temp_dir(task_id=tid, secure=False)
                out_bmp = tmp_dir / "wia_scan.bmp"
                safe_id = target_id.replace("'", "''")
                safe_out = str(out_bmp).replace("'", "''")
                ps_cmd = (
                    "$dm = New-Object -ComObject WIA.DeviceManager; "
                    f"$info = $dm.DeviceInfos | Where-Object {{ $_.DeviceID -eq '{safe_id}' }} | Select-Object -First 1; "
                    "if (-not $info) { throw 'WIA device not found' }; "
                    "$dev = $info.Connect(); "
                    "$item = $dev.Items.Item(1); "
                    "try { ($item.Properties | Where-Object { $_.PropertyID -eq 6147 }).Value = " + str(int(dpi)) + " } catch {}; "
                    "try { ($item.Properties | Where-Object { $_.PropertyID -eq 6148 }).Value = " + str(int(dpi)) + " } catch {}; "
                    "$img = $item.Transfer('{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}'); "
                    f"$img.SaveFile('{safe_out}')"
                )
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    env=clean_child_env(),
                    creationflags=flags,
                    timeout=45,
                )
                if proc.returncode != 0 or not out_bmp.exists():
                    holder["error"] = CaptureError(proc.stderr.strip() or "WIA acquisition failed")
                    return
                holder["image"] = _to_pil_image(out_bmp)
        except Exception as exc:
            holder["error"] = CaptureError(str(exc))
        finally:
            temp_mgr.cleanup_all()
            _com_uninitialize(need_uninit)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=50.0)
    if "error" in holder:
        raise holder["error"]
    if "image" not in holder:
        raise CaptureError("WIA scanner acquisition timed out or produced no image")
    return holder["image"]


def acquire_from_camera(
    *,
    device_id: str = "",
    camera_capture_fn: Callable[[str], Any] | None = None,
) -> Image.Image:
    """Capture a single frame from a QtMultimedia QCamera (or injected seam)."""
    if camera_capture_fn is not None:
        raw = camera_capture_fn(device_id)
        if raw is None:
            raise CaptureError("Camera capture returned no frame")
        return _to_pil_image(raw)

    from PySide6.QtCore import QElapsedTimer, QEventLoop
    from PySide6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices

    cams = QMediaDevices.videoInputs()
    if not cams:
        raise CaptureError("No camera device found")

    chosen = cams[0]
    if device_id:
        for cam in cams:
            if bytes(cam.id()).decode("utf-8", errors="replace") == device_id:
                chosen = cam
                break

    camera = QCamera(chosen)
    capture = QImageCapture()
    session = QMediaCaptureSession()
    session.setCamera(camera)
    session.setImageCapture(capture)

    captured: dict[str, Any] = {}
    loop = QEventLoop()

    def _on_captured(_req_id: int, qimg: Any) -> None:
        captured["image"] = qimg
        loop.quit()

    def _on_error(_req_id: int, _err: Any, msg: str) -> None:
        captured["error"] = msg
        loop.quit()

    capture.imageCaptured.connect(_on_captured)
    capture.errorOccurred.connect(_on_error)
    camera.start()

    try:
        timer = QElapsedTimer()
        timer.start()
        capture.capture()
        while "image" not in captured and "error" not in captured and timer.elapsed() < 5000:
            loop.processEvents()
    finally:
        camera.stop()

    if "error" in captured:
        raise CaptureError(f"Camera capture failed: {captured['error']}")
    if "image" not in captured:
        raise CaptureError("Camera capture timed out")
    return _to_pil_image(captured["image"])


def acquire_device_to_pdf(
    output_pdf: Path,
    *,
    source_type: str = "auto",
    dpi: int = 200,
    quality: int = 85,
    source_lang: str = "auto",
    device_id: str = "",
    device_acquire_fn: Callable[[], Any] | None = None,
    wia_probe_fn: Callable[[], list[CaptureDeviceInfo]] | None = None,
    wia_acquire_fn: Callable[[str, int], Any] | None = None,
    camera_probe_fn: Callable[[], list[CaptureDeviceInfo]] | None = None,
    camera_capture_fn: Callable[[str], Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Acquire one or more pages from WIA scanner or QCamera and compile to PDF."""
    norm_lang = normalize_source_lang(source_lang)
    src_kind = (source_type or "auto").strip().lower()
    if src_kind not in ("auto", "wia", "camera"):
        raise ValueError(f"Unsupported capture source_type: {source_type!r}")

    acquired_items: list[Any] = []
    used_source = src_kind

    if device_acquire_fn is not None:
        raw = device_acquire_fn()
        if raw is None:
            raise CaptureError("No scanner or camera device image acquired")
        if isinstance(raw, (list, tuple)):
            acquired_items = [item for item in raw if item is not None]
        else:
            acquired_items = [raw]
        used_source = "custom_device"
    else:
        if src_kind in ("auto", "wia"):
            wia_devs = probe_wia_devices(wia_probe_fn=wia_probe_fn)
            if wia_devs or wia_acquire_fn is not None:
                img = acquire_from_wia(device_id=device_id, dpi=dpi, wia_acquire_fn=wia_acquire_fn)
                acquired_items = [img]
                used_source = "wia"

        if not acquired_items and src_kind in ("auto", "camera"):
            cam_devs = probe_cameras(camera_probe_fn=camera_probe_fn)
            if cam_devs or camera_capture_fn is not None:
                img = acquire_from_camera(device_id=device_id, camera_capture_fn=camera_capture_fn)
                acquired_items = [img]
                used_source = "camera"

    if not acquired_items:
        raise CaptureError("No scanner or camera device available")

    saved_path = save_images_to_pdf(
        acquired_items,
        output_pdf,
        dpi=dpi,
        quality=quality,
        title="PDF/MD Lector Scan Import",
        subject=f"source={used_source};lang={norm_lang}",
    )
    return saved_path, {
        "source_type": used_source,
        "dpi": int(dpi),
        "quality": int(quality),
        "source_lang": norm_lang,
        "page_count": len(acquired_items),
    }
