"""Convert and OCR PDF tools (5 tools): convert_to_pdf, convert_from_pdf, images_to_pdf, pdf_to_images, pdf_ocr."""

from __future__ import annotations

from html import escape as html_escape
from html.parser import HTMLParser
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from xml.sax.saxutils import escape as rml_escape

from ..ocr_engine import clean_child_env, ocr_pdf
from ..path_guard import is_unc_or_network_path
from ..win32_bridge import atomic_write_text, is_cloud_only_file
from . import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    atomic_replace_file,
    dpi_param_spec,
    emit_progress,
    get_shared_temp_manager,
    make_sibling_stage_path,
    quality_param_spec,
    source_lang_param_spec,
    validate_output_dir_path,
    validate_output_pdf_path,
)

_CONVERT_TO_PDF_EXTENSIONS = {".md", ".markdown", ".docx", ".xlsx", ".html", ".htm", ".txt"}


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def probe_edge_executable() -> Path | None:
    """Locate local Microsoft Edge executable for optional HTML-to-PDF rendering."""
    candidates: list[Path] = []
    which_hit = shutil.which("msedge")
    if which_hit:
        candidates.append(Path(which_hit))
    for base in (
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for cand in candidates:
        try:
            if not is_unc_or_network_path(cand) and cand.exists() and cand.is_file() and not is_cloud_only_file(cand):
                return cand
        except OSError:
            continue
    return None


def probe_office_executable() -> dict[str, Path | None]:
    """Probe optional local LibreOffice (soffice) or MS Office executables."""
    soffice_hit = shutil.which("soffice") or shutil.which("libreoffice")
    soffice: Path | None = Path(soffice_hit) if soffice_hit else None
    if soffice is None:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cand = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if cand.exists() and cand.is_file():
                soffice = cand
                break
    return {"soffice": soffice, "edge": probe_edge_executable()}


def _build_toc_pdf_from_flowables(
    flowables: list[Any],
    stage_pdf: Path,
    *,
    title: str,
    page_size_name: str = "Letter",
    landscape_mode: bool = False,
) -> None:
    from reportlab.lib.pagesizes import A4, LEGAL, LETTER, landscape
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Spacer

    size_map = {"LETTER": LETTER, "A4": A4, "LEGAL": LEGAL}
    pg_size = size_map.get(page_size_name.upper(), LETTER)
    if landscape_mode:
        pg_size = landscape(pg_size)

    if not flowables:
        flowables = [Spacer(1, 12)]

    class _BookmarkDoc(BaseDocTemplate):
        def afterFlowable(self, flowable: object) -> None:
            key = getattr(flowable, "_toc_key", None)
            bm_title = getattr(flowable, "_toc_title", None)
            lvl = getattr(flowable, "_toc_level", None)
            if key and bm_title is not None and lvl is not None:
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(str(bm_title), str(key), level=int(lvl), closed=False)

    doc = _BookmarkDoc(
        str(stage_pdf),
        pagesize=pg_size,
        leftMargin=48,
        rightMargin=48,
        topMargin=48,
        bottomMargin=48,
        title=title,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])
    doc.build(flowables)


