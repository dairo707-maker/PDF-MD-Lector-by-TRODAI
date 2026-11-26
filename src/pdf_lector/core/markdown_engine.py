"""Lazy local GFM Markdown parser, TOC extractor, safe HTML renderer, and native MD<->PDF converter."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape as xml_escape
import os
from pathlib import Path
import re
import tempfile
from typing import Sequence
import unicodedata
import uuid

from .md_security import sanitize_markdown_html
from .path_guard import (
    is_unc_or_network_path,
    resolve_safe_doc_resource,
    validate_local_markdown_path,
    validate_local_pdf_path,
)
from .win32_bridge import atomic_write_text
from .. import get_base_dir


@dataclass(frozen=True)
class TocItem:
    """Heading entry parsed from a Markdown document."""

    level: int
    title: str
    line_number: int
    anchor: str


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_BULLET_PREFIX_RE = re.compile(r"^\s*[\u2022\u00b7\u25e6\u25aa\u2013]\s+")


def slugify_heading(title: str, seen_counts: dict[str, int] | None = None) -> str:
    """Create a deterministic, accent-folded HTML/TOC anchor from a heading title."""
    norm = unicodedata.normalize("NFKD", (title or "").strip().lower())
    ascii_only = "".join(ch for ch in norm if not unicodedata.combining(ch))
    slug = _SLUG_NON_ALNUM_RE.sub("-", ascii_only).strip("-") or "section"
    if seen_counts is None:
        return slug
    count = seen_counts.get(slug, 0) + 1
    seen_counts[slug] = count
    return slug if count == 1 else f"{slug}-{count}"


def _extract_inline_heading_text(inline_token: object) -> str:
    children = getattr(inline_token, "children", None)
    if children:
        parts: list[str] = []
        for ch in children:
            ctype = getattr(ch, "type", "")
            if ctype in ("text", "code_inline"):
                parts.append(getattr(ch, "content", ""))
            elif ctype in ("softbreak", "hardbreak"):
                parts.append(" ")
        joined = "".join(parts).strip()
        if joined:
            return joined
    return str(getattr(inline_token, "content", "") or "").strip()


def _create_markdown_it(*, allow_html: bool) -> object:
    from markdown_it import MarkdownIt

    return MarkdownIt("gfm-like", {"html": allow_html, "linkify": False})


def parse_markdown_toc(text: str) -> list[TocItem]:
    """Parse Markdown ATX/Setext headings into TocItem records (ignores headings in fenced blocks)."""
    content = text or ""
    if not content.strip():
        return []

    md = _create_markdown_it(allow_html=False)
    tokens = md.parse(content)
    seen: dict[str, int] = {}
    items: list[TocItem] = []

    for idx, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        try:
            level = max(1, min(6, int(tok.tag[1:])))
        except Exception:
            level = 1
        title = ""
        if idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
            title = _extract_inline_heading_text(tokens[idx + 1])
        if not title:
            continue
        line_num = (tok.map[0] + 1) if tok.map else 1
        anchor = slugify_heading(title, seen)
        items.append(TocItem(level=level, title=title, line_number=line_num, anchor=anchor))

    return items


def normalize_toc_outline_levels(items: Sequence[TocItem]) -> list[int]:
    """Map Markdown heading levels (1..6) to 0-based outline levels.

    When a document has a single top-level heading (e.g., one H1 title) followed by
    subheadings (H2/H3), promotes both the single root level and the next level to
    top-level outline entries so the navigation tree exposes >= 2 root sections cleanly.
    """
    if not items:
        return []
    min_level = min(item.level for item in items)
    min_count = sum(1 for item in items if item.level == min_level)
    has_deeper = any(item.level > min_level for item in items)
    promote_second = min_count == 1 and has_deeper
    base_for_deeper = min_level + 1 if promote_second else min_level

    levels: list[int] = []
    prev = -1
    for item in items:
        if item.level <= min_level:
            raw = 0
        elif promote_second:
            raw = max(0, item.level - base_for_deeper)
        else:
            raw = max(0, item.level - min_level)
        # Ensure outline levels never jump by more than +1
        clamped = 0 if prev < 0 else min(raw, prev + 1)
        levels.append(clamped)
        prev = clamped
    return levels


def render_markdown_to_safe_html(
    text: str,
    base_dir: Path | None = None,
    allow_remote_images: bool = False,
) -> tuple[str, list[str]]:
    """Render GFM Markdown to strictly sanitized HTML and return (html, blocked_resources)."""
    md = _create_markdown_it(allow_html=True)
    # Allow tokenization of javascript:/vbscript:/file:/data: links & images so md_security
    # explicitly strips and logs them instead of leaving raw URIs in literal paragraph text.
    md.validateLink = lambda _url: True

    tokens = md.parse(text or "")
    seen: dict[str, int] = {}
    for idx, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        title = ""
        if idx + 1 < len(tokens) and tokens[idx + 1].type == "inline":
            title = _extract_inline_heading_text(tokens[idx + 1])
        if title:
            anchor = slugify_heading(title, seen)
            tok.attrSet("id", anchor)

    raw_html = md.renderer.render(tokens, md.options, {})
    res = sanitize_markdown_html(
        raw_html,
        base_dir=base_dir,
        allow_remote_images=allow_remote_images,
    )
    return res.html, list(res.blocked_resources)


def _register_pdf_fonts() -> tuple[str, str]:
    """Register SpaceGrotesk and JetBrainsMono TTF fonts into ReportLab if available."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts_dir = get_base_dir() / "assets" / "fonts"
    ui_font = "Helvetica"
    mono_font = "Courier"

    sg_path = fonts_dir / "SpaceGrotesk[wght].ttf"
    jb_reg = fonts_dir / "JetBrainsMono-Regular.ttf"
    jb_med = fonts_dir / "JetBrainsMono-Medium.ttf"

    registered = set(pdfmetrics.getRegisteredFontNames())
    if sg_path.exists():
        try:
            if "SpaceGrotesk" not in registered:
                pdfmetrics.registerFont(TTFont("SpaceGrotesk", str(sg_path)))
                pdfmetrics.registerFontFamily(
                    "SpaceGrotesk",
                    normal="SpaceGrotesk",
                    bold="SpaceGrotesk",
                    italic="SpaceGrotesk",
                    boldItalic="SpaceGrotesk",
                )
            ui_font = "SpaceGrotesk"
        except Exception:
            ui_font = "Helvetica"

    if jb_reg.exists():
        try:
            if "JetBrainsMono" not in registered:
                pdfmetrics.registerFont(TTFont("JetBrainsMono", str(jb_reg)))
                bold_mono = "JetBrainsMono-Medium" if jb_med.exists() else "JetBrainsMono"
                if jb_med.exists() and "JetBrainsMono-Medium" not in registered:
                    pdfmetrics.registerFont(TTFont("JetBrainsMono-Medium", str(jb_med)))
                pdfmetrics.registerFontFamily(
                    "JetBrainsMono",
                    normal="JetBrainsMono",
                    bold=bold_mono,
                    italic="JetBrainsMono",
                    boldItalic=bold_mono,
                )
            mono_font = "JetBrainsMono"
        except Exception:
            mono_font = "Courier"

    return ui_font, mono_font


