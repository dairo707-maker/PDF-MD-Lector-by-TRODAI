"""ISO 32000 PDF annotation & ink persistence (lazy pikepdf, zero OS username leakage)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Literal, Sequence
import uuid

from .path_guard import validate_local_pdf_path

DEFAULT_ANON_AUTHOR = "An\u00f3nimo"
CLASSIC_HIGHLIGHT_COLORS: tuple[str, ...] = (
    "#FDE047",
    "#86EFAC",
    "#7DD3FC",
    "#F9A8D4",
    "#FDBA74",
    "#D8B4FE",
)
DEFAULT_HIGHLIGHT_COLOR = "#FEF08A"
VALID_KINDS: tuple[str, ...] = ("highlight", "text", "ink", "freetext", "popup")
_MANAGED_SUBTYPES = {"/Highlight", "/Text", "/Ink", "/FreeText", "/Popup"}
_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_PDF_DATE_RE = re.compile(
    r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz]|[+-]\d{2}'?\d{2}'?)?"
)


@dataclass
class AnnotationRecord:
    """Normalized ISO 32000 annotation record (10-arg positional contract preserved)."""

    id: str
    page_index: int
    kind: Literal["highlight", "text", "ink", "freetext", "popup"]
    rect_pdf: tuple[float, float, float, float]
    points_pdf: Sequence[Sequence[tuple[float, ...]]] | tuple[()]
    color_hex: str = DEFAULT_HIGHLIGHT_COLOR
    author: str = DEFAULT_ANON_AUTHOR
    timestamp_iso: str = ""
    content: str = ""
    parent_id: str | None = None
    stroke_width: float = 2.0
    opacity: float = 0.45


def _pikepdf():
    import pikepdf

    return pikepdf


def sanitize_color_hex(color_hex: str | None, fallback: str = DEFAULT_HIGHLIGHT_COLOR) -> str:
    """Validate and normalize #RRGGBB color strings; fallback on invalid hex."""
    raw = (color_hex or "").strip()
    m = _HEX_RE.fullmatch(raw)
    if not m:
        return fallback
    return f"#{m.group(1).upper()}"


def sanitize_author(author: str | None) -> str:
    """Return stripped author or DEFAULT_ANON_AUTHOR; never reads OS username."""
    clean = (author or "").strip()
    return clean if clean else DEFAULT_ANON_AUTHOR


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_to_pdf_date(ts_iso: str | None) -> str:
    """Convert ISO-8601 timestamp string to PDF date format D:YYYYMMDDHHmmSS+00'00'."""
    raw = (ts_iso or "").strip()
    dt: datetime | None = None
    if raw:
        try:
            normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
        except ValueError:
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("D:%Y%m%d%H%M%S+00'00'")


def pdf_date_to_iso(pdf_date: str | None) -> str:
    """Convert PDF date string D:YYYYMMDDHHmmSS... to UTC ISO-8601."""
    raw = (pdf_date or "").strip()
    m = _PDF_DATE_RE.match(raw)
    if not m:
        return raw
    year = int(m.group(1))
    month = int(m.group(2) or 1)
    day = int(m.group(3) or 1)
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return raw


def _hex_to_rgb_floats(color_hex: str) -> tuple[float, float, float]:
    clean = sanitize_color_hex(color_hex)
    r = int(clean[1:3], 16) / 255.0
    g = int(clean[3:5], 16) / 255.0
    b = int(clean[5:7], 16) / 255.0
    return (round(r, 4), round(g, 4), round(b, 4))


def _rgb_array_to_hex(arr: object, fallback: str = DEFAULT_HIGHLIGHT_COLOR) -> str:
    try:
        vals = [float(x) for x in arr]  # type: ignore[union-attr]
        if len(vals) >= 3:
            r, g, b = (max(0, min(255, int(round(c * 255)))) for c in vals[:3])
            return f"#{r:02X}{g:02X}{b:02X}"
    except Exception:
        pass
    return fallback


def _norm_rect(rect: Sequence[float]) -> tuple[float, float, float, float]:
    vals = [float(v) for v in rect[:4]]
    while len(vals) < 4:
        vals.append(0.0)
    x0, y0, x1, y1 = vals
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _choose_backup_path(src_path: Path) -> Path:
    candidate = src_path.with_name(f"{src_path.name}.bak")
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        alt = src_path.with_name(f"{src_path.name}.{idx}.bak")
        if not alt.exists():
            return alt
        idx += 1