def _docx_to_pdf(in_path: Path, stage_pdf: Path, *, page_size_name: str = "Letter") -> int:
    import docx
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    document = docx.Document(str(in_path))
    styles = getSampleStyleSheet()
    h1_style = ParagraphStyle("DocxH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, spaceAfter=8)
    h2_style = ParagraphStyle("DocxH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, spaceAfter=6)
    h3_style = ParagraphStyle("DocxH3", parent=styles["Heading3"], fontName="Helvetica-Bold", fontSize=11, spaceAfter=4)
    body_style = ParagraphStyle("DocxBody", parent=styles["Normal"], fontName="Helvetica", fontSize=10, leading=14, spaceAfter=6)

    flowables: list[Any] = []
    bm_count = 0

    for para in document.paragraphs:
        txt = (para.text or "").strip()
        if not txt:
            flowables.append(Spacer(1, 4))
            continue
        style_name = (para.style.name or "").lower() if para.style else ""
        safe_txt = rml_escape(txt)
        if style_name.startswith("heading 1") or style_name == "title":
            p = Paragraph(safe_txt, h1_style)
            bm_count += 1
            p._toc_key = f"docx_bm_{bm_count}"
            p._toc_title = txt[:120]
            p._toc_level = 0
            flowables.append(p)
        elif style_name.startswith("heading 2"):
            p = Paragraph(safe_txt, h2_style)
            bm_count += 1
            p._toc_key = f"docx_bm_{bm_count}"
            p._toc_title = txt[:120]
            p._toc_level = 1
            flowables.append(p)
        elif style_name.startswith("heading"):
            p = Paragraph(safe_txt, h3_style)
            bm_count += 1
            p._toc_key = f"docx_bm_{bm_count}"
            p._toc_title = txt[:120]
            p._toc_level = 2
            flowables.append(p)
        else:
            flowables.append(Paragraph(safe_txt, body_style))

    for tbl in document.tables:
        rows_data: list[list[Paragraph]] = []
        for row in tbl.rows:
            rows_data.append([Paragraph(rml_escape((cell.text or "").strip()), body_style) for cell in row.cells])
        if rows_data:
            col_cnt = max(len(r) for r in rows_data)
            for r in rows_data:
                while len(r) < col_cnt:
                    r.append(Paragraph("", body_style))
            col_w = 500.0 / max(1, col_cnt)
            rtbl = Table(rows_data, colWidths=[col_w] * col_cnt)
            rtbl.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            flowables.append(rtbl)
            flowables.append(Spacer(1, 8))

    _build_toc_pdf_from_flowables(flowables, stage_pdf, title=in_path.stem, page_size_name=page_size_name)
    return len(document.paragraphs)


def _xlsx_to_pdf(in_path: Path, stage_pdf: Path, *, page_size_name: str = "Letter") -> int:
    import openpyxl
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, Spacer, Table, TableStyle

    wb = openpyxl.load_workbook(str(in_path), data_only=True, read_only=True)
    styles = getSampleStyleSheet()
    hdr_style = ParagraphStyle("XlsxHdr", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, spaceAfter=8)
    cell_style = ParagraphStyle("XlsxCell", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=11)

    flowables: list[Any] = []
    max_cols = 1
    sheet_count = 0

    try:
        for s_idx, ws in enumerate(wb.worksheets):
            sheet_count += 1
            if s_idx > 0:
                flowables.append(PageBreak())
            title_p = Paragraph(rml_escape(ws.title), hdr_style)
            title_p._toc_key = f"xlsx_sheet_{s_idx + 1}"
            title_p._toc_title = ws.title[:120]
            title_p._toc_level = 0
            flowables.append(title_p)

            rows_data: list[list[Paragraph]] = []
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                if any(v != "" for v in vals):
                    rows_data.append([Paragraph(rml_escape(v), cell_style) for v in vals])
            if rows_data:
                cols = max(len(r) for r in rows_data)
                max_cols = max(max_cols, cols)
                for r in rows_data:
                    while len(r) < cols:
                        r.append(Paragraph("", cell_style))
                avail_w = 680.0 if cols > 6 else 500.0
                col_w = avail_w / max(1, cols)
                rtbl = Table(rows_data, colWidths=[col_w] * cols, repeatRows=1)
                rtbl.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2E8F0")),
                            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                flowables.append(rtbl)
                flowables.append(Spacer(1, 8))
    finally:
        wb.close()

    _build_toc_pdf_from_flowables(
        flowables,
        stage_pdf,
        title=in_path.stem,
        page_size_name=page_size_name,
        landscape_mode=max_cols > 6,
    )
    return sheet_count


class _SimpleHtmlExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[tuple[str, str]] = []
        self._tag_stack: list[str] = []
        self._buf: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        t = tag.lower()
        if t in ("script", "style", "head"):
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return
        if t in ("h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote"):
            self._flush()
            self._tag_stack.append(t)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("script", "style", "head"):
            self._ignore_depth = max(0, self._ignore_depth - 1)
            return
        if self._ignore_depth > 0:
            return
        if t in ("h1", "h2", "h3", "h4", "p", "li", "pre", "blockquote"):
            self._flush(t)
            if self._tag_stack and self._tag_stack[-1] == t:
                self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        self._buf.append(data)

    def _flush(self, kind: str | None = None) -> None:
        text = " ".join("".join(self._buf).split()).strip()
        self._buf.clear()
        if text:
            blk_kind = kind or (self._tag_stack[-1] if self._tag_stack else "p")
            self.blocks.append((blk_kind, text))


def _html_to_pdf(
    in_path: Path,
    stage_pdf: Path,
    *,
    page_size_name: str = "Letter",
    use_edge_headless: bool = False,
) -> str:
    if use_edge_headless:
        edge_exe = probe_edge_executable()
        if edge_exe is not None:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                proc = subprocess.run(
                    [
                        str(edge_exe),
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        f"--print-to-pdf={stage_pdf}",
                        in_path.as_uri(),
                    ],
                    capture_output=True,
                    env=clean_child_env(),
                    creationflags=flags,
                    timeout=20,
                )
                if proc.returncode == 0 and stage_pdf.exists() and stage_pdf.stat().st_size > 100:
                    return "edge_headless"
            except Exception:
                pass

    raw_html = in_path.read_text(encoding="utf-8", errors="replace")
    try:
        import nh3

        clean_html = nh3.clean(raw_html)
    except Exception:
        clean_html = raw_html

    parser = _SimpleHtmlExtractor()
    parser.feed(clean_html)
    parser._flush()

    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Preformatted, Spacer

    styles = getSampleStyleSheet()
    h1_style = ParagraphStyle("HtmlH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, spaceAfter=8)
    h2_style = ParagraphStyle("HtmlH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, spaceAfter=6)
    body_style = ParagraphStyle("HtmlBody", parent=styles["Normal"], fontName="Helvetica", fontSize=10.5, leading=14, spaceAfter=6)
    pre_style = ParagraphStyle("HtmlPre", parent=styles["Code"], fontName="Courier", fontSize=9, leading=12)

    flowables: list[Any] = []
    for idx, (kind, text) in enumerate(parser.blocks, start=1):
        safe = rml_escape(text)
        if kind == "h1":
            p = Paragraph(safe, h1_style)
            p._toc_key = f"html_bm_{idx}"
            p._toc_title = text[:120]
            p._toc_level = 0
            flowables.append(p)
        elif kind in ("h2", "h3", "h4"):
            p = Paragraph(safe, h2_style)
            p._toc_key = f"html_bm_{idx}"
            p._toc_title = text[:120]
            p._toc_level = 1
            flowables.append(p)
        elif kind == "li":
            flowables.append(Paragraph(f"- {safe}", body_style))
        elif kind == "pre":
            flowables.append(Preformatted(text, pre_style))
        else:
            flowables.append(Paragraph(safe, body_style))

    if not flowables:
        flowables.append(Spacer(1, 12))

    _build_toc_pdf_from_flowables(flowables, stage_pdf, title=in_path.stem, page_size_name=page_size_name)
    return "local_html_reportlab"


def _txt_to_pdf(in_path: Path, stage_pdf: Path, *, page_size_name: str = "Letter") -> int:
    try:
        raw_txt = in_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw_txt = in_path.read_text(encoding="cp1252", errors="replace")

    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Spacer

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("TxtBody", parent=styles["Normal"], fontName="Helvetica", fontSize=10, leading=14, spaceAfter=4)

    lines = raw_txt.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    flowables: list[Any] = []
    for ln in lines:
        if ln.strip():
            flowables.append(Paragraph(rml_escape(ln), body_style))
        else:
            flowables.append(Spacer(1, 6))

    _build_toc_pdf_from_flowables(flowables, stage_pdf, title=in_path.stem, page_size_name=page_size_name)
    return len(lines)


def _handle_convert_to_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("convert_to_pdf", err_out, "invalid_output")

    in_path = inputs[0]
    ext = in_path.suffix.lower()
    if ext not in _CONVERT_TO_PDF_EXTENSIONS:
        return _error_result("convert_to_pdf", f"Unsupported input extension: {ext}", "unsupported_extension")

    dpi = int(params["dpi"])
    quality = int(params["quality"])
    source_lang = str(params["source_lang"])
    theme = str(params.get("theme") or "light")
    page_size = str(params.get("page_size") or "Letter")
    use_edge = bool(params.get("use_edge_headless", False))

    emit_progress(progress_cb, 15, f"Converting {ext} to PDF")
    temp_mgr = get_shared_temp_manager()
    backend_used = ext.lstrip(".")

    try:
        with temp_mgr.task_scope(secure=False) as tid:
            stage = make_sibling_stage_path(out_path, suffix=".pdf")
            temp_mgr.register_path(stage, task_id=tid, secure=False)

            if ext in (".md", ".markdown"):
                from ..markdown_engine import markdown_to_pdf

                markdown_to_pdf(in_path, stage, dpi=dpi, source_lang=source_lang, theme=theme)
                backend_used = "markdown_engine"
            elif ext == ".docx":
                _docx_to_pdf(in_path, stage, page_size_name=page_size)
                backend_used = "python_docx"
            elif ext == ".xlsx":
                _xlsx_to_pdf(in_path, stage, page_size_name=page_size)
                backend_used = "openpyxl"
            elif ext in (".html", ".htm"):
                backend_used = _html_to_pdf(in_path, stage, page_size_name=page_size, use_edge_headless=use_edge)
            else:
                _txt_to_pdf(in_path, stage, page_size_name=page_size)
                backend_used = "plain_text"

            atomic_replace_file(stage, out_path)
    except Exception as exc:
        return _error_result("convert_to_pdf", f"Conversion failed: {exc}", "conversion_failed")

    emit_progress(progress_cb, 100, "Converted to PDF")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Converted {in_path.name} -> {out_path.name}",
        details={
            "source_format": ext.lstrip("."),
            "backend": backend_used,
            "dpi": dpi,
            "quality": quality,
            "source_lang": source_lang,
        },
    )


def _extract_pdf_pages_text(pdf_path: Path) -> list[str]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    pages_text: list[str] = []
    try:
        for idx in range(len(doc)):
            page = doc[idx]
            tp = None
            try:
                tp = page.get_textpage()
                pages_text.append((tp.get_text_range() or "").strip())
            finally:
                if tp is not None:
                    tp.close()
                page.close()
    finally:
        doc.close()
    return pages_text


def _handle_convert_from_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    in_path = inputs[0]
    fmt = str(params["target_format"]).strip().lower()
    dpi = int(params["dpi"])
    source_lang = str(params["source_lang"])

    out_path = Path(output)
    if out_path.exists() and out_path.is_dir():
        return _error_result("convert_from_pdf", "Output path is a directory", "output_is_directory")

    emit_progress(progress_cb, 15, f"Exporting PDF to {fmt}")
    temp_mgr = get_shared_temp_manager()
    pages_count = 0

    try:
        if fmt == "md":
            from ..markdown_engine import pdf_to_markdown

            pdf_to_markdown(in_path, out_path, dpi=dpi, source_lang=source_lang)
            pages_count = len(_extract_pdf_pages_text(in_path))
        elif fmt == "txt":
            pages = _extract_pdf_pages_text(in_path)
            pages_count = len(pages)
            body = "\n\n".join(
                f"=== Page {i + 1} ===\n{txt}" if len(pages) > 1 else txt for i, txt in enumerate(pages)
            ).strip()
            atomic_write_text(out_path, body + ("\n" if body else ""))
        elif fmt == "html":
            pages = _extract_pdf_pages_text(in_path)
            pages_count = len(pages)
            sections = []
            for i, txt in enumerate(pages, start=1):
                paras = "".join(f"<p>{html_escape(line)}</p>" for line in txt.splitlines() if line.strip())
                sections.append(f"<section data-page='{i}'><h2>Page {i}</h2>{paras or '<p></p>'}</section>")
            html_doc = (
                "<!doctype html>\n<html lang='en'>\n<head><meta charset='utf-8'>"
                f"<title>{html_escape(in_path.stem)}</title></head>\n<body>\n"
                + "\n".join(sections)
                + "\n</body>\n</html>\n"
            )
            atomic_write_text(out_path, html_doc)
        elif fmt == "docx":
            import docx

            pages = _extract_pdf_pages_text(in_path)
            pages_count = len(pages)
            doc_obj = docx.Document()
            doc_obj.add_heading(in_path.stem, level=1)
            for i, txt in enumerate(pages, start=1):
                doc_obj.add_heading(f"Page {i}", level=2)
                for line in txt.splitlines():
                    if line.strip():
                        doc_obj.add_paragraph(line.strip())
            with temp_mgr.task_scope(secure=False) as tid:
                stage = make_sibling_stage_path(out_path, suffix=".docx")
                temp_mgr.register_path(stage, task_id=tid, secure=False)
                doc_obj.save(str(stage))
                atomic_replace_file(stage, out_path)
        elif fmt == "xlsx":
            import openpyxl
            import pdfplumber

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Extracted"
            has_rows = False
            with pdfplumber.open(str(in_path)) as pdf_doc:
                pages_count = len(pdf_doc.pages)
                for p_idx, page in enumerate(pdf_doc.pages, start=1):
                    tables = page.extract_tables() or []
                    if tables:
                        for t_idx, tbl in enumerate(tables, start=1):
                            sheet = ws if not has_rows else wb.create_sheet(f"P{p_idx}_T{t_idx}")
                            for row in tbl:
                                sheet.append([("" if c is None else str(c)) for c in row])
                            has_rows = True
                    else:
                        txt = (page.extract_text() or "").strip()
                        for line in txt.splitlines():
                            if line.strip():
                                ws.append([f"Page {p_idx}", line.strip()])
                                has_rows = True
            if not has_rows:
                ws.append(["Page", "Text"])
            with temp_mgr.task_scope(secure=False) as tid:
                stage = make_sibling_stage_path(out_path, suffix=".xlsx")
                temp_mgr.register_path(stage, task_id=tid, secure=False)
                wb.save(str(stage))
                wb.close()
                atomic_replace_file(stage, out_path)
        else:
            return _error_result("convert_from_pdf", f"Unsupported target_format: {fmt}", "unsupported_format")
    except Exception as exc:
        return _error_result("convert_from_pdf", f"Export failed: {exc}", "conversion_failed")

    emit_progress(progress_cb, 100, f"Exported to {fmt}")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Exported {in_path.name} -> {out_path.name} ({fmt})",
        details={
            "target_format": fmt,
            "dpi": dpi,
            "source_lang": source_lang,
            "pages": pages_count,
        },
    )


def _handle_images_to_pdf(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("images_to_pdf", err_out, "invalid_output")

    dpi = int(params["dpi"])
    quality = int(params["quality"])
    page_size = str(params.get("page_size") or "fit")

    from PIL import Image
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    emit_progress(progress_cb, 10, f"Loading {len(inputs)} image(s)")
    loaded_images: list[Image.Image] = []
    try:
        for img_path in inputs:
            with Image.open(img_path) as opened:
                opened.load()
                if opened.mode in ("RGBA", "LA") or (opened.mode == "P" and "transparency" in opened.info):
                    rgba = opened.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[-1])
                    loaded_images.append(bg)
                else:
                    loaded_images.append(opened.convert("RGB"))
    except Exception as exc:
        return _error_result("images_to_pdf", f"Corrupt or unreadable image: {exc}", "corrupt_image")

    temp_mgr = get_shared_temp_manager()
    try:
        with temp_mgr.task_scope(secure=False) as tid:
            stage = make_sibling_stage_path(out_path, suffix=".pdf")
            temp_mgr.register_path(stage, task_id=tid, secure=False)
            canv = canvas.Canvas(str(stage))
            total = len(loaded_images)

            for idx, pil_img in enumerate(loaded_images):
                import io

                if quality < 95:
                    buf = io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=quality, optimize=True)
                    buf.seek(0)
                    reader = ImageReader(buf)
                else:
                    reader = ImageReader(pil_img)

                img_w_pt = max(1.0, pil_img.width * 72.0 / dpi)
                img_h_pt = max(1.0, pil_img.height * 72.0 / dpi)

                if page_size.lower() == "letter":
                    pw, ph = LETTER
                    scale = min(pw / img_w_pt, ph / img_h_pt)
                    dw, dh = img_w_pt * scale, img_h_pt * scale
                    canv.setPageSize((pw, ph))
                    canv.drawImage(reader, (pw - dw) / 2.0, (ph - dh) / 2.0, width=dw, height=dh)
                elif page_size.lower() == "a4":
                    pw, ph = A4
                    scale = min(pw / img_w_pt, ph / img_h_pt)
                    dw, dh = img_w_pt * scale, img_h_pt * scale
                    canv.setPageSize((pw, ph))
                    canv.drawImage(reader, (pw - dw) / 2.0, (ph - dh) / 2.0, width=dw, height=dh)
                else:
                    canv.setPageSize((img_w_pt, img_h_pt))
                    canv.drawImage(reader, 0, 0, width=img_w_pt, height=img_h_pt)

                canv.showPage()
                emit_progress(progress_cb, 15 + int(((idx + 1) / total) * 80), f"Embedded image {idx + 1}/{total}")

            canv.save()
            atomic_replace_file(stage, out_path)
    except Exception as exc:
        return _error_result("images_to_pdf", f"Failed building PDF: {exc}", "pdf_write_failed")

    emit_progress(progress_cb, 100, "Compiled images to PDF")
    return ToolResult(
        success=True,
        output_paths=[out_path],
        summary=f"Compiled {len(inputs)} image(s) into {out_path.name}",
        details={"dpi": dpi, "quality": quality, "page_count": len(inputs)},
    )


def _handle_pdf_to_images(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    ok_dir, out_dir, err_dir = validate_output_dir_path(output)
    if not ok_dir or out_dir is None:
        return _error_result("pdf_to_images", err_dir, "invalid_output_dir")

    in_path = inputs[0]
    dpi = int(params["dpi"])
    quality = int(params["quality"])
    fmt = str(params.get("format") or "png").strip().lower()
    if fmt == "jpg":
        fmt_pil = "JPEG"
        ext = "jpg"
    elif fmt == "jpeg":
        fmt_pil = "JPEG"
        ext = "jpeg"
    elif fmt == "tiff":
        fmt_pil = "TIFF"
        ext = "tiff"
    elif fmt == "webp":
        fmt_pil = "WEBP"
        ext = "webp"
    else:
        fmt_pil = "PNG"
        ext = "png"

    import pypdfium2 as pdfium

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(in_path))
    outputs: list[Path] = []
    temp_mgr = get_shared_temp_manager()

    try:
        total = len(doc)
        if total <= 0:
            return _error_result("pdf_to_images", "PDF has zero pages", "empty_pdf")

        scale = dpi / 72.0
        with temp_mgr.task_scope(secure=False) as tid:
            for idx in range(total):
                page = doc[idx]
                try:
                    bmp = page.render(scale=scale)
                    pil_img = bmp.to_pil()
                finally:
                    page.close()

                target = out_dir / f"{in_path.stem}_page_{idx + 1:04d}.{ext}"
                stage = make_sibling_stage_path(target, suffix=f".{ext}")
                temp_mgr.register_path(stage, task_id=tid, secure=False)

                save_kwargs: dict[str, Any] = {}
                if fmt_pil in ("JPEG", "WEBP"):
                    pil_img = pil_img.convert("RGB")
                    save_kwargs["quality"] = quality
                elif fmt_pil == "PNG":
                    save_kwargs["dpi"] = (dpi, dpi)

                pil_img.save(stage, format=fmt_pil, **save_kwargs)
                atomic_replace_file(stage, target)
                outputs.append(target)
                emit_progress(progress_cb, int(((idx + 1) / total) * 100), f"Rendered page {idx + 1}/{total}")
    except Exception as exc:
        return _error_result("pdf_to_images", f"Render failed: {exc}", "render_failed")
    finally:
        doc.close()

    return ToolResult(
        success=True,
        output_paths=outputs,
        summary=f"Rendered {len(outputs)} page(s) at {dpi} DPI",
        details={"dpi": dpi, "quality": quality, "format": fmt, "page_count": len(outputs)},
    )


def _handle_pdf_ocr(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    in_path = inputs[0]
    dpi = int(params["dpi"])
    quality = int(params["quality"])
    source_lang = str(params["source_lang"])
    output_format = str(params.get("output_format") or "pdf").strip().lower()
    if Path(output).suffix.lower() in (".txt", ".md") and "output_format" not in params:
        output_format = Path(output).suffix.lower().lstrip(".")

    ocr_provider = params.get("ocr_provider")
    disable_winrt_probe = bool(params.get("disable_winrt_probe", False))
    disable_tesseract_probe = bool(params.get("disable_tesseract_probe", False))
    disable_rapidocr_probe = bool(params.get("disable_rapidocr_probe", False))
    preferred_backend = str(params.get("backend") or "auto")

    # If caller explicitly supplied ocr_provider=None and disabled WinRT without requesting Tesseract/RapidOCR,
    # treat as simulated "no backend available" unless caller explicitly enabled another probe.
    if (
        "ocr_provider" in params
        and params["ocr_provider"] is None
        and disable_winrt_probe
        and preferred_backend == "auto"
        and "disable_tesseract_probe" not in params
        and "disable_rapidocr_probe" not in params
    ):
        disable_tesseract_probe = True
        disable_rapidocr_probe = True

    try:
        ocr_res = ocr_pdf(
            in_path,
            output,
            dpi=dpi,
            quality=quality,
            source_lang=source_lang,
            output_format=output_format,
            ocr_provider=ocr_provider,
            disable_winrt_probe=disable_winrt_probe,
            disable_tesseract_probe=disable_tesseract_probe,
            disable_rapidocr_probe=disable_rapidocr_probe,
            preferred_backend=preferred_backend,
            progress_cb=lambda pct, msg: emit_progress(progress_cb, pct, msg),
        )
    except Exception as exc:
        return _error_result("pdf_ocr", str(exc), "ocr_failed")

    return ToolResult(
        success=True,
        output_paths=[Path(output)],
        summary=f"OCR completed ({len(ocr_res.pages)} page(s), {ocr_res.total_chars} chars via {ocr_res.backend})",
        details={
            "dpi": dpi,
            "quality": quality,
            "source_lang": source_lang,
            "backend": ocr_res.backend,
            "page_count": len(ocr_res.pages),
            "total_chars": ocr_res.total_chars,
            "output_format": output_format,
        },
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="convert_to_pdf",
        category="convert",
        params=[
            dpi_param_spec(150),
            quality_param_spec(85),
            source_lang_param_spec("auto"),
            ToolParamSpec("theme", "choice", "light", choices=("light", "dark")),
            ToolParamSpec("page_size", "choice", "Letter", choices=("Letter", "A4", "Legal")),
            ToolParamSpec("use_edge_headless", "bool", False),
        ],
        handler=_handle_convert_to_pdf,
        icon="import",
        min_inputs=1,
        max_inputs=1,
        input_validator="none",
        output_kind="file",
    ),
    ToolDefinition(
        id="convert_from_pdf",
        category="convert",
        params=[
            ToolParamSpec("target_format", "choice", "txt", choices=("md", "docx", "xlsx", "html", "txt")),
            dpi_param_spec(150),
            source_lang_param_spec("auto"),
        ],
        handler=_handle_convert_from_pdf,
        icon="export",
        min_inputs=1,
        max_inputs=1,
        input_validator="pdf",
        output_kind="file",
    ),
    ToolDefinition(
        id="images_to_pdf",
        category="convert",
        params=[
            dpi_param_spec(150),
            quality_param_spec(85),
            ToolParamSpec("page_size", "choice", "fit", choices=("fit", "Letter", "A4")),
        ],
        handler=_handle_images_to_pdf,
        icon="image",
        min_inputs=1,
        input_validator="media",
        output_kind="file",
    ),
    ToolDefinition(
        id="pdf_to_images",
        category="convert",
        params=[
            dpi_param_spec(150),
            quality_param_spec(90),
            ToolParamSpec("format", "choice", "png", choices=("png", "jpg", "jpeg", "tiff", "webp")),
        ],
        handler=_handle_pdf_to_images,
        icon="image",
        min_inputs=1,
        max_inputs=1,
        input_validator="pdf",
        output_kind="dir",
    ),
    ToolDefinition(
        id="pdf_ocr",
        category="convert",
        params=[
            dpi_param_spec(300),
            quality_param_spec(85),
            source_lang_param_spec("auto"),
            ToolParamSpec("output_format", "choice", "pdf", choices=("pdf", "txt", "md")),
            ToolParamSpec("backend", "choice", "auto", choices=("auto", "winrt", "tesseract", "rapidocr")),
        ],
        handler=_handle_pdf_ocr,
        icon="ocr",
        badge="WINRT",
        min_inputs=1,
        max_inputs=1,
        input_validator="pdf",
        output_kind="file",
    ),
)


def register_tools(registry: type[ToolRegistry] = ToolRegistry) -> None:
    """Register all 5 Convert & OCR tools in ToolRegistry."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn, overwrite=True)
