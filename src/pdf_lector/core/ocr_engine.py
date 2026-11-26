"""Offline-first OCR engine for PDF/MD Lector: WinRT Windows.Media.Ocr, Tesseract probe, and optional RapidOCR."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Any

from PIL import Image

from .path_guard import is_unc_or_network_path, validate_local_pdf_path
from .temp_manager import TempManager
from .win32_bridge import is_cloud_only_file

SUPPORTED_OCR_LANGS: tuple[str, ...] = ("auto", "es", "en", "pt", "fr", "de", "it", "multi")

_TESSERACT_LANG_MAP: dict[str, str] = {
    "auto": "spa+eng",
    "multi": "spa+eng",
    "es": "spa",
    "en": "eng",
    "pt": "por",
    "fr": "fra",
    "de": "deu",
    "it": "ita",
}

_WINRT_BCP47_MAP: dict[str, str] = {
    "es": "es-ES",
    "en": "en-US",
    "pt": "pt-BR",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
}


class NoOcrBackendError(RuntimeError):
    """Raised when no usable OCR backend is available or enabled."""


@dataclass(frozen=True)
class OcrWord:
    """Recognized OCR word with pixel bounding box in source image coordinates."""

    text: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class OcrLine:
    """Recognized OCR text line."""

    text: str
    words: list[OcrWord] = field(default_factory=list)


@dataclass(frozen=True)
class OcrPageResult:
    """OCR recognition output for a single PDF page or image."""

    page_index: int
    text: str
    lines: list[OcrLine]
    backend: str
    source_lang: str
    dpi: int


@dataclass(frozen=True)
class OcrDocumentResult:
    """Aggregated OCR output across all pages of a document."""

    pages: list[OcrPageResult]
    backend: str
    total_chars: int
    source_lang: str
    dpi: int


def normalize_source_lang(source_lang: str | None) -> str:
    """Validate and normalize OCR source language code."""
    code = (source_lang or "auto").strip().lower()
    if code not in SUPPORTED_OCR_LANGS:
        raise ValueError(f"Unsupported source_lang: {source_lang!r}")
    return code


def clean_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return environment dict with PyInstaller _MEIPASS* vars stripped before spawning subprocesses."""
    env = dict(os.environ)
    for k in list(env.keys()):
        if k.startswith("_MEIPASS"):
            env.pop(k, None)
    if extra:
        env.update(extra)
    return env


