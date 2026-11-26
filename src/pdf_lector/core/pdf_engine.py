"""High-performance PDFium rendering engine with bounded LRU cache and async QThreadPool workers."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
from pathlib import Path
import re
import threading
import uuid

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage


@dataclass(frozen=True)
class TocItem:
    """Normalized Table of Contents entry extracted from a PDF outline."""

    title: str
    page_index: int
    level: int


@dataclass(frozen=True)
class WordBox:
    """Extracted PDF word with unrotated PDF user-space bounding box (x0, y0, x1, y1)."""

    word: str
    rect_pdf: tuple[float, float, float, float]
    char_start: int = 0
    char_end: int = 0
    page_index: int = 0


class RenderSignals(QObject):
    """Qt signals emitted from background PDF render tasks (connect with Qt.QueuedConnection)."""

    rendered = Signal(str, int, float, int, QImage)
    failed = Signal(str, int, str)


class AsyncPageRenderTask(QRunnable):
    """Background worker that renders a single PDF page without freezing the UI thread."""

    def __init__(
        self,
        session: PdfDocumentSession,
        doc_id: str,
        page_index: int,
        scale: float,
        rotation: int,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.session = session
        self.doc_id = doc_id
        self.page_index = page_index
        self.scale = scale
        self.rotation = rotation
        self.signals = RenderSignals()

    def run(self) -> None:
        try:
            img = self.session.render_page(self.page_index, scale=self.scale, rotation=self.rotation)
            self.signals.rendered.emit(self.doc_id, self.page_index, self.scale, self.rotation, img)
        except Exception as exc:
            self.signals.failed.emit(self.doc_id, self.page_index, str(exc))


class PdfDocumentSession:
    """Thread-safe wrapper around a PDFium document with a bounded LRU QImage cache."""

    def __init__(
        self,
        pdf_path: Path,
        *,
        max_cache_entries: int = 32,
        max_cache_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        import pypdfium2 as pdfium

        self.pdf_path = Path(pdf_path)
        self.doc_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._pdf_bytes: bytes = b""
        if self.pdf_path.exists() and self.pdf_path.stat().st_size <= 128 * 1024 * 1024:
            self._pdf_bytes = self.pdf_path.read_bytes()
            self._doc = pdfium.PdfDocument(self._pdf_bytes)
        else:
            self._doc = pdfium.PdfDocument(str(self.pdf_path))
        self._max_entries = max(1, int(max_cache_entries))
        self._max_bytes = max(1024 * 1024, int(max_cache_bytes))
        self._cache: OrderedDict[tuple[int, int, int], QImage] = OrderedDict()
        self._cache_bytes = 0
        self._closed = False

    @property
    def page_count(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            return len(self._doc)

    @property
    def cache_count(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def cache_bytes(self) -> int:
        with self._lock:
            return self._cache_bytes

    def _cache_key(self, page_index: int, scale: float, rotation: int) -> tuple[int, int, int]:
        return (int(page_index), int(round(scale * 1000)), int(rotation % 360))

    def _evict_if_needed(self) -> None:
        while self._cache and (len(self._cache) > self._max_entries or self._cache_bytes > self._max_bytes):
            _, oldest = self._cache.popitem(last=False)
            self._cache_bytes = max(0, self._cache_bytes - oldest.sizeInBytes())

    def get_cached_page(self, page_index: int, *, scale: float = 1.0, rotation: int = 0) -> QImage | None:
        key = self._cache_key(page_index, scale, rotation)
        with self._lock:
            img = self._cache.get(key)
            if img is not None:
                self._cache.move_to_end(key)
            return img

    def render_page(self, page_index: int, *, scale: float = 1.0, rotation: int = 0) -> QImage:
        """Render a PDF page to an owned QImage and store in the bounded LRU cache."""
        safe_scale = max(0.2, min(4.0, float(scale)))
        safe_rot = int(rotation) % 360
        key = self._cache_key(page_index, safe_scale, safe_rot)

        with self._lock:
            if self._closed:
                raise RuntimeError("PdfDocumentSession is closed")
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

            if page_index < 0 or page_index >= len(self._doc):
                raise IndexError(f"Page index {page_index} out of range")

            page = self._doc[page_index]
            bitmap = None
            try:
                bitmap = page.render(scale=safe_scale, rotation=safe_rot)
                pil_img = bitmap.to_pil().convert("RGBA")
                raw = pil_img.tobytes("raw", "RGBA")
                qimg = QImage(raw, pil_img.width, pil_img.height, pil_img.width * 4, QImage.Format.Format_RGBA8888).copy()
            finally:
                if bitmap is not None:
                    bitmap.close()
                page.close()

            self._cache[key] = qimg
            self._cache.move_to_end(key)
            self._cache_bytes += qimg.sizeInBytes()
            self._evict_if_needed()
            return qimg

    def render_thumbnail(self, page_index: int, *, width: int = 120) -> QImage:
        """Render a compact page thumbnail scaled to target pixel width."""
        with self._lock:
            if self._closed:
                raise RuntimeError("PdfDocumentSession is closed")
            page = self._doc[page_index]
            try:
                pw, _ = page.get_size()
            finally:
                page.close()
        scale = max(0.12, min(0.6, float(width) / max(100.0, float(pw))))
        return self.render_page(page_index, scale=scale, rotation=0)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_bytes = 0

    def get_toc(self) -> list[TocItem]:
        """Extract native PDF outline (Table of Contents) via PDFium."""
        with self._lock:
            if self._closed:
                return []
            items: list[TocItem] = []
            try:
                for bm in self._doc.get_toc():
                    title = (bm.get_title() or "").strip()
                    dest = bm.get_dest()
                    page_idx = dest.get_index() if dest is not None else None
                    if title and page_idx is not None and 0 <= page_idx < len(self._doc):
                        items.append(TocItem(title=title, page_index=int(page_idx), level=max(0, int(bm.level))))
            except Exception:
                return []
            return items

    def extract_page_text(self, page_index: int) -> str:
        """Extract plain Unicode text from a single page using PDFium textpage."""
        with self._lock:
            if self._closed or page_index < 0 or page_index >= len(self._doc):
                return ""
            page = self._doc[page_index]
            tp = None
            try:
                tp = page.get_textpage()
                return tp.get_text_range() or ""
            except Exception:
                return ""
            finally:
                if tp is not None:
                    tp.close()
                page.close()

    def extract_page_words(self, page_index: int, *, limit: int = 18) -> list[str]:
        """Return deduplicated notable words on a page for quick Dictionary lookup chips."""
        text = self.extract_page_text(page_index)
        if not text:
            return []
        seen: set[str] = set()
        words: list[str] = []
        for match in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{4,}", text):
            lower = match.lower()
            if lower in seen:
                continue
            seen.add(lower)
            words.append(match)
            if len(words) >= limit:
                break
        return words

    def _require_open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("PdfDocumentSession is closed")

    def get_page_size(self, page_index: int) -> tuple[float, float]:
        """Return unrotated PDF page size (width, height) in PDF points."""
        with self._lock:
            self._require_open()
            if page_index < 0 or page_index >= len(self._doc):
                raise IndexError(f"Page index {page_index} out of range")
            page = self._doc[page_index]
            try:
                w, h = page.get_size()
                return (float(w), float(h))
            finally:
                page.close()

    def get_page_box(self, page_index: int) -> tuple[float, float, float, float]:
        """Return effective page origin and size (origin_x, origin_y, width, height) in PDF points."""
        with self._lock:
            self._require_open()
            if page_index < 0 or page_index >= len(self._doc):
                raise IndexError(f"Page index {page_index} out of range")
            page = self._doc[page_index]
            try:
                w, h = page.get_size()
                ox, oy = 0.0, 0.0
                try:
                    bx0, by0, bx1, by1 = page.get_bbox()
                    if bx1 > bx0 and by1 > by0:
                        ox, oy = float(bx0), float(by0)
                except Exception:
                    ox, oy = 0.0, 0.0
                return (ox, oy, float(w), float(h))
            finally:
                page.close()

    def get_page_word_boxes(self, page_index: int) -> list[WordBox]:
        """Extract contiguous alphanumeric words with PDF user-space bounding boxes."""
        import pypdfium2.raw as pdfium_raw

        with self._lock:
            self._require_open()
            if page_index < 0 or page_index >= len(self._doc):
                return []
            page = self._doc[page_index]
            tp = None
            try:
                tp = page.get_textpage()
                total_chars = int(tp.count_chars())
                if total_chars <= 0:
                    return []

                boxes: list[WordBox] = []
                buf: list[str] = []
                start_idx = -1
                min_l = min_b = math.inf
                max_r = max_t = -math.inf

                def _flush(end_idx: int) -> None:
                    nonlocal start_idx, min_l, min_b, max_r, max_t
                    if buf and math.isfinite(min_l) and max_r >= min_l and max_t >= min_b:
                        boxes.append(
                            WordBox(
                                word="".join(buf),
                                rect_pdf=(float(min_l), float(min_b), float(max_r), float(max_t)),
                                char_start=start_idx,
                                char_end=end_idx,
                                page_index=page_index,
                            )
                        )
                    buf.clear()
                    start_idx = -1
                    min_l = min_b = math.inf
                    max_r = max_t = -math.inf

                for idx in range(total_chars):
                    code = int(pdfium_raw.FPDFText_GetUnicode(tp.raw, idx))
                    ch = chr(code) if code > 0 else ""
                    if ch and ch.isalnum():
                        l, b, r, t = tp.get_charbox(idx)
                        if r <= l or t <= b:
                            l, b, r, t = tp.get_charbox(idx, loose=True)
                        if not buf:
                            start_idx = idx
                        buf.append(ch)
                        min_l = min(min_l, float(l))
                        min_b = min(min_b, float(b))
                        max_r = max(max_r, float(r))
                        max_t = max(max_t, float(t))
                    else:
                        if buf:
                            _flush(idx)
                if buf:
                    _flush(total_chars)
                return boxes
            finally:
                if tp is not None:
                    tp.close()
                page.close()

    def hit_test_word(
        self,
        page_index: int,
        x: float,
        y: float,
        *,
        rotation: int = 0,
        tolerance: float = 2.0,
    ) -> WordBox | None:
        """Hit-test a word at PDF user-space coordinates (x, y)."""
        del rotation  # Coordinates passed here are unrotated PDF user-space points.
        with self._lock:
            self._require_open()
            if page_index < 0 or page_index >= len(self._doc):
                return None

        boxes = self.get_page_word_boxes(page_index)
        if not boxes:
            return None

        tol = max(0.0, float(tolerance))
        exact_matches: list[tuple[float, WordBox]] = []
        tol_matches: list[tuple[float, WordBox]] = []

        for box in boxes:
            x0, y0, x1, y1 = box.rect_pdf
            cx = (x0 + x1) * 0.5
            cy = (y0 + y1) * 0.5
            dist2 = (x - cx) ** 2 + (y - cy) ** 2
            if x0 <= x <= x1 and y0 <= y <= y1:
                exact_matches.append((dist2, box))
            elif (x0 - tol) <= x <= (x1 + tol) and (y0 - tol) <= y <= (y1 + tol):
                tol_matches.append((dist2, box))

        if exact_matches:
            exact_matches.sort(key=lambda item: item[0])
            return exact_matches[0][1]
        if tol_matches:
            tol_matches.sort(key=lambda item: item[0])
            return tol_matches[0][1]
        return None

    def select_text_in_rect(
        self,
        page_index: int,
        rect_pdf: tuple[float, float, float, float],
    ) -> tuple[str, list[tuple[float, float, float, float]]]:
        """Extract selected text and line-merged PDF quads intersecting rect_pdf."""
        import pypdfium2.raw as pdfium_raw

        with self._lock:
            self._require_open()
            if page_index < 0 or page_index >= len(self._doc):
                return ("", [])

            x0, y0, x1, y1 = (float(v) for v in rect_pdf)
            rx0, ry0, rx1, ry1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
            pad = 1.0

            page = self._doc[page_index]
            tp = None
            try:
                tp = page.get_textpage()
                total_chars = int(tp.count_chars())
                if total_chars <= 0:
                    return ("", [])

                chars_out: list[str] = []
                char_boxes: list[tuple[float, float, float, float]] = []
                line_break_pending = False

                for idx in range(total_chars):
                    code = int(pdfium_raw.FPDFText_GetUnicode(tp.raw, idx))
                    ch = chr(code) if code > 0 else ""
                    if not ch:
                        continue
                    if ch in ("\r", "\n"):
                        if chars_out and not line_break_pending:
                            line_break_pending = True
                        continue

                    cl, cb, cr, ct = tp.get_charbox(idx)
                    if cr <= cl or ct <= cb:
                        cl, cb, cr, ct = tp.get_charbox(idx, loose=True)
                    if cr < cl or ct < cb:
                        continue

                    intersects = not (
                        cr < (rx0 - pad)
                        or cl > (rx1 + pad)
                        or ct < (ry0 - pad)
                        or cb > (ry1 + pad)
                    )
                    if not intersects:
                        continue

                    if line_break_pending and chars_out:
                        chars_out.append("\n")
                    line_break_pending = False
                    chars_out.append(ch)
                    if not ch.isspace() and cr > cl and ct > cb:
                        char_boxes.append((float(cl), float(cb), float(cr), float(ct)))

                sel_text = "".join(chars_out).strip()
                if not sel_text and char_boxes:
                    try:
                        sel_text = (tp.get_text_bounded(rx0, ry0, rx1, ry1) or "").strip()
                    except Exception:
                        sel_text = ""

                # Merge charboxes by visual line
                line_quads: list[tuple[float, float, float, float]] = []
                for cl, cb, cr, ct in char_boxes:
                    if not line_quads:
                        line_quads.append((cl, cb, cr, ct))
                        continue
                    pl, pb, pr, pt = line_quads[-1]
                    prev_cy = (pb + pt) * 0.5
                    cur_cy = (cb + ct) * 0.5
                    max_h = max(1.0, pt - pb, ct - cb)
                    if abs(cur_cy - prev_cy) <= 0.6 * max_h:
                        line_quads[-1] = (min(pl, cl), min(pb, cb), max(pr, cr), max(pt, ct))
                    else:
                        line_quads.append((cl, cb, cr, ct))

                return (sel_text, line_quads)
            finally:
                if tp is not None:
                    tp.close()
                page.close()

    def pdf_to_screen_point(
        self,
        page_index: int,
        x_pdf: float,
        y_pdf: float,
        *,
        scale: float = 1.0,
        rotation: int = 0,
    ) -> tuple[float, float]:
        """Map PDF user-space point to widget screen pixel coordinates under CW rotation."""
        ox, oy, w, h = self.get_page_box(page_index)
        u = float(x_pdf) - ox
        v = float(y_pdf) - oy
        s = max(0.05, float(scale))
        rot = int(rotation) % 360
        if rot == 90:
            return (v * s, u * s)
        if rot == 180:
            return ((w - u) * s, v * s)
        if rot == 270:
            return ((h - v) * s, (w - u) * s)
        return (u * s, (h - v) * s)

    def screen_to_pdf_point(
        self,
        page_index: int,
        sx: float,
        sy: float,
        *,
        scale: float = 1.0,
        rotation: int = 0,
    ) -> tuple[float, float]:
        """Map widget screen pixel coordinates to unrotated PDF user-space coordinates."""
        ox, oy, w, h = self.get_page_box(page_index)
        s = max(0.05, float(scale))
        rot = int(rotation) % 360
        if rot == 90:
            u, v = sy / s, sx / s
        elif rot == 180:
            u, v = w - sx / s, sy / s
        elif rot == 270:
            u, v = w - sy / s, h - sx / s
        else:
            u, v = sx / s, h - sy / s
        return (u + ox, v + oy)

    def pdf_to_screen_rect(
        self,
        page_index: int,
        rect_pdf: tuple[float, float, float, float],
        *,
        scale: float = 1.0,
        rotation: int = 0,
    ) -> tuple[float, float, float, float]:
        """Map a PDF user-space rectangle to normalized screen pixel coordinates."""
        x0, y0, x1, y1 = rect_pdf
        sx0, sy0 = self.pdf_to_screen_point(page_index, x0, y0, scale=scale, rotation=rotation)
        sx1, sy1 = self.pdf_to_screen_point(page_index, x1, y1, scale=scale, rotation=rotation)
        return (min(sx0, sx1), min(sy0, sy1), max(sx0, sx1), max(sy0, sy1))

    def screen_to_pdf_rect(
        self,
        page_index: int,
        rect_screen: tuple[float, float, float, float],
        *,
        scale: float = 1.0,
        rotation: int = 0,
    ) -> tuple[float, float, float, float]:
        """Map a screen pixel rectangle to normalized PDF user-space coordinates."""
        sx0, sy0, sx1, sy1 = rect_screen
        px0, py0 = self.screen_to_pdf_point(page_index, sx0, sy0, scale=scale, rotation=rotation)
        px1, py1 = self.screen_to_pdf_point(page_index, sx1, sy1, scale=scale, rotation=rotation)
        return (min(px0, px1), min(py0, py1), max(px0, px1), max(py0, py1))

    def hit_test_word_screen(
        self,
        page_index: int,
        sx: float,
        sy: float,
        *,
        scale: float = 1.0,
        rotation: int = 0,
        tolerance_px: float = 3.0,
    ) -> WordBox | None:
        """Hit-test a word from screen pixel coordinates under current zoom and CW rotation."""
        x_pdf, y_pdf = self.screen_to_pdf_point(page_index, sx, sy, scale=scale, rotation=rotation)
        tol_pdf = max(0.5, float(tolerance_px) / max(0.05, float(scale)))
        return self.hit_test_word(page_index, x_pdf, y_pdf, tolerance=tol_pdf)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
            self._cache_bytes = 0
            try:
                self._doc.close()
            except Exception:
                pass
            self._pdf_bytes = b""
            self._pdf_bytes = b""


def generate_sample_pdf(output_path: Path) -> Path:
    """Create a multi-page TRODAI specimen PDF using ReportLab for immediate live testing."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(output_path), pagesize=LETTER)
    w, h = LETTER

    navy = colors.HexColor("#0A0F1D")
    cyan = colors.HexColor("#0EA5E9")
    crimson = colors.HexColor("#DC2626")
    slate = colors.HexColor("#475569")
    hairline = colors.HexColor("#CBD5E1")

    # Page 1: Architecture Cover
    c.setFillColor(navy)
    c.rect(0, h - 135, w, 135, stroke=0, fill=1)
    c.setFillColor(cyan)
    c.rect(0, h - 139, w, 4, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(54, h - 68, "PDF Lector by TRODAI")
    c.setFont("Helvetica", 11)
    c.drawString(54, h - 92, "Documento de Prueba Arquitectonico · Motor PDFium + Theme Studio")
    c.setFillColor(crimson)
    c.rect(54, h - 114, 72, 3, stroke=0, fill=1)

    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(54, h - 190, "1. Lectura Nitida con Fondos Personalizables")
    c.setFillColor(slate)
    c.setFont("Helvetica", 11)
    lines = [
        "Este documento multipagina permite validar en tiempo real el rendimiento de PDFium,",
        "la nitidez tipografica sobre fondos personalizados, el control de zoom y la barra",
        "de miniaturas lateral sin afectar el contenido de la hoja.",
        "",
        "Caracteristicas verificadas en este prototipo:",
        "  - Pantalla de carga temporal tipo Adobe con logotipo de la app y sello by TRODAI.",
        "  - Hub modular con las 8 categorias de herramientas PDF e iconos 100% poligonales.",
        "  - Theme Studio en vivo: fotos locales (PNG/JPG/WebP), GIF/WebP animado y Motion.",
        "  - Velo automatico (Auto-Scrim) y guardia de bateria/rendimiento en Windows 11.",
    ]
    y = h - 220
    for line in lines:
        c.drawString(54, y, line)
        y -= 18

    c.setStrokeColor(hairline)
    c.rect(54, 90, w - 108, 120, stroke=1, fill=0)
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 185, "Garantias de Privacidad y Seguridad Local")
    c.setFillColor(slate)
    c.setFont("Helvetica", 10)
    c.drawString(72, 162, "Procesamiento 100% local · Bloqueo de rutas UNC y URLs externas en temas")
    c.drawString(72, 145, "Cero telemetria oculta · Cache LRU acotada de paginas para bajo uso de RAM")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 54, 42, "TRODAI · Pagina 1 de 4")
    c.bookmarkPage("toc_p1")
    c.addOutlineEntry("1. Lectura nitida y privacidad local", "toc_p1", level=0, closed=False)
    c.showPage()

    # Page 2: Technical Summary Table
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(54, h - 72, "2. Matriz Modular de Rendimiento y UX")
    c.setStrokeColor(cyan)
    c.line(54, h - 82, w - 54, h - 82)

    rows = [
        ("Componente", "Tecnologia / Estrategia", "Objetivo"),
        ("Renderizado PDF", "pypdfium2 + Cache LRU (64 MB)", "Scroll y zoom fluidos"),
        ("Arranque", "Splash visual + carga diferida", "Primer fotograma < 300 ms"),
        ("Temas & Motion", "QPainter 60 FPS + QMovie + Pillow", "Banners propios con Auto-Scrim"),
        ("Seguridad", "PathGuard local + Win32 Chrome", "Sin fugas UNC ni telemetria"),
    ]
    y = h - 130
    for idx, (col1, col2, col3) in enumerate(rows):
        if idx == 0:
            c.setFillColor(navy)
            c.rect(54, y - 8, w - 108, 26, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 10)
        else:
            c.setStrokeColor(hairline)
            c.rect(54, y - 8, w - 108, 26, stroke=1, fill=0)
            c.setFillColor(navy)
            c.setFont("Helvetica", 10)
        c.drawString(64, y, col1)
        c.drawString(195, y, col2)
        c.drawString(410, y, col3)
        y -= 30

    c.setFillColor(slate)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 54, 42, "TRODAI · Pagina 2 de 4")
    c.bookmarkPage("toc_p2")
    c.addOutlineEntry("2. Matriz modular de rendimiento", "toc_p2", level=0, closed=False)
    c.showPage()

    # Page 3: Sample Invoice Layout (preview for Facturas module)
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(54, h - 72, "3. Ejemplo Visual: Factura Electronica Estructurada")
    c.setStrokeColor(hairline)
    c.rect(54, h - 320, w - 108, 220, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, h - 125, "EMISOR: TRODAI ANALITICA & GOBERNANZA S.A.S.")
    c.setFont("Helvetica", 10)
    c.setFillColor(slate)
    c.drawString(72, h - 145, "Perfil compatible: Colombia DIAN UBL 2.1 / Internacional Factur-X EN 16931")
    c.drawString(72, h - 170, "Concepto: Licencia Empresarial Soporte PDF Lector + Modulo MCP")
    c.drawString(72, h - 190, "Subtotal: $ 2,400,000   |   IVA (19%): $ 456,000   |   Total: $ 2,856,000")
    c.setFillColor(navy)
    c.rect(w - 155, h - 295, 75, 75, stroke=1, fill=0)
    c.setFont("Helvetica", 8)
    c.drawCentredString(w - 117, h - 260, "QR / CUFE")
    c.setFillColor(slate)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 54, 42, "TRODAI · Pagina 3 de 4")
    c.bookmarkPage("toc_p3")
    c.addOutlineEntry("3. Factura electronica estructurada", "toc_p3", level=0, closed=False)
    c.addOutlineEntry("3.1 Perfil DIAN UBL 2.1 / Factur-X", "toc_p3", level=1, closed=False)
    c.showPage()

    # Page 4: Notes & Contrast Test
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(54, h - 72, "4. Prueba de Contraste, Rotacion y Zoom")
    c.setFillColor(slate)
    c.setFont("Helvetica", 11)
    c.drawString(54, h - 110, "Usa los controles del Visor para girar esta pagina, acercar o alternar el fondo exterior.")
    c.setStrokeColor(crimson)
    c.setLineWidth(2)
    c.rect(54, h - 260, 220, 110, stroke=1, fill=0)
    c.setStrokeColor(cyan)
    c.rect(310, h - 260, 220, 110, stroke=1, fill=0)
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(74, h - 195, "Acento PDF Lector (#DC2626)")
    c.drawString(330, h - 195, "Acento TRODAI (#0EA5E9)")
    c.setFillColor(slate)
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 54, 42, "TRODAI · Pagina 4 de 4")
    c.bookmarkPage("toc_p4")
    c.addOutlineEntry("4. Contraste, rotacion y diccionario", "toc_p4", level=0, closed=False)
    c.showPage()

    c.save()
    return output_path