def _write_backup_atomic(src_path: Path) -> Path:
    bak_path = _choose_backup_path(src_path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{bak_path.name}.", dir=str(src_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(src_path, tmp_path)
        os.replace(tmp_path, bak_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return bak_path


def _build_ap_stream(
    pdf,
    pikepdf,
    kind: str,
    rect: tuple[float, float, float, float],
    rgb: tuple[float, float, float],
    rec: AnnotationRecord,
):
    x0, y0, x1, y1 = rect
    w = max(1.0, x1 - x0)
    h = max(1.0, y1 - y0)
    r, g, b = rgb

    if kind == "highlight":
        ca = max(0.15, min(0.85, float(rec.opacity)))
        data = f"q /GS1 gs {r:.3f} {g:.3f} {b:.3f} rg 0 0 {w:.2f} {h:.2f} re f Q\n".encode("ascii")
        ap = pikepdf.Stream(pdf, data)
        ap["/Type"] = pikepdf.Name("/XObject")
        ap["/Subtype"] = pikepdf.Name("/Form")
        ap["/BBox"] = pikepdf.Array([0.0, 0.0, w, h])
        ap["/Matrix"] = pikepdf.Array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        ap["/Resources"] = pikepdf.Dictionary(
            {
                "/ExtGState": pikepdf.Dictionary(
                    {
                        "/GS1": pikepdf.Dictionary(
                            {
                                "/Type": pikepdf.Name("/ExtGState"),
                                "/ca": ca,
                                "/CA": ca,
                                "/BM": pikepdf.Name("/Multiply"),
                            }
                        )
                    }
                )
            }
        )
        return pdf.make_indirect(ap)

    if kind == "text":
        data = (
            f"q {r:.3f} {g:.3f} {b:.3f} rg 0.2 0.2 0.2 RG 1 w "
            f"0 0 {w:.2f} {h:.2f} re B "
            f"3 {h * 0.65:.2f} m {w - 3:.2f} {h * 0.65:.2f} l S "
            f"3 {h * 0.40:.2f} m {w - 5:.2f} {h * 0.40:.2f} l S Q\n"
        ).encode("ascii")
        ap = pikepdf.Stream(pdf, data)
        ap["/Type"] = pikepdf.Name("/XObject")
        ap["/Subtype"] = pikepdf.Name("/Form")
        ap["/BBox"] = pikepdf.Array([0.0, 0.0, w, h])
        ap["/Matrix"] = pikepdf.Array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        return pdf.make_indirect(ap)

    if kind == "ink":
        stroke_w = max(0.5, min(24.0, float(rec.stroke_width)))
        cmds: list[str] = [f"q {stroke_w:.2f} w 1 J 1 j {r:.3f} {g:.3f} {b:.3f} RG"]
        for stroke in rec.points_pdf:
            if len(stroke) < 2:
                continue
            sx0, sy0 = float(stroke[0][0]) - x0, float(stroke[0][1]) - y0
            cmds.append(f"{sx0:.2f} {sy0:.2f} m")
            for pt in stroke[1:]:
                cmds.append(f"{float(pt[0]) - x0:.2f} {float(pt[1]) - y0:.2f} l")
            cmds.append("S")
        cmds.append("Q\n")
        ap = pikepdf.Stream(pdf, " ".join(cmds).encode("ascii"))
        ap["/Type"] = pikepdf.Name("/XObject")
        ap["/Subtype"] = pikepdf.Name("/Form")
        ap["/BBox"] = pikepdf.Array([0.0, 0.0, w, h])
        ap["/Matrix"] = pikepdf.Array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        return pdf.make_indirect(ap)

    if kind == "freetext":
        safe_txt = (rec.content or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        data = (
            f"q 1 1 1 rg {r:.3f} {g:.3f} {b:.3f} RG 1 w 0 0 {w:.2f} {h:.2f} re B "
            f"BT /Helv 10 Tf 0.08 0.10 0.16 rg 4 {max(4.0, h - 14.0):.2f} Td ({safe_txt}) Tj ET Q\n"
        ).encode("cp1252", errors="replace")
        ap = pikepdf.Stream(pdf, data)
        ap["/Type"] = pikepdf.Name("/XObject")
        ap["/Subtype"] = pikepdf.Name("/Form")
        ap["/BBox"] = pikepdf.Array([0.0, 0.0, w, h])
        ap["/Matrix"] = pikepdf.Array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        ap["/Resources"] = pikepdf.Dictionary(
            {
                "/Font": pikepdf.Dictionary(
                    {
                        "/Helv": pikepdf.Dictionary(
                            {
                                "/Type": pikepdf.Name("/Font"),
                                "/Subtype": pikepdf.Name("/Type1"),
                                "/BaseFont": pikepdf.Name("/Helvetica"),
                                "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
                            }
                        )
                    }
                )
            }
        )
        return pdf.make_indirect(ap)

    return None


def load_pdf_annotations(pdf_path: Path | str) -> list[AnnotationRecord]:
    """Load ISO 32000 markup annotations from a validated local PDF using lazy pikepdf."""
    ok, reason = validate_local_pdf_path(pdf_path, check_exists=True)
    if not ok:
        raise ValueError(reason)

    pikepdf = _pikepdf()
    resolved = Path(pdf_path).resolve()
    records: list[AnnotationRecord] = []

    try:
        with pikepdf.open(resolved) as pdf:
            if pdf.is_encrypted:
                raise PermissionError("encrypted_pdf_requires_unlock")

            seen_objgens: set[tuple[int, int]] = set()
            raw_entries: list[tuple[int, object, tuple[int, int], str]] = []
            id_by_objgen: dict[tuple[int, int], str] = {}

            for page_idx, page in enumerate(pdf.pages):
                annots = page.get("/Annots")
                if not annots:
                    continue
                for idx, annot in enumerate(annots):
                    try:
                        subtype = str(annot.get("/Subtype", ""))
                    except Exception:
                        continue
                    if subtype not in _MANAGED_SUBTYPES:
                        continue
                    objgen = getattr(annot, "objgen", (page_idx, idx))
                    if objgen in seen_objgens:
                        continue
                    seen_objgens.add(objgen)
                    nm = str(annot.get("/NM", "")).strip() or f"annot-{page_idx}-{idx}"
                    id_by_objgen[objgen] = nm
                    raw_entries.append((page_idx, annot, objgen, nm))

            for page_idx, annot, _objgen, nm in raw_entries:
                subtype = str(annot.get("/Subtype", ""))
                kind = subtype.lstrip("/").lower()
                if kind not in VALID_KINDS:
                    continue

                rect_raw = annot.get("/Rect")
                rect_pdf = _norm_rect([float(v) for v in rect_raw]) if rect_raw else (0.0, 0.0, 0.0, 0.0)

                points_pdf: list[list[tuple[float, float]]] = []
                if kind == "ink" and "/InkList" in annot:
                    for stroke in annot["/InkList"]:
                        coords = [float(v) for v in stroke]
                        pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
                        if pts:
                            points_pdf.append(pts)

                color_hex = _rgb_array_to_hex(annot.get("/C"))
                author = sanitize_author(str(annot.get("/T", "")) if "/T" in annot else "")
                ts_iso = pdf_date_to_iso(str(annot.get("/M", "")) if "/M" in annot else "")
                content = str(annot.get("/Contents", "")) if "/Contents" in annot else ""

                parent_id: str | None = None
                parent_obj = annot.get("/IRT") or annot.get("/Parent")
                if parent_obj is not None:
                    p_og = getattr(parent_obj, "objgen", None)
                    if p_og in id_by_objgen:
                        parent_id = id_by_objgen[p_og]
                    else:
                        p_nm = str(parent_obj.get("/NM", "")).strip() if hasattr(parent_obj, "get") else ""
                        parent_id = p_nm or None

                stroke_width = 2.0
                if "/BS" in annot and "/W" in annot["/BS"]:
                    try:
                        stroke_width = float(annot["/BS"]["/W"])
                    except Exception:
                        stroke_width = 2.0
                opacity = 0.45
                if "/CA" in annot:
                    try:
                        opacity = float(annot["/CA"])
                    except Exception:
                        opacity = 0.45

                records.append(
                    AnnotationRecord(
                        id=nm,
                        page_index=page_idx,
                        kind=kind,  # type: ignore[arg-type]
                        rect_pdf=rect_pdf,
                        points_pdf=points_pdf if kind == "ink" else (),
                        color_hex=color_hex,
                        author=author,
                        timestamp_iso=ts_iso,
                        content=content,
                        parent_id=parent_id,
                        stroke_width=stroke_width,
                        opacity=opacity,
                    )
                )
    except PermissionError:
        raise
    except pikepdf.PasswordError as exc:
        raise PermissionError("encrypted_pdf_requires_unlock") from exc
    except pikepdf.PdfError as exc:
        raise ValueError(f"invalid_pdf: {exc}") from exc

    return records


def save_pdf_annotations(
    pdf_path: Path | str,
    annotations: Sequence[AnnotationRecord],
    *,
    output_path: Path | str | None = None,
    create_backup: bool = True,
    flatten: bool = False,
) -> Path:
    """Persist ISO 32000 annotations with /AP streams, optional .bak backup, and optional vector flatten."""
    ok, reason = validate_local_pdf_path(pdf_path, check_exists=True)
    if not ok:
        raise ValueError(reason)

    src_path = Path(pdf_path).resolve()
    if output_path is not None:
        ok_out, reason_out = validate_local_pdf_path(output_path, check_exists=False)
        if not ok_out:
            raise ValueError(reason_out)
        target = Path(output_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target = src_path

    pikepdf = _pikepdf()

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".pdf.tmp", dir=str(target.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with pikepdf.open(src_path) as pdf:
            if pdf.is_encrypted:
                raise PermissionError("encrypted_pdf_requires_unlock")

            page_count = len(pdf.pages)
            for rec in annotations:
                if rec.page_index < 0 or rec.page_index >= page_count:
                    raise ValueError(f"invalid_page_index: {rec.page_index}")
                if rec.kind not in VALID_KINDS:
                    raise ValueError(f"invalid_annotation_kind: {rec.kind}")
                if rec.kind == "ink":
                    valid_strokes = [s for s in (rec.points_pdf or ()) if len(s) >= 2]
                    if not valid_strokes:
                        raise ValueError("empty_ink_strokes")

            if create_backup and (output_path is None or target == src_path):
                _write_backup_atomic(src_path)

            # Group annotations by page
            by_page: dict[int, list[AnnotationRecord]] = {}
            for rec in annotations:
                by_page.setdefault(int(rec.page_index), []).append(rec)

            id_map: dict[str, object] = {}
            pending_links: list[tuple[object, AnnotationRecord]] = []
            page_managed_aps: dict[int, list[tuple[tuple[float, float, float, float], object]]] = {}

            for page_idx, page in enumerate(pdf.pages):
                preserved = []
                for existing in page.get("/Annots", []):
                    try:
                        st = str(existing.get("/Subtype", ""))
                    except Exception:
                        st = ""
                    if st not in _MANAGED_SUBTYPES:
                        preserved.append(existing)

                recs = by_page.get(page_idx, [])
                built_annots = list(preserved)

                for rec in recs:
                    rect = _norm_rect(rec.rect_pdf)
                    if rec.kind == "ink" and (rect[2] <= rect[0] or rect[3] <= rect[1]):
                        xs = [float(p[0]) for s in rec.points_pdf for p in s]
                        ys = [float(p[1]) for s in rec.points_pdf for p in s]
                        rect = (min(xs) - 2.0, min(ys) - 2.0, max(xs) + 2.0, max(ys) + 2.0)
                    elif rect[2] <= rect[0] or rect[3] <= rect[1]:
                        rect = (rect[0], rect[1], rect[0] + 24.0, rect[1] + 24.0)

                    clean_hex = sanitize_color_hex(rec.color_hex)
                    rgb = _hex_to_rgb_floats(clean_hex)
                    author = sanitize_author(rec.author)
                    pdf_date = iso_to_pdf_date(rec.timestamp_iso)
                    annot_id = (rec.id or "").strip() or f"{rec.kind}-{uuid.uuid4().hex[:8]}"

                    subtype_name = {
                        "highlight": "/Highlight",
                        "text": "/Text",
                        "ink": "/Ink",
                        "freetext": "/FreeText",
                        "popup": "/Popup",
                    }[rec.kind]

                    d = pikepdf.Dictionary(
                        {
                            "/Type": pikepdf.Name("/Annot"),
                            "/Subtype": pikepdf.Name(subtype_name),
                            "/Rect": pikepdf.Array(list(rect)),
                            "/NM": annot_id,
                            "/C": pikepdf.Array(list(rgb)),
                            "/T": author,
                            "/M": pdf_date,
                            "/Contents": rec.content or "",
                            "/F": 4,
                        }
                    )

                    if rec.kind == "highlight":
                        x0, y0, x1, y1 = rect
                        d["/QuadPoints"] = pikepdf.Array([x0, y1, x1, y1, x0, y0, x1, y0])
                        d["/CA"] = max(0.15, min(0.85, float(rec.opacity)))
                    elif rec.kind == "text":
                        d["/Name"] = pikepdf.Name("/Comment")
                        d["/Open"] = False
                    elif rec.kind == "ink":
                        ink_list = pikepdf.Array(
                            [
                                pikepdf.Array(
                                    [coord for pt in stroke for coord in (float(pt[0]), float(pt[1]))]
                                )
                                for stroke in rec.points_pdf
                                if len(stroke) >= 2
                            ]
                        )
                        d["/InkList"] = ink_list
                        d["/BS"] = pikepdf.Dictionary(
                            {"/W": max(0.5, min(24.0, float(rec.stroke_width))), "/S": pikepdf.Name("/S")}
                        )
                    elif rec.kind == "freetext":
                        d["/DA"] = f"/Helv 10 Tf {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg"
                    elif rec.kind == "popup":
                        d["/Open"] = True

                    ap_stream = _build_ap_stream(pdf, pikepdf, rec.kind, rect, rgb, rec)
                    if ap_stream is not None:
                        d["/AP"] = pikepdf.Dictionary({"/N": ap_stream})
                        page_managed_aps.setdefault(page_idx, []).append((rect, ap_stream))

                    indirect = pdf.make_indirect(d)
                    id_map[annot_id] = indirect
                    pending_links.append((indirect, rec))
                    built_annots.append(indirect)

                if built_annots:
                    page["/Annots"] = pikepdf.Array(built_annots)
                elif "/Annots" in page:
                    del page["/Annots"]

            # Second pass: wire /IRT and /Parent /Popup relationships
            for indirect, rec in pending_links:
                if rec.parent_id and rec.parent_id in id_map:
                    parent_ref = id_map[rec.parent_id]
                    indirect["/IRT"] = parent_ref
                    indirect["/RT"] = pikepdf.Name("/R")
                    if rec.kind == "popup":
                        indirect["/Parent"] = parent_ref
                        if "/Popup" not in parent_ref:
                            parent_ref["/Popup"] = indirect

            if flatten:
                for page_idx, page in enumerate(pdf.pages):
                    ap_items = page_managed_aps.get(page_idx, [])
                    if ap_items:
                        resources = page.get("/Resources")
                        if resources is None:
                            resources = pikepdf.Dictionary()
                            page["/Resources"] = resources
                        xobjs = resources.get("/XObject")
                        if xobjs is None:
                            xobjs = pikepdf.Dictionary()
                            resources["/XObject"] = xobjs

                        draw_cmds: list[str] = []
                        for ap_idx, (rect, ap_stream) in enumerate(ap_items):
                            xname = f"/TRODAI_AP_{page_idx}_{ap_idx}_{uuid.uuid4().hex[:6]}"
                            xobjs[pikepdf.Name(xname)] = ap_stream
                            draw_cmds.append(f"q 1 0 0 1 {rect[0]:.3f} {rect[1]:.3f} cm {xname} Do Q")

                        overlay_stream = pdf.make_indirect(
                            pikepdf.Stream(pdf, ("\n".join(draw_cmds) + "\n").encode("ascii"))
                        )
                        existing_contents = page.get("/Contents")
                        if existing_contents is None:
                            page["/Contents"] = overlay_stream
                        elif isinstance(existing_contents, pikepdf.Array):
                            existing_contents.append(overlay_stream)
                        else:
                            page["/Contents"] = pikepdf.Array([existing_contents, overlay_stream])

                    # Strip managed annotations from /Annots
                    remaining = []
                    for a in page.get("/Annots", []):
                        try:
                            if str(a.get("/Subtype", "")) not in _MANAGED_SUBTYPES:
                                remaining.append(a)
                        except Exception:
                            remaining.append(a)
                    if remaining:
                        page["/Annots"] = pikepdf.Array(remaining)
                    elif "/Annots" in page:
                        del page["/Annots"]

            pdf.save(str(tmp_path), linearize=False)

        # Replace target AFTER closing pikepdf handle to src_path (required on Windows)
        os.replace(tmp_path, target)

    except PermissionError:
        raise
    except pikepdf.PasswordError as exc:
        raise PermissionError("encrypted_pdf_requires_unlock") from exc
    except pikepdf.PdfError as exc:
        raise ValueError(f"invalid_pdf: {exc}") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return target