def _run_coro_sync(coro: Any) -> Any:
    """Run an async WinRT/asyncio coroutine safely even if an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    holder: dict[str, Any] = {}

    def _runner() -> None:
        try:
            holder["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=60.0)
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def _create_winrt_engine(source_lang: str = "auto") -> Any | None:
    """Instantiate Windows.Media.Ocr.OcrEngine for the requested language or user profile."""
    if os.name != "nt":
        return None
    try:
        from winrt.windows.media.ocr import OcrEngine
    except Exception:
        return None

    norm = normalize_source_lang(source_lang)
    bcp47 = _WINRT_BCP47_MAP.get(norm)
    if bcp47 and hasattr(OcrEngine, "try_create_from_language"):
        try:
            from winrt.windows.globalization import Language

            lang_obj = Language(bcp47)
            if getattr(OcrEngine, "is_language_supported", lambda _l: True)(lang_obj):
                eng = OcrEngine.try_create_from_language(lang_obj)
                if eng is not None:
                    return eng
        except Exception:
            pass

    try:
        return OcrEngine.try_create_from_user_profile_languages()
    except Exception:
        return None


def probe_winrt_ocr(source_lang: str = "auto") -> bool:
    """Return True when Windows 11 WinRT OCR is available on this host."""
    return _create_winrt_engine(source_lang) is not None


def find_tesseract_executable() -> Path | None:
    """Probe PATH and standard local Windows directories for tesseract.exe."""
    which_hit = shutil.which("tesseract")
    candidates: list[Path] = []
    if which_hit:
        candidates.append(Path(which_hit))

    local_app = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (prog_files, prog_files_x86, local_app):
        if base:
            candidates.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
            candidates.append(Path(base) / "Programs" / "Tesseract-OCR" / "tesseract.exe")

    for cand in candidates:
        try:
            if is_unc_or_network_path(cand):
                continue
            if cand.exists() and cand.is_file() and not is_cloud_only_file(cand):
                return cand
        except OSError:
            continue
    return None


def probe_rapidocr() -> bool:
    """Return True when optional rapidocr_onnxruntime package is installed."""
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def get_available_ocr_backends(source_lang: str = "auto") -> dict[str, bool]:
    """Report availability of WinRT, Tesseract, and optional RapidOCR backends."""
    return {
        "winrt": probe_winrt_ocr(source_lang),
        "tesseract": find_tesseract_executable() is not None,
        "rapidocr": probe_rapidocr(),
    }


def recognize_image_winrt(image: Image.Image, *, source_lang: str = "auto") -> tuple[str, list[OcrLine]]:
    """Run Windows 11 WinRT OCR on a PIL Image."""
    engine = _create_winrt_engine(source_lang)
    if engine is None:
        raise NoOcrBackendError("Windows.Media.Ocr engine is unavailable")

    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    rgba = image.convert("RGBA")
    raw_bytes = rgba.tobytes("raw", "RGBA")

    writer = DataWriter()
    writer.write_bytes(raw_bytes)
    buf = writer.detach_buffer()

    bitmap = SoftwareBitmap(
        BitmapPixelFormat.RGBA8,
        rgba.width,
        rgba.height,
        BitmapAlphaMode.PREMULTIPLIED,
    )
    bitmap.copy_from_buffer(buf)

    async def _recognize() -> Any:
        return await engine.recognize_async(bitmap)

    winrt_result = _run_coro_sync(_recognize())
    if winrt_result is None:
        return "", []

    lines: list[OcrLine] = []
    text_lines: list[str] = []
    for ln in getattr(winrt_result, "lines", []) or []:
        words: list[OcrWord] = []
        for wd in getattr(ln, "words", []) or []:
            rect = getattr(wd, "bounding_rect", None)
            wx = float(getattr(rect, "x", 0.0)) if rect is not None else 0.0
            wy = float(getattr(rect, "y", 0.0)) if rect is not None else 0.0
            ww = float(getattr(rect, "width", 0.0)) if rect is not None else 0.0
            wh = float(getattr(rect, "height", 0.0)) if rect is not None else 0.0
            w_txt = str(getattr(wd, "text", "") or "")
            if w_txt:
                words.append(OcrWord(text=w_txt, x=wx, y=wy, width=ww, height=wh))
        ln_txt = str(getattr(ln, "text", "") or "").strip()
        if not ln_txt and words:
            ln_txt = " ".join(w.text for w in words)
        if ln_txt:
            text_lines.append(ln_txt)
            lines.append(OcrLine(text=ln_txt, words=words))

    full_text = str(getattr(winrt_result, "text", "") or "").strip()
    if not full_text and text_lines:
        full_text = "\n".join(text_lines)
    return full_text, lines


def recognize_image_tesseract(
    image: Image.Image,
    *,
    source_lang: str = "auto",
    tesseract_cmd: Path | None = None,
) -> tuple[str, list[OcrLine]]:
    """Run local Tesseract executable on a PIL Image via stdin/stdout."""
    exe = tesseract_cmd or find_tesseract_executable()
    if exe is None:
        raise NoOcrBackendError("Tesseract executable not found")

    lang = normalize_source_lang(source_lang)
    tess_lang = _TESSERACT_LANG_MAP.get(lang, "eng")
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        [str(exe), "stdin", "stdout", "-l", tess_lang, "--psm", "3"],
        input=buf.getvalue(),
        capture_output=True,
        env=clean_child_env(),
        creationflags=flags,
        timeout=60,
    )
    if proc.returncode != 0:
        # Retry with eng if requested language pack is missing
        if tess_lang != "eng":
            proc = subprocess.run(
                [str(exe), "stdin", "stdout", "-l", "eng", "--psm", "3"],
                input=buf.getvalue(),
                capture_output=True,
                env=clean_child_env(),
                creationflags=flags,
                timeout=60,
            )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Tesseract failed ({proc.returncode}): {err}")

    text = proc.stdout.decode("utf-8", errors="replace").strip()
    lines = [OcrLine(text=ln.strip()) for ln in text.splitlines() if ln.strip()]
    return text, lines


def recognize_image_rapidocr(image: Image.Image, *, source_lang: str = "auto") -> tuple[str, list[OcrLine]]:
    """Run optional on-demand RapidOCR ONNX engine on a PIL Image."""
    del source_lang
    if not probe_rapidocr():
        raise NoOcrBackendError("rapidocr_onnxruntime is not installed")
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="PNG")
    result, _ = engine(buf.getvalue())
    lines: list[OcrLine] = []
    if result:
        for item in result:
            if len(item) >= 2 and item[1]:
                lines.append(OcrLine(text=str(item[1]).strip()))
    full_text = "\n".join(ln.text for ln in lines if ln.text)
    return full_text, lines


def _normalize_provider_output(raw: Any) -> tuple[str, list[OcrLine]]:
    if raw is None:
        return "", []
    if isinstance(raw, str):
        text = raw.strip()
        lines = [OcrLine(text=ln.strip()) for ln in text.splitlines() if ln.strip()]
        return text, lines
    if isinstance(raw, tuple) and len(raw) == 2:
        text = str(raw[0] or "").strip()
        raw_lines = raw[1] or []
        lines = [
            ln if isinstance(ln, OcrLine) else OcrLine(text=str(ln))
            for ln in raw_lines
        ]
        return text, lines
    text = str(raw).strip()
    lines = [OcrLine(text=ln.strip()) for ln in text.splitlines() if ln.strip()]
    return text, lines


def recognize_image(
    image: Image.Image,
    *,
    source_lang: str = "auto",
    ocr_provider: Callable[[Image.Image, str], Any] | None = None,
    disable_winrt_probe: bool = False,
    disable_tesseract_probe: bool = False,
    disable_rapidocr_probe: bool = False,
    preferred_backend: str = "auto",
) -> tuple[str, list[OcrLine], str]:
    """Recognize text from a PIL image via injected provider, WinRT, Tesseract, or RapidOCR."""
    norm_lang = normalize_source_lang(source_lang)

    if ocr_provider is not None:
        text, lines = _normalize_provider_output(ocr_provider(image, norm_lang))
        return text, lines, "custom_provider"

    pref = (preferred_backend or "auto").strip().lower()
    if pref not in ("auto", "winrt", "tesseract", "rapidocr"):
        raise ValueError(f"Unsupported OCR backend: {preferred_backend!r}")

    order: list[str]
    if pref == "auto":
        order = ["winrt", "tesseract", "rapidocr"]
    else:
        order = [pref]

    errors: list[str] = []
    for backend in order:
        if backend == "winrt":
            if disable_winrt_probe:
                continue
            try:
                text, lines = recognize_image_winrt(image, source_lang=norm_lang)
                return text, lines, "winrt"
            except Exception as exc:
                errors.append(f"winrt: {exc}")
        elif backend == "tesseract":
            if disable_tesseract_probe:
                continue
            try:
                text, lines = recognize_image_tesseract(image, source_lang=norm_lang)
                return text, lines, "tesseract"
            except Exception as exc:
                errors.append(f"tesseract: {exc}")
        elif backend == "rapidocr":
            if disable_rapidocr_probe:
                continue
            try:
                text, lines = recognize_image_rapidocr(image, source_lang=norm_lang)
                return text, lines, "rapidocr"
            except Exception as exc:
                errors.append(f"rapidocr: {exc}")

    detail = "; ".join(errors) if errors else "all OCR backends disabled or unavailable"
    raise NoOcrBackendError(f"No OCR backend available ({detail})")


def _safe_pdf_text(text: str) -> str:
    """Normalize text for ReportLab Helvetica WinAnsi rendering while preserving readable characters."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text or "")
    try:
        cleaned.encode("cp1252")
        return cleaned
    except UnicodeEncodeError:
        return cleaned.encode("cp1252", errors="replace").decode("cp1252")