def _inline_tokens_to_rml(inline_token: object, mono_font: str) -> str:
    """Convert MarkdownIt inline children into ReportLab Paragraph XML markup safely."""
    children = getattr(inline_token, "children", None)
    if not children:
        return xml_escape(str(getattr(inline_token, "content", "") or ""))

    parts: list[str] = []
    for ch in children:
        ctype = getattr(ch, "type", "")
        content = getattr(ch, "content", "") or ""
        if ctype == "text":
            parts.append(xml_escape(content))
        elif ctype == "code_inline":
            parts.append(f'<font name="{mono_font}">{xml_escape(content)}</font>')
        elif ctype == "strong_open":
            parts.append("<b>")
        elif ctype == "strong_close":
            parts.append("</b>")
        elif ctype == "em_open":
            parts.append("<i>")
        elif ctype == "em_close":
            parts.append("</i>")
        elif ctype == "s_open":
            parts.append("<strike>")
        elif ctype == "s_close":
            parts.append("</strike>")
        elif ctype in ("softbreak", "hardbreak"):
            parts.append("<br/>" if ctype == "hardbreak" else " ")
        elif ctype == "link_open":
            parts.append("<u>")
        elif ctype == "link_close":
            parts.append("</u>")
        elif ctype == "image":
            alt = _extract_inline_heading_text(ch) or content
            if alt:
                parts.append(f"[{xml_escape(alt)}]")
    return "".join(parts)


