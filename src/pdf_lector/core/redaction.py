"""Genuine destructive PDF redaction engine (pikepdf content-stream surgery + pypdfium2)."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import time
from typing import Any, Literal, Sequence
import uuid

import pikepdf
import pypdfium2 as pdfium

from .metadata_sanitizer import sanitize_pdf_metadata

IDENTITY_MATRIX: tuple[float, float, float, float, float, float] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_STD14_ALIASES = {
    "helvetica": "Helvetica",
    "arial": "Helvetica",
    "arialmt": "Helvetica",
    "helvetica-bold": "Helvetica-Bold",
    "arial-boldmt": "Helvetica-Bold",
    "helvetica-oblique": "Helvetica-Oblique",
    "helvetica-boldoblique": "Helvetica-BoldOblique",
    "times-roman": "Times-Roman",
    "timesnewromanpsmt": "Times-Roman",
    "times-bold": "Times-Bold",
    "times-italic": "Times-Italic",
    "times-bolditalic": "Times-BoldItalic",
    "courier": "Courier",
    "couriernewpsmt": "Courier",
    "courier-bold": "Courier-Bold",
    "courier-oblique": "Courier-Oblique",
    "courier-boldoblique": "Courier-BoldOblique",
    "symbol": "Symbol",
    "zapfdingbats": "ZapfDingbats",
}

_PATH_BUILD_OPS = {"m", "l", "c", "v", "y", "h", "re"}
_PATH_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
_SKIP_CLONED_STREAM_KEYS = {
    "/Length",
    "/Filter",
    "/DecodeParms",
    "/DL",
    "/F",
    "/FFilter",
    "/FDecodeParms",
}
_FORM_RESOURCE_SUBDICTS = (
    "/Font",
    "/XObject",
    "/ExtGState",
    "/ColorSpace",
    "/Pattern",
    "/Shading",
    "/Properties",
)


def _merge_form_resources(
    target_res: Any,
    parent_res: Any,
    *,
    exclude_xobj_name: str = "",
) -> tuple[Any, set[str]]:
    """Merge parent resource sub-dictionaries as fallbacks for a Form XObject without overwriting own entries."""
    fallback_xobj_keys: set[str] = set()
    if not isinstance(target_res, pikepdf.Dictionary):
        if not isinstance(parent_res, pikepdf.Dictionary):
            return None, fallback_xobj_keys
        merged = pikepdf.Dictionary(parent_res)
        parent_xobjs = parent_res.get("/XObject")
        if isinstance(parent_xobjs, pikepdf.Dictionary):
            cloned_xobjs = pikepdf.Dictionary()
            for k, v in parent_xobjs.items():
                sk = str(k)
                if sk == exclude_xobj_name:
                    continue
                cloned_xobjs[k] = v
                fallback_xobj_keys.add(sk)
            merged["/XObject"] = cloned_xobjs
        return merged, fallback_xobj_keys

    if not isinstance(parent_res, pikepdf.Dictionary):
        return target_res, fallback_xobj_keys

    merged = pikepdf.Dictionary(target_res)
    for sub_key in _FORM_RESOURCE_SUBDICTS:
        parent_sub = parent_res.get(sub_key)
        if not isinstance(parent_sub, pikepdf.Dictionary):
            continue
        child_sub = target_res.get(sub_key)
        if isinstance(child_sub, pikepdf.Dictionary):
            merged_sub = pikepdf.Dictionary(child_sub)
            added_any = False
            for k, v in parent_sub.items():
                sk = str(k)
                if sk not in merged_sub:
                    if sub_key == "/XObject" and sk == exclude_xobj_name:
                        continue
                    merged_sub[k] = v
                    added_any = True
                    if sub_key == "/XObject":
                        fallback_xobj_keys.add(sk)
            if added_any:
                merged[sub_key] = merged_sub
        elif sub_key not in target_res:
            if sub_key == "/XObject":
                cloned_xobjs = pikepdf.Dictionary()
                for k, v in parent_sub.items():
                    sk = str(k)
                    if sk == exclude_xobj_name:
                        continue
                    cloned_xobjs[k] = v
                    fallback_xobj_keys.add(sk)
                if len(cloned_xobjs) > 0:
                    merged[sub_key] = cloned_xobjs
            else:
                merged[sub_key] = parent_sub

    return merged, fallback_xobj_keys


@dataclass(frozen=True)
class RedactionRegion:
    page_index: int
    rect: tuple[float, float, float, float]  # (x0, y0, x1, y1)
    coords_space: Literal["pdf", "display"] = "pdf"


@dataclass(frozen=True)
class RedactionOptions:
    fill_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    remove_annotations: bool = True
    expand_pt: float = 0.25
    partial_word_mode: Literal["glyph", "span"] = "glyph"
    max_vector_bg_area_ratio: float = 0.85
    strip_metadata_on_save: bool = False


@dataclass(frozen=True)
class RedactionResult:
    output_path: Path
    pages_modified: int
    regions_applied: int
    removed_text_ops: int
    removed_vector_ops: int
    removed_image_ops: int
    removed_annots: int
    verified_clean: bool


def _normalize_rect(rect: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _mat_mul(
    m1: tuple[float, float, float, float, float, float],
    m2: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply_matrix(
    m: tuple[float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _transform_bbox(
    m: tuple[float, float, float, float, float, float],
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    pts = [
        _apply_matrix(m, x0, y0),
        _apply_matrix(m, x1, y0),
        _apply_matrix(m, x0, y1),
        _apply_matrix(m, x1, y1),
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _rects_intersect(
    r1: tuple[float, float, float, float],
    r2: tuple[float, float, float, float],
    *,
    expand: float = 0.0,
) -> bool:
    ax0, ay0, ax1, ay1 = _normalize_rect(r1)
    bx0, by0, bx1, by1 = _normalize_rect(r2)
    return not (
        ax1 < (bx0 - expand)
        or ax0 > (bx1 + expand)
        or ay1 < (by0 - expand)
        or ay0 > (by1 + expand)
    )


def _glyph_intersects_rect(
    glyph_bbox: tuple[float, float, float, float],
    rect: tuple[float, float, float, float],
    *,
    expand_pt: float,
) -> bool:
    gx0, gy0, gx1, gy1 = _normalize_rect(glyph_bbox)
    rx0, ry0, rx1, ry1 = _normalize_rect(rect)
    if gx1 < (rx0 - expand_pt) or gx0 > (rx1 + expand_pt):
        return False
    ov_y = min(gy1, ry1) - max(gy0, ry0)
    if ov_y <= 0.0:
        return False
    gh = max(1e-3, gy1 - gy0)
    rh = max(1e-3, ry1 - ry0)
    return ov_y >= min(0.30 * gh, 0.30 * rh)


def display_rect_to_pdf_rect(
    rect: tuple[float, float, float, float],
    crop_or_mediabox: tuple[float, float, float, float],
    rotate_deg: int,
) -> tuple[float, float, float, float]:
    """Convert rotated display-space rect back to unrotated PDF user-space coordinates."""
    dx0, dy0, dx1, dy1 = _normalize_rect(rect)
    cx0, cy0, cx1, cy1 = _normalize_rect(crop_or_mediabox)
    w = cx1 - cx0
    h = cy1 - cy0
    rot = int(rotate_deg) % 360

    def _pt(x: float, y: float) -> tuple[float, float]:
        if rot == 90:
            return (cx0 + (w - y), cy0 + x)
        if rot == 180:
            return (cx0 + (w - x), cy0 + (h - y))
        if rot == 270:
            return (cx0 + y, cy0 + (h - x))
        return (cx0 + x, cy0 + y)

    pts = [_pt(dx0, dy0), _pt(dx1, dy0), _pt(dx0, dy1), _pt(dx1, dy1)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class _FontMetricsInfo:
    is_cid: bool = False
    code_bytes: int = 1
    default_width: float = 500.0
    widths: dict[int, float] | None = None
    fallback_widths: dict[int, float] | None = None
    descent: float = -0.25
    ascent: float = 0.95
    scale_x: float = 1.0


class _PageCharOracle:
    """Lazy pypdfium2 character-box oracle for fonts lacking /Widths."""

    def __init__(self, doc: pdfium.PdfDocument, page_index: int) -> None:
        page = doc[page_index]
        tp = page.get_textpage()
        count = tp.count_chars()
        self.text: str = tp.get_text_range(0, count) if count > 0 else ""
        self.boxes: list[tuple[float, float, float, float]] = [
            tp.get_charbox(i) for i in range(min(count, len(self.text)))
        ]
        self.used_indices: set[int] = set()

    def find_run_boxes(
        self,
        run_text: str,
        sx: float,
        sy: float,
        font_size_user: float,
    ) -> list[tuple[float, float, float, float]] | None:
        if not run_text or not self.text or len(run_text) > len(self.boxes):
            return None
        tol_y = max(12.0, 1.5 * font_size_user)
        tol_x = max(80.0, 6.0 * font_size_user)
        best_idx = -1
        best_dist = float("inf")
        start = 0
        n_run = len(run_text)
        while True:
            idx = self.text.find(run_text, start)
            if idx < 0 or idx + n_run > len(self.boxes):
                break
            if idx not in self.used_indices:
                l0, b0, r0, t0 = self.boxes[idx]
                dy = 0.0 if (b0 - tol_y <= sy <= t0 + tol_y) else abs((b0 + t0) * 0.5 - sy)
                dx = abs(l0 - sx)
                if dy <= tol_y and dx <= tol_x:
                    dist = dx + dy * 4.0
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = idx
            start = idx + 1
        if best_idx < 0:
            return None
        for k in range(best_idx, best_idx + n_run):
            self.used_indices.add(k)
        return self.boxes[best_idx : best_idx + n_run]


def _std14_widths(base_font_name: str) -> dict[int, float] | None:
    clean = base_font_name.lstrip("/")
    if "+" in clean:
        clean = clean.split("+", 1)[1]
    mapped = _STD14_ALIASES.get(clean.lower())
    if not mapped:
        return None
    try:
        from reportlab.pdfbase import pdfmetrics

        font_obj = pdfmetrics.getFont(mapped)
        widths = getattr(font_obj, "widths", None)
        if isinstance(widths, list):
            return {idx: float(w) for idx, w in enumerate(widths)}
        if isinstance(widths, dict):
            return {int(k): float(v) for k, v in widths.items()}
    except Exception:
        pass
    return None


def _parse_cid_w_array(w_arr: Any) -> dict[int, float]:
    widths: dict[int, float] = {}
    if not isinstance(w_arr, pikepdf.Array):
        return widths
    i = 0
    n = len(w_arr)
    while i < n:
        first_cid = int(w_arr[i])
        if i + 1 >= n:
            break
        nxt = w_arr[i + 1]
        if isinstance(nxt, pikepdf.Array):
            for offset, w_val in enumerate(nxt):
                widths[first_cid + offset] = float(w_val)
            i += 2
        elif i + 2 < n:
            last_cid = int(nxt)
            w_val = float(w_arr[i + 2])
            for cid in range(first_cid, last_cid + 1):
                widths[cid] = w_val
            i += 3
        else:
            break
    return widths


def _extract_font_metrics(
    font_obj: Any,
    cache: dict[tuple[int, int], _FontMetricsInfo],
) -> _FontMetricsInfo:
    if not isinstance(font_obj, pikepdf.Dictionary):
        return _FontMetricsInfo()
    objgen = getattr(font_obj, "objgen", (0, 0))
    if objgen != (0, 0) and objgen in cache:
        return cache[objgen]

    info = _FontMetricsInfo()
    subtype = str(font_obj.get("/Subtype", ""))

    if subtype == "/Type3":
        fmat = font_obj.get("/FontMatrix")
        if isinstance(fmat, pikepdf.Array) and len(fmat) >= 1 and float(fmat[0]) != 0:
            info.scale_x = float(fmat[0]) * 1000.0

    if subtype == "/Type0":
        info.is_cid = True
        info.code_bytes = 2
        info.default_width = 1000.0
        desc_arr = font_obj.get("/DescendantFonts")
        if isinstance(desc_arr, pikepdf.Array) and len(desc_arr) > 0:
            cid_font = desc_arr[0]
            if isinstance(cid_font, pikepdf.Dictionary):
                if "/DW" in cid_font:
                    info.default_width = float(cid_font["/DW"])
                if "/W" in cid_font:
                    info.widths = _parse_cid_w_array(cid_font["/W"])
                fd = cid_font.get("/FontDescriptor")
                if isinstance(fd, pikepdf.Dictionary):
                    fbbox = fd.get("/FontBBox")
                    if isinstance(fbbox, pikepdf.Array) and len(fbbox) == 4:
                        info.descent = min(-0.15, float(fbbox[1]) / 1000.0)
                        info.ascent = max(0.75, float(fbbox[3]) / 1000.0)
    else:
        widths_map: dict[int, float] = {}
        first_char = int(font_obj.get("/FirstChar", 0))
        w_arr = font_obj.get("/Widths")
        if isinstance(w_arr, pikepdf.Array):
            for idx, w_val in enumerate(w_arr):
                widths_map[first_char + idx] = float(w_val) * info.scale_x
        else:
            base_font = str(font_obj.get("/BaseFont", ""))
            std_map = _std14_widths(base_font)
            if std_map:
                widths_map = std_map

        fd = font_obj.get("/FontDescriptor")
        flags = 0
        if isinstance(fd, pikepdf.Dictionary):
            if "/Flags" in fd:
                try:
                    flags = int(fd["/Flags"])
                except Exception:
                    flags = 0
            if "/MissingWidth" in fd:
                info.default_width = float(fd["/MissingWidth"])
            fbbox = fd.get("/FontBBox")
            if isinstance(fbbox, pikepdf.Array) and len(fbbox) == 4:
                info.descent = min(-0.15, float(fbbox[1]) / 1000.0)
                info.ascent = max(0.75, float(fbbox[3]) / 1000.0)
        info.widths = widths_map or None
        if info.widths is None and (flags & 1) == 0:
            info.fallback_widths = _std14_widths("Helvetica")

    if objgen != (0, 0):
        cache[objgen] = info
    return info


@dataclass
class _GState:
    ctm: tuple[float, float, float, float, float, float] = IDENTITY_MATRIX
    font_name: str = ""
    font_size: float = 12.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    h_scale: float = 100.0
    leading: float = 0.0
    text_rise: float = 0.0
    render_mode: int = 0

    def clone(self) -> _GState:
        return replace(self)


@dataclass
class _FilterCounters:
    removed_text_ops: int = 0
    removed_vector_ops: int = 0
    removed_image_ops: int = 0


def _split_pdf_string_glyphs(
    raw_bytes: bytes,
    state: _GState,
    tm: tuple[float, float, float, float, float, float],
    font_info: _FontMetricsInfo,
    regions: Sequence[tuple[float, float, float, float]],
    options: RedactionOptions,
    *,
    char_oracle: _PageCharOracle | None = None,
) -> tuple[list[bytes | float], tuple[float, float, float, float, float, float], bool, bool]:
    """Evaluate every glyph in `raw_bytes` against `regions`.

    Returns `(tj_parts, new_tm, any_removed, all_removed)`.
    `tj_parts` alternates kept `bytes` runs and `float` TJ kerning adjustments.
    """
    step = 2 if (font_info.is_cid and len(raw_bytes) >= 2 and len(raw_bytes) % 2 == 0) else 1
    hz = max(1e-4, state.h_scale / 100.0)
    fsize = state.font_size if abs(state.font_size) > 1e-6 else 12.0
    widths = font_info.widths or font_info.fallback_widths or {}

    oracle_boxes: list[tuple[float, float, float, float]] | None = None
    ctm_tot = _mat_mul(tm, state.ctm)
    sx, sy = _apply_matrix(ctm_tot, 0.0, 0.0)
    ux_pt, uy_pt = _apply_matrix(ctm_tot, 1.0, 0.0)
    dx_dir = ux_pt - sx
    dy_dir = uy_pt - sy
    scale_user = math.hypot(dx_dir, dy_dir)
    ux = dx_dir / scale_user if scale_user > 1e-6 else 1.0
    uy = dy_dir / scale_user if scale_user > 1e-6 else 0.0
    font_size_user = max(1.0, abs(fsize) * hz * scale_user)

    if font_info.widths is None and not font_info.is_cid and char_oracle is not None and raw_bytes:
        run_text = raw_bytes.decode("cp1252", errors="replace")
        oracle_boxes = char_oracle.find_run_boxes(run_text, sx, sy, font_size_user)

    glyph_records: list[tuple[bytes, float, bool]] = []
    cur_tm = tm
    any_removed = False

    if oracle_boxes is not None and len(oracle_boxes) == len(raw_bytes):
        _, yb_line = _apply_matrix(ctm_tot, 0.0, state.text_rise + font_info.descent * fsize)
        _, yt_line = _apply_matrix(ctm_tot, 0.0, state.text_rise + font_info.ascent * fsize)
        line_y0, line_y1 = min(yb_line, yt_line), max(yb_line, yt_line)

        for pos in range(len(raw_bytes)):
            chunk = raw_bytes[pos : pos + 1]
            l0, b0, r0, t0 = oracle_boxes[pos]
            if (t0 - b0) < 0.5:
                b0, t0 = line_y0, line_y1
            if (r0 - l0) < 0.2:
                r0 = l0 + 0.25 * font_size_user
            bbox = (l0, b0, r0, t0)

            if pos + 1 < len(oracle_boxes):
                nl, nb, nr, nt = oracle_boxes[pos + 1]
                if abs(ux) >= abs(uy):
                    step_user = (nl - l0) if ux >= 0 else (r0 - nr)
                else:
                    step_user = (nb - b0) if uy >= 0 else (t0 - nt)
                if step_user < 0.0:
                    step_user = (r0 - l0) if abs(ux) >= abs(uy) else (t0 - b0)
            else:
                step_user = (r0 - l0) if abs(ux) >= abs(uy) else (t0 - b0)
            adv_t = step_user / max(1e-6, scale_user)

            hit = any(
                _glyph_intersects_rect(bbox, reg, expand_pt=options.expand_pt)
                for reg in regions
            )
            if hit:
                any_removed = True
            glyph_records.append((chunk, adv_t, hit))
            cur_tm = _mat_mul((1.0, 0.0, 0.0, 1.0, adv_t, 0.0), cur_tm)
    else:
        for pos in range(0, len(raw_bytes), step):
            chunk = raw_bytes[pos : pos + step]
            code = int.from_bytes(chunk, "big") if step == 2 else chunk[0]
            w = widths.get(code, font_info.default_width)
            adv_t = (
                (w / 1000.0) * fsize
                + state.char_spacing
                + (state.word_spacing if code == 0x20 else 0.0)
            ) * hz
            glyph_w = max(0.5, (w / 1000.0) * fsize * hz)
            y0_t = state.text_rise + font_info.descent * fsize
            y1_t = state.text_rise + font_info.ascent * fsize
            bbox = _transform_bbox(_mat_mul(cur_tm, state.ctm), (0.0, y0_t, glyph_w, y1_t))

            hit = any(
                _glyph_intersects_rect(bbox, reg, expand_pt=options.expand_pt)
                for reg in regions
            )
            if hit:
                any_removed = True
            glyph_records.append((chunk, adv_t, hit))
            cur_tm = _mat_mul((1.0, 0.0, 0.0, 1.0, adv_t, 0.0), cur_tm)

    if not any_removed:
        return [raw_bytes], cur_tm, False, False

    if options.partial_word_mode == "span":
        return [], cur_tm, True, True

    if all(h for _, _, h in glyph_records):
        return [], cur_tm, True, True

    parts: list[bytes | float] = []
    buf = bytearray()
    removed_adv = 0.0
    denom = max(1e-6, fsize * hz)

    for chunk, adv_t, hit in glyph_records:
        if hit:
            if buf:
                parts.append(bytes(buf))
                buf.clear()
            removed_adv += adv_t
        else:
            if removed_adv != 0.0:
                tj_num = -(removed_adv / denom) * 1000.0
                parts.append(tj_num)
                removed_adv = 0.0
            buf.extend(chunk)

    if buf:
        parts.append(bytes(buf))
    if removed_adv != 0.0:
        tj_num = -(removed_adv / denom) * 1000.0
        parts.append(tj_num)

    return parts, cur_tm, True, False


def _coalesce_tj_items(items: Sequence[bytes | float]) -> list[Any]:
    out: list[Any] = []
    acc_num = 0.0
    has_num = False

    def _flush_num() -> None:
        nonlocal acc_num, has_num
        if has_num and abs(acc_num) > 1e-4:
            out.append(round(acc_num, 3))
        acc_num = 0.0
        has_num = False

    for item in items:
        if isinstance(item, bytes):
            if item:
                _flush_num()
                out.append(pikepdf.String(item))
        else:
            acc_num += float(item)
            has_num = True
    _flush_num()
    return out


def _filter_content_stream(
    pdf: pikepdf.Pdf,
    stream_owner: Any,
    resources: Any,
    regions: Sequence[tuple[float, float, float, float]],
    page_area: float,
    options: RedactionOptions,
    font_cache: dict[tuple[int, int], _FontMetricsInfo],
    visited_forms: set[tuple[int, int]],
    counters: _FilterCounters,
    *,
    initial_ctm: tuple[float, float, float, float, float, float] = IDENTITY_MATRIX,
    form_clone_map: dict[tuple[tuple[int, int], tuple[float, ...]], pikepdf.Stream] | None = None,
    get_char_oracle: Any = None,
) -> tuple[list[Any], int, Any]:
    """Parse and filter a page or Form XObject content stream, returning (ops, unclosed_q, resources)."""
    ops = pikepdf.parse_content_stream(stream_owner)
    out_ops: list[Any] = []
    working_res = resources
    res_cloned = False
    local_form_names: dict[tuple[tuple[int, int], tuple[float, ...]], str] = {}
    replaced_form_names: set[str] = set()

    def _ensure_writable_res() -> pikepdf.Dictionary | None:
        nonlocal working_res, res_cloned
        if not isinstance(working_res, pikepdf.Dictionary):
            return None
        if not res_cloned:
            working_res = pikepdf.Dictionary(working_res)
            res_cloned = True
            if isinstance(stream_owner, pikepdf.Page):
                stream_owner["/Resources"] = working_res
        return working_res

    state = _GState(ctm=initial_ctm)
    stack: list[_GState] = []
    in_text = False
    tm = IDENTITY_MATRIX
    tlm = IDENTITY_MATRIX
    tm_dirty = False

    path_buffer: list[Any] = []
    path_pts: list[tuple[float, float]] = []
    pending_clip = False
    cur_pt = (0.0, 0.0)
    bdc_stack: list[tuple[pikepdf.Name | None, Any, bool]] = []  # (prop_name, prop_dict, had_redacted)

    def _flush_path_keep() -> None:
        nonlocal pending_clip
        if path_buffer:
            out_ops.extend(path_buffer)
            path_buffer.clear()
        path_pts.clear()
        pending_clip = False

    def _emit_tm_if_dirty() -> None:
        nonlocal tm_dirty
        if tm_dirty:
            out_ops.append(
                pikepdf.ContentStreamInstruction(
                    [round(float(v), 6) for v in tm],
                    pikepdf.Operator("Tm"),
                )
            )
            tm_dirty = False

    def _get_font_info() -> _FontMetricsInfo:
        if not isinstance(working_res, pikepdf.Dictionary) or not state.font_name:
            return _FontMetricsInfo()
        fonts = working_res.get("/Font")
        if not isinstance(fonts, pikepdf.Dictionary):
            return _FontMetricsInfo()
        return _extract_font_metrics(fonts.get(state.font_name), font_cache)

    def _handle_text_string_op(raw_bytes: bytes, *, prepend_tstar: bool = False) -> None:
        nonlocal tm, tlm, tm_dirty
        if prepend_tstar:
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -state.leading), tlm)
            tm = tlm
            out_ops.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
            tm_dirty = False

        finfo = _get_font_info()
        oracle = (
            get_char_oracle()
            if (finfo.widths is None and not finfo.is_cid and get_char_oracle is not None)
            else None
        )
        parts, new_tm, any_removed, _all_removed = _split_pdf_string_glyphs(
            raw_bytes, state, tm, finfo, regions, options, char_oracle=oracle
        )
        tm = new_tm

        if not any_removed:
            _emit_tm_if_dirty()
            out_ops.append(
                pikepdf.ContentStreamInstruction(
                    [pikepdf.String(raw_bytes)],
                    pikepdf.Operator("Tj"),
                )
            )
            return

        counters.removed_text_ops += 1
        for idx_b in range(len(bdc_stack)):
            bdc_stack[idx_b] = (bdc_stack[idx_b][0], bdc_stack[idx_b][1], True)
        has_kept_bytes = any(isinstance(p, bytes) and len(p) > 0 for p in parts)
        if not has_kept_bytes:
            tm_dirty = True
            return

        _emit_tm_if_dirty()
        tj_elems = _coalesce_tj_items(parts)
        if len(tj_elems) == 1 and isinstance(tj_elems[0], pikepdf.String):
            out_ops.append(
                pikepdf.ContentStreamInstruction([tj_elems[0]], pikepdf.Operator("Tj"))
            )
        else:
            out_ops.append(
                pikepdf.ContentStreamInstruction(
                    [pikepdf.Array(tj_elems)],
                    pikepdf.Operator("TJ"),
                )
            )

    def _handle_tj_array_op(arr_obj: pikepdf.Array) -> None:
        nonlocal tm, tm_dirty
        finfo = _get_font_info()
        oracle = (
            get_char_oracle()
            if (finfo.widths is None and not finfo.is_cid and get_char_oracle is not None)
            else None
        )
        hz = max(1e-4, state.h_scale / 100.0)
        fsize = state.font_size if abs(state.font_size) > 1e-6 else 12.0

        combined: list[bytes | float] = []
        any_removed = False

        for elem in arr_obj:
            if isinstance(elem, pikepdf.String):
                raw_bytes = bytes(elem)
                parts, new_tm, elem_removed, _ = _split_pdf_string_glyphs(
                    raw_bytes, state, tm, finfo, regions, options, char_oracle=oracle
                )
                tm = new_tm
                if elem_removed:
                    any_removed = True
                combined.extend(parts)
            else:
                try:
                    num = float(elem)
                except Exception:
                    continue
                adv_t = -(num / 1000.0) * fsize * hz
                tm = _mat_mul((1.0, 0.0, 0.0, 1.0, adv_t, 0.0), tm)
                combined.append(num)

        if not any_removed:
            _emit_tm_if_dirty()
            out_ops.append(
                pikepdf.ContentStreamInstruction([arr_obj], pikepdf.Operator("TJ"))
            )
            return

        counters.removed_text_ops += 1
        for idx_b in range(len(bdc_stack)):
            bdc_stack[idx_b] = (bdc_stack[idx_b][0], bdc_stack[idx_b][1], True)
        has_kept_bytes = any(isinstance(p, bytes) and len(p) > 0 for p in combined)
        if not has_kept_bytes:
            tm_dirty = True
            return

        _emit_tm_if_dirty()
        tj_elems = _coalesce_tj_items(combined)
        out_ops.append(
            pikepdf.ContentStreamInstruction(
                [pikepdf.Array(tj_elems)],
                pikepdf.Operator("TJ"),
            )
        )

    for item in ops:
        if isinstance(item, pikepdf.ContentStreamInlineImage):
            bbox = _transform_bbox(state.ctm, (0.0, 0.0, 1.0, 1.0))
            if any(_rects_intersect(bbox, reg, expand=options.expand_pt) for reg in regions):
                counters.removed_image_ops += 1
            else:
                out_ops.append(item)
            continue

        op_name = str(item.operator)
        operands = item.operands

        if op_name == "q":
            stack.append(state.clone())
            out_ops.append(item)
        elif op_name == "Q":
            if stack:
                state = stack.pop()
            out_ops.append(item)
        elif op_name == "cm" and len(operands) == 6:
            m = tuple(float(v) for v in operands)
            state.ctm = _mat_mul(m, state.ctm)  # type: ignore[arg-type]
            out_ops.append(item)
        elif op_name == "BT":
            in_text = True
            tm = IDENTITY_MATRIX
            tlm = IDENTITY_MATRIX
            tm_dirty = False
            out_ops.append(item)
        elif op_name == "ET":
            in_text = False
            tm_dirty = False
            out_ops.append(item)
        elif op_name == "Tf" and len(operands) >= 2:
            state.font_name = str(operands[0])
            state.font_size = float(operands[1])
            out_ops.append(item)
        elif op_name == "Tc" and len(operands) >= 1:
            state.char_spacing = float(operands[0])
            out_ops.append(item)
        elif op_name == "Tw" and len(operands) >= 1:
            state.word_spacing = float(operands[0])
            out_ops.append(item)
        elif op_name == "Tz" and len(operands) >= 1:
            state.h_scale = float(operands[0])
            out_ops.append(item)
        elif op_name == "TL" and len(operands) >= 1:
            state.leading = float(operands[0])
            out_ops.append(item)
        elif op_name == "Ts" and len(operands) >= 1:
            state.text_rise = float(operands[0])
            out_ops.append(item)
        elif op_name == "Tr" and len(operands) >= 1:
            state.render_mode = int(operands[0])
            out_ops.append(item)
        elif op_name == "Td" and len(operands) >= 2:
            tx, ty = float(operands[0]), float(operands[1])
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
            tm = tlm
            tm_dirty = False
            out_ops.append(item)
        elif op_name == "TD" and len(operands) >= 2:
            tx, ty = float(operands[0]), float(operands[1])
            state.leading = -ty
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
            tm = tlm
            tm_dirty = False
            out_ops.append(item)
        elif op_name == "Tm" and len(operands) == 6:
            tm = tuple(float(v) for v in operands)  # type: ignore[assignment]
            tlm = tm
            tm_dirty = False
            out_ops.append(item)
        elif op_name == "T*":
            tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -state.leading), tlm)
            tm = tlm
            tm_dirty = False
            out_ops.append(item)
        elif in_text and op_name == "Tj" and len(operands) >= 1:
            _handle_text_string_op(bytes(operands[0]), prepend_tstar=False)
        elif in_text and op_name == "TJ" and len(operands) >= 1 and isinstance(operands[0], pikepdf.Array):
            _handle_tj_array_op(operands[0])
        elif in_text and op_name == "'" and len(operands) >= 1:
            _handle_text_string_op(bytes(operands[0]), prepend_tstar=True)
        elif in_text and op_name == '"' and len(operands) >= 3:
            state.word_spacing = float(operands[0])
            state.char_spacing = float(operands[1])
            out_ops.append(pikepdf.ContentStreamInstruction([operands[0]], pikepdf.Operator("Tw")))
            out_ops.append(pikepdf.ContentStreamInstruction([operands[1]], pikepdf.Operator("Tc")))
            _handle_text_string_op(bytes(operands[2]), prepend_tstar=True)
        elif op_name in _PATH_BUILD_OPS:
            path_buffer.append(item)
            if op_name in ("m", "l") and len(operands) >= 2:
                pt = (float(operands[0]), float(operands[1]))
                cur_pt = pt
                path_pts.append(_apply_matrix(state.ctm, pt[0], pt[1]))
            elif op_name == "c" and len(operands) >= 6:
                for idx in (0, 2, 4):
                    path_pts.append(_apply_matrix(state.ctm, float(operands[idx]), float(operands[idx + 1])))
                cur_pt = (float(operands[4]), float(operands[5]))
            elif op_name in ("v", "y") and len(operands) >= 4:
                for idx in (0, 2):
                    path_pts.append(_apply_matrix(state.ctm, float(operands[idx]), float(operands[idx + 1])))
                cur_pt = (float(operands[2]), float(operands[3]))
            elif op_name == "re" and len(operands) >= 4:
                rx, ry, rw, rh = (float(operands[i]) for i in range(4))
                for px, py in ((rx, ry), (rx + rw, ry), (rx, ry + rh), (rx + rw, ry + rh)):
                    path_pts.append(_apply_matrix(state.ctm, px, py))
                cur_pt = (rx, ry)
            else:
                path_pts.append(_apply_matrix(state.ctm, cur_pt[0], cur_pt[1]))
        elif op_name in ("W", "W*"):
            pending_clip = True
            path_buffer.append(item)
        elif op_name == "n":
            path_buffer.append(item)
            _flush_path_keep()
        elif op_name in _PATH_PAINT_OPS:
            if path_pts:
                xs = [p[0] for p in path_pts]
                ys = [p[1] for p in path_pts]
                pbbox = (min(xs), min(ys), max(xs), max(ys))
                p_area = max(0.0, (pbbox[2] - pbbox[0]) * (pbbox[3] - pbbox[1]))
                intersects = any(_rects_intersect(pbbox, reg, expand=options.expand_pt) for reg in regions)
                fully_inside = any(
                    pbbox[0] >= reg[0] - options.expand_pt
                    and pbbox[1] >= reg[1] - options.expand_pt
                    and pbbox[2] <= reg[2] + options.expand_pt
                    and pbbox[3] <= reg[3] + options.expand_pt
                    for reg in regions
                )
                is_bg = (page_area > 0.0) and ((p_area / page_area) >= options.max_vector_bg_area_ratio)
                if intersects and (not is_bg or fully_inside):
                    counters.removed_vector_ops += 1
                    if pending_clip:
                        out_ops.extend(path_buffer)
                        out_ops.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("n")))
                    path_buffer.clear()
                    path_pts.clear()
                    pending_clip = False
                    continue

            path_buffer.append(item)
            _flush_path_keep()
        elif op_name == "Do" and len(operands) >= 1:
            xobj_name = str(operands[0])
            xobjs = working_res.get("/XObject") if isinstance(working_res, pikepdf.Dictionary) else None
            target = xobjs.get(xobj_name) if isinstance(xobjs, pikepdf.Dictionary) else None
            emitted_op = item
            if isinstance(target, pikepdf.Stream):
                subtype = str(target.get("/Subtype", ""))
                if subtype == "/Image":
                    bbox = _transform_bbox(state.ctm, (0.0, 0.0, 1.0, 1.0))
                    if any(_rects_intersect(bbox, reg, expand=options.expand_pt) for reg in regions):
                        counters.removed_image_ops += 1
                        continue
                elif subtype == "/Form":
                    fmat_arr = target.get("/Matrix")
                    if isinstance(fmat_arr, pikepdf.Array) and len(fmat_arr) == 6:
                        fmat = tuple(float(v) for v in fmat_arr)
                    else:
                        fmat = IDENTITY_MATRIX
                    form_ctm = _mat_mul(fmat, state.ctm)  # type: ignore[arg-type]

                    fbbox_arr = target.get("/BBox")
                    if isinstance(fbbox_arr, pikepdf.Array) and len(fbbox_arr) == 4:
                        fbbox = tuple(float(v) for v in fbbox_arr)
                        fbbox_user = _transform_bbox(form_ctm, fbbox)  # type: ignore[arg-type]
                        form_hits = any(
                            _rects_intersect(fbbox_user, reg, expand=options.expand_pt) for reg in regions
                        )
                    else:
                        form_hits = True

                    t_gen = getattr(target, "objgen", (0, 0))
                    if form_hits and t_gen not in visited_forms:
                        if t_gen != (0, 0):
                            visited_forms.add(t_gen)
                        base_name = xobj_name if xobj_name.startswith("/") else f"/{xobj_name}"
                        ctm_key = tuple(round(float(v), 4) for v in form_ctm)
                        clone_key = (t_gen, ctm_key)
                        cloned_form = (
                            form_clone_map.get(clone_key)
                            if (form_clone_map is not None and t_gen != (0, 0))
                            else None
                        )
                        if cloned_form is None:
                            target_res = target.get("/Resources")
                            form_res, fallback_xobj_keys = _merge_form_resources(
                                target_res,
                                working_res,
                                exclude_xobj_name=base_name,
                            )
                            form_ops, _, form_out_res = _filter_content_stream(
                                pdf,
                                target,
                                form_res,
                                regions,
                                page_area,
                                options,
                                font_cache,
                                visited_forms,
                                counters,
                                initial_ctm=form_ctm,
                                form_clone_map=form_clone_map,
                                get_char_oracle=get_char_oracle,
                            )
                            if fallback_xobj_keys and isinstance(form_out_res, pikepdf.Dictionary):
                                form_do_names = {
                                    str(op.operands[0])
                                    for op in form_ops
                                    if hasattr(op, "operator") and str(op.operator) == "Do" and op.operands
                                }
                                fxobjs = form_out_res.get("/XObject")
                                if isinstance(fxobjs, pikepdf.Dictionary):
                                    drop_fb = [
                                        fk
                                        for fk in fallback_xobj_keys
                                        if fk in fxobjs and fk not in form_do_names
                                    ]
                                    if drop_fb:
                                        cleaned_fx = pikepdf.Dictionary(fxobjs)
                                        for fk in drop_fb:
                                            del cleaned_fx[fk]
                                        had_own_xobjs = isinstance(
                                            target_res.get("/XObject")
                                            if isinstance(target_res, pikepdf.Dictionary)
                                            else None,
                                            pikepdf.Dictionary,
                                        )
                                        if len(cleaned_fx) == 0 and not had_own_xobjs:
                                            del form_out_res["/XObject"]
                                        else:
                                            form_out_res["/XObject"] = cleaned_fx
                            cloned_form = pdf.make_stream(pikepdf.unparse_content_stream(form_ops))
                            for k, v in target.items():
                                sk = str(k)
                                if sk in _SKIP_CLONED_STREAM_KEYS:
                                    continue
                                if sk == "/Resources" and form_out_res is not None:
                                    continue
                                cloned_form[k] = v
                            if form_out_res is not None:
                                cloned_form["/Resources"] = form_out_res
                            if form_clone_map is not None and t_gen != (0, 0):
                                form_clone_map[clone_key] = cloned_form
                        if isinstance(xobjs, pikepdf.Dictionary):
                            unique_name = local_form_names.get(clone_key) if t_gen != (0, 0) else None
                            if unique_name is None:
                                wres = _ensure_writable_res()
                                if wres is not None:
                                    cur_xobjs = wres.get("/XObject")
                                    new_xobjs = (
                                        pikepdf.Dictionary(cur_xobjs)
                                        if isinstance(cur_xobjs, pikepdf.Dictionary)
                                        else pikepdf.Dictionary()
                                    )
                                    r_idx = 1
                                    unique_name = f"{base_name}_r{r_idx}"
                                    while unique_name in new_xobjs:
                                        r_idx += 1
                                        unique_name = f"{base_name}_r{r_idx}"
                                    new_xobjs[unique_name] = cloned_form
                                    wres["/XObject"] = new_xobjs
                                    xobjs = new_xobjs
                                    if t_gen != (0, 0):
                                        local_form_names[clone_key] = unique_name
                            if unique_name is not None:
                                emitted_op = pikepdf.ContentStreamInstruction(
                                    [pikepdf.Name(unique_name)], pikepdf.Operator("Do")
                                )
                                replaced_form_names.add(base_name)
                        if t_gen != (0, 0):
                            visited_forms.discard(t_gen)

            out_ops.append(emitted_op)
        elif op_name == "BMC":
            bdc_stack.append((None, None, False))
            out_ops.append(item)
        elif op_name == "BDC" and len(operands) >= 2:
            prop = operands[1]
            prop_name: pikepdf.Name | None = None
            prop_dict: Any = None
            if isinstance(prop, pikepdf.Dictionary):
                prop_dict = prop
            elif isinstance(prop, pikepdf.Name) and isinstance(working_res, pikepdf.Dictionary):
                prop_name = prop
                props = working_res.get("/Properties")
                if isinstance(props, pikepdf.Dictionary):
                    prop_dict = props.get(str(prop))
            bdc_stack.append((prop_name, prop_dict, False))
            out_ops.append(item)
        elif op_name == "EMC":
            if bdc_stack:
                prop_name, prop_dict, had_redacted = bdc_stack.pop()
                if had_redacted and isinstance(prop_dict, pikepdf.Dictionary):
                    if any(k in prop_dict for k in ("/ActualText", "/Alt")):
                        if prop_name is not None:
                            wres = _ensure_writable_res()
                            if wres is not None and isinstance(wres.get("/Properties"), pikepdf.Dictionary):
                                new_props = pikepdf.Dictionary(wres["/Properties"])
                                cloned_prop = pikepdf.Dictionary(prop_dict)
                                for k in ("/ActualText", "/Alt"):
                                    if k in cloned_prop:
                                        del cloned_prop[k]
                                new_props[str(prop_name)] = cloned_prop
                                wres["/Properties"] = new_props
                        else:
                            for k in ("/ActualText", "/Alt"):
                                if k in prop_dict:
                                    del prop_dict[k]
            out_ops.append(item)
        else:
            if path_buffer:
                _flush_path_keep()
            out_ops.append(item)

    if path_buffer:
        _flush_path_keep()

    if replaced_form_names and isinstance(working_res, pikepdf.Dictionary):
        remaining_do_names = {
            str(op.operands[0])
            for op in out_ops
            if hasattr(op, "operator") and str(op.operator) == "Do" and op.operands
        }
        wres = _ensure_writable_res()
        if wres is not None and isinstance(wres.get("/XObject"), pikepdf.Dictionary):
            cur_xobjs = wres["/XObject"]
            to_remove = [
                name
                for name in replaced_form_names
                if name in cur_xobjs and name not in remaining_do_names
            ]
            if to_remove:
                new_xobjs = pikepdf.Dictionary(cur_xobjs)
                for name in to_remove:
                    del new_xobjs[name]
                wres["/XObject"] = new_xobjs

    return out_ops, len(stack), working_res


def _remove_intersecting_annotations(
    page: pikepdf.Page,
    regions: Sequence[tuple[float, float, float, float]],
    expand_pt: float,
) -> int:
    annots = page.get("/Annots")
    if not isinstance(annots, pikepdf.Array) or len(annots) == 0:
        return 0

    removed_objgens: set[tuple[int, int]] = set()
    kept: list[Any] = []
    removed = 0

    # First pass: detect directly intersecting annotations + collect popup objgens
    candidates: list[Any] = []
    for annot in annots:
        if not isinstance(annot, pikepdf.Dictionary):
            candidates.append(annot)
            continue
        rect_arr = annot.get("/Rect")
        hit = False
        if isinstance(rect_arr, pikepdf.Array) and len(rect_arr) == 4:
            arect = tuple(float(v) for v in rect_arr)
            hit = any(_rects_intersect(arect, reg, expand=expand_pt) for reg in regions)
        if hit:
            removed += 1
            og = getattr(annot, "objgen", (0, 0))
            if og != (0, 0):
                removed_objgens.add(og)
            popup = annot.get("/Popup")
            if isinstance(popup, pikepdf.Dictionary):
                pog = getattr(popup, "objgen", (0, 0))
                if pog != (0, 0):
                    removed_objgens.add(pog)
        else:
            candidates.append(annot)

    # Second pass: drop any Popup whose /Parent was removed
    for annot in candidates:
        if isinstance(annot, pikepdf.Dictionary):
            og = getattr(annot, "objgen", (0, 0))
            parent = annot.get("/Parent")
            parent_og = getattr(parent, "objgen", (0, 0)) if isinstance(parent, pikepdf.Dictionary) else (0, 0)
            if (og != (0, 0) and og in removed_objgens) or (
                parent_og != (0, 0) and parent_og in removed_objgens
            ):
                removed += 1
                continue
        kept.append(annot)

    if removed > 0:
        if kept:
            page["/Annots"] = pikepdf.Array(kept)
        else:
            del page["/Annots"]
    return removed


def verify_redaction_clean(
    pdf_path: Path,
    forbidden_terms: Sequence[str],
    *,
    regions_by_page: dict[int, list[tuple[float, float, float, float]]] | None = None,
) -> tuple[bool, list[str]]:
    """Verify via BOTH pypdfium2 extraction and raw pikepdf streams that forbidden terms are gone."""
    p = Path(pdf_path)
    terms = [str(t) for t in forbidden_terms if str(t)]
    findings: list[str] = []
    if not terms:
        return True, findings

    # 1. PDFium text extraction check
    doc = pdfium.PdfDocument(str(p))
    try:
        for idx in range(len(doc)):
            page = doc[idx]
            tp = page.get_textpage()
            full_text = tp.get_text_range()
            for term in terms:
                if term.lower() in full_text.lower():
                    findings.append(f"pdfium:page[{idx}]:term:{term}")
            if regions_by_page and idx in regions_by_page:
                for rect in regions_by_page[idx]:
                    x0, y0, x1, y1 = _normalize_rect(rect)
                    bounded = tp.get_text_bounded(x0, y0, x1, y1).strip()
                    for term in terms:
                        if term.lower() in bounded.lower():
                            findings.append(f"pdfium:page[{idx}]:bounded:{term}")
    finally:
        doc.close()

    # 2. Raw pikepdf stream, Form XObject, and /Annots check
    def _scan_stream(owner: Any, loc: str, visited: set[tuple[int, int]]) -> None:
        try:
            ops = pikepdf.parse_content_stream(owner)
        except Exception as exc:
            findings.append(f"pikepdf:{loc}:decode_error:{type(exc).__name__}:{exc}")
            return
        chunks: list[str] = []
        for item in ops:
            if not hasattr(item, "operator"):
                continue
            op_name = str(item.operator)
            if op_name in ("Tj", "'", '"'):
                for op in item.operands:
                    if isinstance(op, pikepdf.String):
                        raw = bytes(op)
                        chunks.append(raw.decode("latin-1", errors="ignore"))
                        if len(raw) >= 2 and len(raw) % 2 == 0:
                            chunks.append(raw.decode("utf-16-be", errors="ignore"))
            elif op_name == "TJ" and item.operands and isinstance(item.operands[0], pikepdf.Array):
                for el in item.operands[0]:
                    if isinstance(el, pikepdf.String):
                        raw = bytes(el)
                        chunks.append(raw.decode("latin-1", errors="ignore"))
                        if len(raw) >= 2 and len(raw) % 2 == 0:
                            chunks.append(raw.decode("utf-16-be", errors="ignore"))
            elif op_name in ("BDC", "DP") and len(item.operands) >= 2:
                prop = item.operands[1]
                if isinstance(prop, pikepdf.Dictionary):
                    for k in ("/ActualText", "/Alt"):
                        if k in prop:
                            chunks.append(str(prop[k]))
        joined = "".join(chunks)
        for term in terms:
            if term.lower() in joined.lower():
                findings.append(f"pikepdf:{loc}:term:{term}")

        if isinstance(owner, pikepdf.Page):
            res = owner.get("/Resources")
        elif isinstance(owner, pikepdf.Stream):
            res = owner.stream_dict.get("/Resources")
        elif isinstance(owner, pikepdf.Dictionary):
            res = owner.get("/Resources")
        else:
            res = None
        if isinstance(res, pikepdf.Dictionary):
            xobjs = res.get("/XObject")
            if isinstance(xobjs, pikepdf.Dictionary):
                for name, xobj in xobjs.items():
                    if isinstance(xobj, pikepdf.Stream) and str(xobj.get("/Subtype", "")) == "/Form":
                        og = getattr(xobj, "objgen", (0, 0))
                        if og != (0, 0) and og in visited:
                            continue
                        if og != (0, 0):
                            visited.add(og)
                        _scan_stream(xobj, f"{loc}/Form{name}", visited)

    with pikepdf.Pdf.open(p) as pdf:
        visited: set[tuple[int, int]] = set()
        for idx, page in enumerate(pdf.pages):
            _scan_stream(page, f"page[{idx}]", visited)
            annots = page.get("/Annots")
            if isinstance(annots, pikepdf.Array):
                for a_idx, annot in enumerate(annots):
                    if not isinstance(annot, pikepdf.Dictionary):
                        continue
                    for key in ("/Contents", "/RC", "/T", "/Subj"):
                        if key in annot:
                            val = str(annot[key])
                            for term in terms:
                                if term.lower() in val.lower():
                                    findings.append(f"pikepdf:page[{idx}]/Annot[{a_idx}]{key}:{term}")

    return (len(findings) == 0), findings


def redact_pdf_regions(
    input_pdf: Path,
    output_pdf: Path,
    regions: Sequence[RedactionRegion],
    *,
    options: RedactionOptions | None = None,
) -> RedactionResult:
    """Destructively remove underlying text, vectors, images, and annots intersecting `regions`."""
    opts = options or RedactionOptions()
    in_path = Path(input_pdf)
    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    same_file = False
    try:
        same_file = in_path.resolve() == out_path.resolve()
    except OSError:
        same_file = False

    grouped: dict[int, list[RedactionRegion]] = {}
    for reg in regions:
        grouped.setdefault(int(reg.page_index), []).append(reg)

    counters = _FilterCounters()
    pages_modified = 0
    regions_applied = 0
    removed_annots = 0
    font_cache: dict[tuple[int, int], _FontMetricsInfo] = {}

    pdfium_doc: pdfium.PdfDocument | None = None
    try:
        with pikepdf.Pdf.open(in_path) as pdf:
            num_pages = len(pdf.pages)
            for page_idx, page_regs in grouped.items():
                if page_idx < 0 or page_idx >= num_pages:
                    continue
                page = pdf.pages[page_idx]
                mbox = page.get("/CropBox") or page.get("/MediaBox") or pikepdf.Array([0, 0, 612, 792])
                box_tuple = (float(mbox[0]), float(mbox[1]), float(mbox[2]), float(mbox[3]))
                page_area = max(1.0, (box_tuple[2] - box_tuple[0]) * (box_tuple[3] - box_tuple[1]))
                rotate_deg = int(page.get("/Rotate", 0) or 0)

                pdf_rects: list[tuple[float, float, float, float]] = []
                for reg in page_regs:
                    if reg.coords_space == "display":
                        pdf_rects.append(display_rect_to_pdf_rect(reg.rect, box_tuple, rotate_deg))
                    else:
                        pdf_rects.append(_normalize_rect(reg.rect))

                if not pdf_rects:
                    continue

                page_oracle_cache: list[_PageCharOracle | None] = [None]

                def _get_page_oracle(
                    p_idx: int = page_idx,
                    cache: list[_PageCharOracle | None] = page_oracle_cache,
                ) -> _PageCharOracle | None:
                    nonlocal pdfium_doc
                    if cache[0] is not None:
                        return cache[0]
                    try:
                        if pdfium_doc is None:
                            pdfium_doc = pdfium.PdfDocument(str(in_path))
                        cache[0] = _PageCharOracle(pdfium_doc, p_idx)
                        return cache[0]
                    except Exception:
                        return None

                visited_forms: set[tuple[int, int]] = set()
                form_clone_map: dict[tuple[tuple[int, int], tuple[float, ...]], pikepdf.Stream] = {}
                page_res = page.get("/Resources")
                filtered_ops, open_q, _ = _filter_content_stream(
                    pdf,
                    page,
                    page_res,
                    pdf_rects,
                    page_area,
                    opts,
                    font_cache,
                    visited_forms,
                    counters,
                    initial_ctm=IDENTITY_MATRIX,
                    form_clone_map=form_clone_map,
                    get_char_oracle=_get_page_oracle,
                )

                # Balance q/Q stack and draw opaque redaction rectangles
                wrapped_ops: list[Any] = [
                    pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                    *filtered_ops,
                ]
                for _ in range(1 + max(0, open_q)):
                    wrapped_ops.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")))

                r, g, b = (max(0.0, min(1.0, float(c))) for c in opts.fill_color)
                for rx0, ry0, rx1, ry1 in pdf_rects:
                    rw = max(0.5, rx1 - rx0)
                    rh = max(0.5, ry1 - ry0)
                    wrapped_ops.extend(
                        [
                            pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")),
                            pikepdf.ContentStreamInstruction(
                                [round(r, 4), round(g, 4), round(b, 4)],
                                pikepdf.Operator("rg"),
                            ),
                            pikepdf.ContentStreamInstruction(
                                [round(rx0, 4), round(ry0, 4), round(rw, 4), round(rh, 4)],
                                pikepdf.Operator("re"),
                            ),
                            pikepdf.ContentStreamInstruction([], pikepdf.Operator("f")),
                            pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")),
                        ]
                    )

                page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(wrapped_ops))
                if opts.remove_annotations:
                    removed_annots += _remove_intersecting_annotations(page, pdf_rects, opts.expand_pt)

                pages_modified += 1
                regions_applied += len(pdf_rects)

            pdf.remove_unreferenced_resources()

            tmp_target = (
                out_path.with_name(f"{out_path.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.pdf")
                if same_file
                else out_path
            )
            try:
                pdf.save(
                    tmp_target,
                    linearize=False,
                    object_stream_mode=pikepdf.ObjectStreamMode.disable,
                )
            except Exception:
                if same_file and tmp_target.exists():
                    tmp_target.unlink(missing_ok=True)
                raise
    finally:
        if pdfium_doc is not None:
            pdfium_doc.close()

    if same_file:
        last_err: OSError | None = None
        for attempt in range(4):
            try:
                os.replace(tmp_target, out_path)
                last_err = None
                break
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.05 * (attempt + 1))
        if last_err is not None:
            raise last_err

    if opts.strip_metadata_on_save:
        sanitize_pdf_metadata(out_path, out_path)

    return RedactionResult(
        output_path=out_path,
        pages_modified=pages_modified,
        regions_applied=regions_applied,
        removed_text_ops=counters.removed_text_ops,
        removed_vector_ops=counters.removed_vector_ops,
        removed_image_ops=counters.removed_image_ops,
        removed_annots=removed_annots,
        verified_clean=True,
    )


def redact_pdf_terms(
    input_pdf: Path,
    output_pdf: Path,
    terms: Sequence[str],
    *,
    pages: Sequence[int] | None = None,
    match_case: bool = False,
    match_whole_word: bool = False,
    options: RedactionOptions | None = None,
) -> RedactionResult:
    """Locate exact PDF user-space bounding rects for `terms` via pypdfium2 and destructively redact."""
    in_path = Path(input_pdf)
    clean_terms = [str(t) for t in terms if str(t)]
    regions: list[RedactionRegion] = []
    regions_by_page: dict[int, list[tuple[float, float, float, float]]] = {}

    doc = pdfium.PdfDocument(str(in_path))
    try:
        target_pages = (
            range(len(doc))
            if pages is None
            else [p for p in pages if 0 <= p < len(doc)]
        )
        for page_idx in target_pages:
            page = doc[page_idx]
            tp = page.get_textpage()
            for term in clean_terms:
                searcher = tp.search(
                    term,
                    index=0,
                    match_case=match_case,
                    match_whole_word=match_whole_word,
                )
                while True:
                    hit = searcher.get_next()
                    if hit is None:
                        break
                    char_idx, count = hit
                    n_rects = tp.count_rects(char_idx, count)
                    if n_rects > 0:
                        for r_idx in range(n_rects):
                            rect = _normalize_rect(tp.get_rect(r_idx))
                            regions.append(RedactionRegion(page_idx, rect, "pdf"))
                            regions_by_page.setdefault(page_idx, []).append(rect)
                    elif count > 0:
                        boxes = [tp.get_charbox(char_idx + off) for off in range(count)]
                        rect = (
                            min(b[0] for b in boxes),
                            min(b[1] for b in boxes),
                            max(b[2] for b in boxes),
                            max(b[3] for b in boxes),
                        )
                        regions.append(RedactionRegion(page_idx, rect, "pdf"))
                        regions_by_page.setdefault(page_idx, []).append(rect)
    finally:
        doc.close()

    res = redact_pdf_regions(in_path, output_pdf, regions, options=options)
    clean_ok, _ = verify_redaction_clean(
        res.output_path,
        clean_terms,
        regions_by_page=regions_by_page,
    )
    return replace(res, verified_clean=clean_ok)