def _draw_invisible_ocr_page(
    canv: Any,
    page_w_pt: float,
    page_h_pt: float,
    img: Image.Image,
    img_w_px: int,
    img_h_px: int,
    text: str,
    lines: list[OcrLine],
    *,
    embed_image: bool = True,
) -> None:
    from reportlab.lib.utils import ImageReader

    canv.setPageSize((page_w_pt, page_h_pt))
    if embed_image:
        canv.drawImage(ImageReader(img), 0, 0, width=page_w_pt, height=page_h_pt)

    text_obj = canv.beginText()
    text_obj.setTextRenderMode(3)  # ISO 32000 invisible searchable text

    scale_x = page_w_pt / max(1, img_w_px)
    scale_y = page_h_pt / max(1, img_h_px)

    if lines and any(ln.words for ln in lines):
        for ln in lines:
            if ln.words:
                for wd in ln.words:
                    x_pt = max(0.0, min(page_w_pt - 10.0, wd.x * scale_x))
                    font_pt = max(6.0, min(48.0, wd.height * scale_y if wd.height > 0 else 10.0))
                    y_pt = max(4.0, min(page_h_pt - font_pt, page_h_pt - (wd.y + wd.height) * scale_y))
                    text_obj.setFont("Helvetica", font_pt)
                    text_obj.setTextOrigin(x_pt, y_pt)
                    text_obj.textOut(_safe_pdf_text(wd.text + " "))
            else:
                text_obj.setFont("Helvetica", 10)
                text_obj.textLine(_safe_pdf_text(ln.text))
    else:
        raw_lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if raw_lines:
            font_pt = 10.0
            y_pt = page_h_pt - 36.0
            step = 14.0
            text_obj.setFont("Helvetica", font_pt)
            for ln in raw_lines:
                if y_pt < 24.0:
                    y_pt = page_h_pt - 36.0
                text_obj.setTextOrigin(36.0, y_pt)
                text_obj.textLine(_safe_pdf_text(ln))
                y_pt -= step

    canv.drawText(text_obj)
    canv.showPage()