def markdown_to_pdf(
    md_path: Path,
    output_pdf: Path,
    *,
    dpi: int = 150,
    source_lang: str = "auto",
    theme: str = "light",
) -> Path:
    """Convert a local .md/.markdown file to a styled PDF with embedded TOC outline bookmarks."""
    del source_lang
    ok, reason = validate_local_markdown_path(md_path, check_exists=True)
    if not ok:
        raise ValueError(reason)

    ok_out, reason_out = validate_local_pdf_path(output_pdf, check_exists=False)
    if not ok_out:
        raise ValueError(reason_out)

    src_path = Path(md_path).resolve()
    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        HRFlowable,
        Image as RLImage,
        PageTemplate,
        Paragraph,
        Preformatted,
        Spacer,
        Table,
        TableStyle,
    )

    text = src_path.read_text(encoding="utf-8-sig")
    ui_font, mono_font = _register_pdf_fonts()
    is_dark = str(theme).strip().lower() == "dark"

    bg_col = colors.HexColor("#0B0F19" if is_dark else "#FFFFFF")
    fg_col = colors.HexColor("#F8FAFC" if is_dark else "#0A0F1D")
    sec_col = colors.HexColor("#CBD5E1" if is_dark else "#334155")
    muted_col = colors.HexColor("#94A3B8" if is_dark else "#4A5568")
    code_bg = colors.HexColor("#182234" if is_dark else "#EEF2F6")
    border_col = colors.HexColor("#334155" if is_dark else "#CBD5E1")
    tbl_hdr_bg = colors.HexColor("#212D44" if is_dark else "#E2E8F0")

    body_style = ParagraphStyle(
        "TRODAIBody",
        fontName=ui_font,
        fontSize=10.5,
        leading=15,
        textColor=sec_col,
        spaceAfter=6,
    )
    quote_style = ParagraphStyle(
        "TRODAIQuote",
        parent=body_style,
        leftIndent=14,
        textColor=muted_col,
        spaceBefore=4,
        spaceAfter=6,
    )
    code_style = ParagraphStyle(
        "TRODAICode",
        fontName=mono_font,
        fontSize=9,
        leading=12.5,
        textColor=fg_col,
        backColor=code_bg,
        borderColor=border_col,
        borderWidth=0.5,
        borderPadding=6,
        spaceBefore=4,
        spaceAfter=8,
    )
    heading_styles = {
        1: ParagraphStyle("TRODAIH1", fontName=ui_font, fontSize=20, leading=25, textColor=fg_col, spaceBefore=10, spaceAfter=8),
        2: ParagraphStyle("TRODAIH2", fontName=ui_font, fontSize=15, leading=20, textColor=fg_col, spaceBefore=10, spaceAfter=6),
        3: ParagraphStyle("TRODAIH3", fontName=ui_font, fontSize=12.5, leading=17, textColor=fg_col, spaceBefore=8, spaceAfter=4),
        4: ParagraphStyle("TRODAIH4", fontName=ui_font, fontSize=11, leading=15, textColor=fg_col, spaceBefore=6, spaceAfter=4),
        5: ParagraphStyle("TRODAIH5", fontName=ui_font, fontSize=10.5, leading=14, textColor=sec_col, spaceBefore=5, spaceAfter=3),
        6: ParagraphStyle("TRODAIH6", fontName=ui_font, fontSize=10, leading=14, textColor=muted_col, spaceBefore=4, spaceAfter=3),
    }

    toc_items = parse_markdown_toc(text)
    outline_levels = normalize_toc_outline_levels(toc_items)
    toc_idx = 0

    md = _create_markdown_it(allow_html=False)
    tokens = md.parse(text)

    flowables: list[object] = []
    list_stack: list[tuple[str, int]] = []
    in_blockquote = False
    max_width = LETTER[0] - 108.0
    safe_dpi = max(72, min(600, int(dpi)))

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        ttype = tok.type

        if ttype == "heading_open":
            level = max(1, min(6, int(tok.tag[1:])))
            inline_tok = tokens[i + 1] if i + 1 < len(tokens) else None
            rml = _inline_tokens_to_rml(inline_tok, mono_font) if inline_tok else ""
            plain_title = _extract_inline_heading_text(inline_tok) if inline_tok else ""
            p = Paragraph(rml or xml_escape(plain_title), heading_styles[level])
            if plain_title and toc_idx < len(toc_items):
                p._toc_title = toc_items[toc_idx].title  # type: ignore[attr-defined]
                p._toc_level = outline_levels[toc_idx]  # type: ignore[attr-defined]
                p._toc_key = f"md_toc_{toc_idx}_{toc_items[toc_idx].anchor}"  # type: ignore[attr-defined]
                toc_idx += 1
            flowables.append(p)
            i += 3  # heading_open, inline, heading_close
            continue

        if ttype == "blockquote_open":
            in_blockquote = True
            i += 1
            continue
        if ttype == "blockquote_close":
            in_blockquote = False
            i += 1
            continue

        if ttype == "bullet_list_open":
            list_stack.append(("ul", 0))
            i += 1
            continue
        if ttype == "ordered_list_open":
            start = int(tok.attrGet("start") or 1)
            list_stack.append(("ol", start))
            i += 1
            continue
        if ttype in ("bullet_list_close", "ordered_list_close"):
            if list_stack:
                list_stack.pop()
            i += 1
            continue

        if ttype == "paragraph_open":
            inline_tok = tokens[i + 1] if i + 1 < len(tokens) else None
            if inline_tok is not None:
                children = getattr(inline_tok, "children", None) or []
                # Check for standalone validated local image
                if len(children) == 1 and getattr(children[0], "type", "") == "image":
                    img_tok = children[0]
                    src = str(img_tok.attrGet("src") or "").strip()
                    if src and not is_unc_or_network_path(src):
                        try:
                            resolved_img = resolve_safe_doc_resource(
                                src_path.parent,
                                src.split("#", 1)[0],
                                check_exists=True,
                            )
                            from PIL import Image as PILImage

                            with PILImage.open(resolved_img) as pil_im:
                                iw, ih = pil_im.size
                            scale_pt = 72.0 / float(safe_dpi)
                            draw_w = min(max_width, max(24.0, float(iw) * scale_pt))
                            draw_h = draw_w * (float(ih) / max(1.0, float(iw)))
                            if draw_h > 360.0:
                                draw_h = 360.0
                                draw_w = draw_h * (float(iw) / max(1.0, float(ih)))
                            flowables.append(RLImage(str(resolved_img), width=draw_w, height=draw_h))
                            flowables.append(Spacer(1, 6))
                            i += 3
                            continue
                        except Exception:
                            pass

                rml = _inline_tokens_to_rml(inline_tok, mono_font)
                prefix = ""
                if list_stack:
                    kind, num = list_stack[-1]
                    indent = "&nbsp;" * (max(0, len(list_stack) - 1) * 4)
                    if kind == "ul":
                        prefix = f"{indent}- "
                    else:
                        prefix = f"{indent}{num}. "
                        list_stack[-1] = (kind, num + 1)
                style = quote_style if in_blockquote else body_style
                if rml.strip():
                    flowables.append(Paragraph(prefix + rml, style))
            i += 3
            continue

        if ttype in ("fence", "code_block"):
            flowables.append(Preformatted((tok.content or "").rstrip("\n"), code_style))
            i += 1
            continue

        if ttype == "hr":
            flowables.append(HRFlowable(width="100%", thickness=0.75, color=border_col, spaceBefore=6, spaceAfter=6))
            i += 1
            continue

        if ttype == "table_open":
            rows_data: list[list[Paragraph]] = []
            current_row: list[Paragraph] = []
            j = i + 1
            while j < len(tokens) and tokens[j].type != "table_close":
                sub = tokens[j]
                if sub.type == "tr_open":
                    current_row = []
                elif sub.type == "tr_close":
                    if current_row:
                        rows_data.append(current_row)
                elif sub.type == "inline":
                    cell_rml = _inline_tokens_to_rml(sub, mono_font)
                    current_row.append(Paragraph(cell_rml, body_style))
                j += 1
            if rows_data:
                col_count = max(len(r) for r in rows_data)
                for r in rows_data:
                    while len(r) < col_count:
                        r.append(Paragraph("", body_style))
                col_w = max_width / max(1, col_count)
                tbl = Table(rows_data, colWidths=[col_w] * col_count, repeatRows=1)
                tbl.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), tbl_hdr_bg),
                            ("TEXTCOLOR", (0, 0), (-1, -1), fg_col),
                            ("GRID", (0, 0), (-1, -1), 0.5, border_col),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 5),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                            ("LEFTPADDING", (0, 0), (-1, -1), 6),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ]
                    )
                )
                flowables.append(tbl)
                flowables.append(Spacer(1, 8))
            i = j + 1
            continue

        i += 1

    if not flowables:
        flowables.append(Spacer(1, 12))

    class _TocDocTemplate(BaseDocTemplate):
        def afterFlowable(self, flowable: object) -> None:
            key = getattr(flowable, "_toc_key", None)
            title = getattr(flowable, "_toc_title", None)
            level = getattr(flowable, "_toc_level", None)
            if key and title is not None and level is not None:
                self.canv.bookmarkPage(key)
                self.canv.addOutlineEntry(str(title), str(key), level=int(level), closed=False)

    def _draw_bg(canv, _doc) -> None:
        canv.saveState()
        canv.setFillColor(bg_col)
        canv.rect(0, 0, LETTER[0], LETTER[1], stroke=0, fill=1)
        canv.restoreState()

    fd, tmp_name = tempfile.mkstemp(prefix=f".{out_path.stem}.", suffix=".pdf.tmp", dir=str(out_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        doc = _TocDocTemplate(
            str(tmp_path),
            pagesize=LETTER,
            leftMargin=54,
            rightMargin=54,
            topMargin=54,
            bottomMargin=54,
            title=src_path.stem,
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
        doc.addPageTemplates([PageTemplate(id="md_page", frames=[frame], onPage=_draw_bg)])
        doc.build(flowables)
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return out_path


def pdf_to_markdown(
    pdf_path: Path,
    output_md: Path,
    *,
    dpi: int = 150,
    source_lang: str = "auto",
) -> Path:
    """Extract structured Markdown (.md) from a local PDF via pypdfium2."""
    del dpi
    ok, reason = validate_local_pdf_path(pdf_path, check_exists=True)
    if not ok:
        raise ValueError(reason)

    ok_out, reason_out = validate_local_markdown_path(output_md, check_exists=False)
    if not ok_out:
        raise ValueError(reason_out)

    import pypdfium2 as pdfium

    src_path = Path(pdf_path).resolve()
    out_path = Path(output_md)
    lang_code = (source_lang or "auto").strip().lower()
    page_word = "Page" if lang_code == "en" else "Pagina"

    doc = pdfium.PdfDocument(str(src_path))
    try:
        toc_by_page: dict[int, list[tuple[int, str]]] = {}
        try:
            for bm in doc.get_toc():
                title = (bm.get_title() or "").strip()
                dest = bm.get_dest()
                pidx = dest.get_index() if dest is not None else None
                if title and pidx is not None and 0 <= pidx < len(doc):
                    lvl = max(1, min(6, int(bm.level) + 1))
                    toc_by_page.setdefault(int(pidx), []).append((lvl, title))
        except Exception:
            toc_by_page = {}

        has_any_toc = bool(toc_by_page)
        sections: list[str] = []

        for page_idx in range(len(doc)):
            page = doc[page_idx]
            tp = None
            try:
                tp = page.get_textpage()
                raw_text = tp.get_text_range() or ""
            finally:
                if tp is not None:
                    tp.close()
                page.close()

            raw_lines = [ln.strip() for ln in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
            non_empty = [ln for ln in raw_lines if ln]

            page_out: list[str] = []
            page_toc = toc_by_page.get(page_idx, [])
            toc_titles_lower = {t.lower() for _, t in page_toc}

            if page_toc:
                for lvl, title in page_toc:
                    page_out.append(f"{'#' * lvl} {title}")
                page_out.append("")
            elif not has_any_toc and non_empty:
                # Emit deterministic section heading if PDF lacked an outline
                first = non_empty[0]
                if not first.startswith("#"):
                    page_out.append(f"## {page_word} {page_idx + 1}")
                    page_out.append("")

            para_buf: list[str] = []

            def _flush_para() -> None:
                if para_buf:
                    page_out.append(" ".join(para_buf))
                    page_out.append("")
                    para_buf.clear()

            for line in raw_lines:
                if not line:
                    _flush_para()
                    continue
                if line.lower() in toc_titles_lower:
                    continue
                if _BULLET_PREFIX_RE.match(line):
                    _flush_para()
                    cleaned_bullet = _BULLET_PREFIX_RE.sub("- ", line, count=1)
                    page_out.append(cleaned_bullet)
                elif line.startswith("- ") or line.startswith("* "):
                    _flush_para()
                    page_out.append("- " + line[2:].strip())
                else:
                    para_buf.append(line)

            _flush_para()
            block = "\n".join(page_out).strip()
            if block:
                sections.append(block)

        if not sections:
            final_md = f"# {src_path.stem}\n"
        else:
            final_md = "\n\n".join(sections).strip() + "\n"
            if not parse_markdown_toc(final_md):
                final_md = f"# {src_path.stem}\n\n{final_md}"

        atomic_write_text(out_path, final_md)
        return out_path
    finally:
        doc.close()