def ocr_pdf(
    pdf_path: Path,
    output_path: Path,
    *,
    dpi: int = 300,
    quality: int = 85,
    source_lang: str = "auto",
    output_format: str = "pdf",
    ocr_provider: Callable[[Image.Image, str], Any] | None = None,
    disable_winrt_probe: bool = False,
    disable_tesseract_probe: bool = False,
    disable_rapidocr_probe: bool = False,
    preferred_backend: str = "auto",
    progress_cb: Callable[[int, str], Any] | None = None,
) -> OcrDocumentResult:
    """Run OCR on a local PDF and write searchable PDF, plain TXT, or Markdown output."""
    ok, reason = validate_local_pdf_path(pdf_path, check_exists=True)
    if not ok:
        raise ValueError(f"Invalid PDF input: {reason}")
    if is_unc_or_network_path(output_path):
        raise ValueError("network_or_url_blocked")

    dpi_val = int(dpi)
    if dpi_val < 36 or dpi_val > 1200:
        raise ValueError(f"dpi out of range 36..1200: {dpi_val}")
    norm_lang = normalize_source_lang(source_lang)
    fmt = (output_format or "pdf").strip().lower()
    if fmt not in ("pdf", "txt", "md"):
        raise ValueError(f"Unsupported OCR output_format: {output_format!r}")

    import pypdfium2 as pdfium
    from reportlab.pdfgen import canvas

    src_path = Path(pdf_path).resolve()
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    temp_mgr = TempManager(register_atexit=False)
    doc = pdfium.PdfDocument(str(src_path))
    page_results: list[OcrPageResult] = []
    used_backend = "none"

    try:
        total = len(doc)
        if total <= 0:
            raise ValueError("PDF contains zero pages")

        with temp_mgr.task_scope(secure=False) as tid:
            fd, raw_stage = tempfile.mkstemp(
                prefix=f".{out_path.stem}.{os.getpid()}.",
                suffix=f".ocr.tmp{out_path.suffix or '.pdf'}",
                dir=str(out_path.parent),
            )
            os.close(fd)
            stage = Path(raw_stage)
            temp_mgr.register_path(stage, task_id=tid, secure=False)

            canv = canvas.Canvas(str(stage)) if fmt == "pdf" else None
            scale = dpi_val / 72.0
            qual_val = max(10, min(100, int(quality)))

            for idx in range(total):
                page = doc[idx]
                try:
                    w_pt, h_pt = page.get_size()
                    bmp = page.render(scale=scale)
                    pil_img = bmp.to_pil()
                finally:
                    page.close()

                # Optionally normalize quality when re-encoding raster backdrop for PDF
                render_img = pil_img.convert("RGB")
                if fmt == "pdf" and qual_val < 95:
                    q_buf = io.BytesIO()
                    render_img.save(q_buf, format="JPEG", quality=qual_val, optimize=True)
                    q_buf.seek(0)
                    render_img = Image.open(q_buf).convert("RGB")

                text, lines, backend = recognize_image(
                    pil_img,
                    source_lang=norm_lang,
                    ocr_provider=ocr_provider,
                    disable_winrt_probe=disable_winrt_probe,
                    disable_tesseract_probe=disable_tesseract_probe,
                    disable_rapidocr_probe=disable_rapidocr_probe,
                    preferred_backend=preferred_backend,
                )
                used_backend = backend
                page_results.append(
                    OcrPageResult(
                        page_index=idx,
                        text=text,
                        lines=lines,
                        backend=backend,
                        source_lang=norm_lang,
                        dpi=dpi_val,
                    )
                )

                if canv is not None:
                    _draw_invisible_ocr_page(
                        canv,
                        float(w_pt),
                        float(h_pt),
                        render_img,
                        pil_img.width,
                        pil_img.height,
                        text,
                        lines,
                        embed_image=True,
                    )

                if progress_cb is not None:
                    pct = int(((idx + 1) / total) * 95)
                    progress_cb(pct, f"OCR page {idx + 1}/{total}")

            if canv is not None:
                canv.save()
            elif fmt == "txt":
                joined = "\n\n".join(
                    f"--- Page {p.page_index + 1} ---\n{p.text}" if total > 1 else p.text
                    for p in page_results
                ).strip()
                stage.write_text(joined + ("\n" if joined else ""), encoding="utf-8")
            else:
                md_parts: list[str] = []
                for p in page_results:
                    body = p.text.strip()
                    if not body:
                        continue
                    if not body.startswith("#"):
                        md_parts.append(f"## Page {p.page_index + 1}\n\n{body}")
                    else:
                        md_parts.append(body)
                stage.write_text(("\n\n".join(md_parts).strip() + "\n") if md_parts else f"# {src_path.stem}\n", encoding="utf-8")

            os.replace(stage, out_path)
    finally:
        doc.close()
        temp_mgr.cleanup_all()

    if progress_cb is not None:
        progress_cb(100, "OCR completed")

    total_chars = sum(len(p.text) for p in page_results)
    return OcrDocumentResult(
        pages=page_results,
        backend=used_backend,
        total_chars=total_chars,
        source_lang=norm_lang,
        dpi=dpi_val,
    )
