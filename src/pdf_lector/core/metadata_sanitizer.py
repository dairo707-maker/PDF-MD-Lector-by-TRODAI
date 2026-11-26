"""Genuine destructive PDF metadata, XMP, and hidden application-data sanitizer."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
import uuid

import pikepdf


@dataclass(frozen=True)
class MetadataAudit:
    info_keys: list[str]
    has_xmp_metadata: bool
    xmp_bytes_len: int
    piece_info_locations: list[str]
    xmp_locations: list[str]
    has_struct_tree: bool
    has_spider_info: bool
    embedded_files_count: int


@dataclass(frozen=True)
class SanitizeOptions:
    strip_info: bool = True
    strip_xmp: bool = True
    strip_piece_info: bool = True
    strip_hidden_app_data: bool = True  # /SpiderInfo, /Measure, /VP
    strip_struct_tree: bool = False  # opt-in to preserve accessibility tags by default
    strip_embedded_files: bool = False  # opt-in


@dataclass(frozen=True)
class SanitizeResult:
    output_path: Path
    removed_info_keys: list[str]
    removed_xmp_streams: int
    removed_piece_info_count: int
    removed_struct_tree: bool
    remaining_info_keys: list[str]
    remaining_xmp: bool
    remaining_piece_info: int


def _stream_len(obj: Any) -> int:
    try:
        if isinstance(obj, pikepdf.Stream):
            return len(bytes(obj.read_bytes()))
    except Exception:
        pass
    return 0


def _walk_resources_dicts(
    res_obj: Any,
    location: str,
    visited: set[tuple[int, int]],
    callback: Any,
) -> None:
    if not isinstance(res_obj, pikepdf.Dictionary):
        return
    objgen = getattr(res_obj, "objgen", (0, 0))
    if objgen != (0, 0):
        if objgen in visited:
            return
        visited.add(objgen)

    callback(res_obj, location)

    for subkey in ("/XObject", "/Font", "/Properties", "/Pattern", "/Shading", "/ExtGState"):
        sub = res_obj.get(subkey)
        if not isinstance(sub, pikepdf.Dictionary):
            continue
        for name, target in list(sub.items()):
            if not isinstance(target, (pikepdf.Dictionary, pikepdf.Stream)):
                continue
            t_gen = getattr(target, "objgen", (0, 0))
            if t_gen != (0, 0):
                if t_gen in visited:
                    continue
                visited.add(t_gen)
            child_loc = f"{location}{subkey}{name}"
            callback(target, child_loc)
            nested_res = target.get("/Resources")
            if isinstance(nested_res, pikepdf.Dictionary):
                _walk_resources_dicts(nested_res, f"{child_loc}/Resources", visited, callback)


def _walk_annots(
    annots_obj: Any,
    location: str,
    visited: set[tuple[int, int]],
    callback: Any,
) -> None:
    if not isinstance(annots_obj, pikepdf.Array):
        return
    for idx, annot in enumerate(annots_obj):
        if not isinstance(annot, pikepdf.Dictionary):
            continue
        og = getattr(annot, "objgen", (0, 0))
        if og != (0, 0):
            if og in visited:
                continue
            visited.add(og)
        annot_loc = f"{location}[{idx}]"
        callback(annot, annot_loc)

        popup = annot.get("/Popup")
        if isinstance(popup, pikepdf.Dictionary):
            pog = getattr(popup, "objgen", (0, 0))
            if pog == (0, 0) or pog not in visited:
                if pog != (0, 0):
                    visited.add(pog)
                callback(popup, f"{annot_loc}/Popup")

        ap = annot.get("/AP")
        if isinstance(ap, pikepdf.Dictionary):
            for ap_key, ap_val in list(ap.items()):
                if isinstance(ap_val, pikepdf.Stream):
                    aog = getattr(ap_val, "objgen", (0, 0))
                    if aog == (0, 0) or aog not in visited:
                        if aog != (0, 0):
                            visited.add(aog)
                        ap_loc = f"{annot_loc}/AP{ap_key}"
                        callback(ap_val, ap_loc)
                        ap_res = ap_val.get("/Resources")
                        if isinstance(ap_res, pikepdf.Dictionary):
                            _walk_resources_dicts(ap_res, f"{ap_loc}/Resources", visited, callback)
                elif isinstance(ap_val, pikepdf.Dictionary):
                    for state_key, state_obj in list(ap_val.items()):
                        if isinstance(state_obj, (pikepdf.Stream, pikepdf.Dictionary)):
                            sog = getattr(state_obj, "objgen", (0, 0))
                            if sog == (0, 0) or sog not in visited:
                                if sog != (0, 0):
                                    visited.add(sog)
                                s_loc = f"{annot_loc}/AP{ap_key}{state_key}"
                                callback(state_obj, s_loc)
                                s_res = state_obj.get("/Resources")
                                if isinstance(s_res, pikepdf.Dictionary):
                                    _walk_resources_dicts(s_res, f"{s_loc}/Resources", visited, callback)


def inspect_pdf_metadata(pdf_path: Path) -> MetadataAudit:
    """Inspect /Info, XMP /Metadata, /PieceInfo, /SpiderInfo, /StructTreeRoot, and embedded files."""
    p = Path(pdf_path)
    info_keys: list[str] = []
    xmp_locations: list[str] = []
    piece_info_locations: list[str] = []
    xmp_bytes_len = 0
    has_struct_tree = False
    has_spider_info = False
    embedded_files_count = 0

    with pikepdf.Pdf.open(p) as pdf:
        # Never access pdf.docinfo (which lazily creates /Info); inspect trailer directly.
        if "/Info" in pdf.trailer:
            info_obj = pdf.trailer.get("/Info")
            if isinstance(info_obj, pikepdf.Dictionary):
                info_keys = sorted(str(k) for k in info_obj.keys())

        root = pdf.Root
        if "/Metadata" in root:
            xmp_locations.append("Catalog")
            xmp_bytes_len += _stream_len(root.get("/Metadata"))
        if "/PieceInfo" in root:
            piece_info_locations.append("Catalog")
        if "/SpiderInfo" in root:
            has_spider_info = True
        if "/StructTreeRoot" in root:
            has_struct_tree = True

        names = root.get("/Names")
        if isinstance(names, pikepdf.Dictionary):
            ef = names.get("/EmbeddedFiles")
            if isinstance(ef, pikepdf.Dictionary):
                ef_names = ef.get("/Names")
                if isinstance(ef_names, pikepdf.Array):
                    embedded_files_count = len(ef_names) // 2

        visited: set[tuple[int, int]] = set()

        def _inspect_obj(obj: Any, loc: str) -> None:
            nonlocal xmp_bytes_len
            if "/Metadata" in obj:
                xmp_locations.append(loc)
                xmp_bytes_len += _stream_len(obj.get("/Metadata"))
            if "/PieceInfo" in obj:
                piece_info_locations.append(loc)

        for idx, page in enumerate(pdf.pages):
            page_loc = f"Page[{idx}]"
            if "/Metadata" in page:
                xmp_locations.append(page_loc)
                xmp_bytes_len += _stream_len(page.get("/Metadata"))
            if "/PieceInfo" in page:
                piece_info_locations.append(page_loc)
            res = page.get("/Resources")
            if isinstance(res, pikepdf.Dictionary):
                _walk_resources_dicts(res, f"{page_loc}/Resources", visited, _inspect_obj)
            annots = page.get("/Annots")
            if isinstance(annots, pikepdf.Array):
                _walk_annots(annots, f"{page_loc}/Annots", visited, _inspect_obj)

        acro = root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary):
            dr = acro.get("/DR")
            if isinstance(dr, pikepdf.Dictionary):
                _walk_resources_dicts(dr, "AcroForm/DR", visited, _inspect_obj)

    return MetadataAudit(
        info_keys=info_keys,
        has_xmp_metadata=bool(xmp_locations),
        xmp_bytes_len=xmp_bytes_len,
        piece_info_locations=piece_info_locations,
        xmp_locations=xmp_locations,
        has_struct_tree=has_struct_tree,
        has_spider_info=has_spider_info,
        embedded_files_count=embedded_files_count,
    )


def sanitize_pdf_metadata(
    input_pdf: Path,
    output_pdf: Path,
    *,
    options: SanitizeOptions | None = None,
) -> SanitizeResult:
    """Destructively strip /Info, XMP /Metadata, /PieceInfo, and incremental history."""
    opts = options or SanitizeOptions()
    in_path = Path(input_pdf)
    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    same_file = False
    try:
        same_file = in_path.resolve() == out_path.resolve()
    except OSError:
        same_file = False

    removed_info_keys: list[str] = []
    removed_xmp_streams = 0
    removed_piece_info_count = 0
    removed_struct_tree = False

    with pikepdf.Pdf.open(in_path) as pdf:
        if opts.strip_info and "/Info" in pdf.trailer:
            info_obj = pdf.trailer.get("/Info")
            if isinstance(info_obj, pikepdf.Dictionary):
                removed_info_keys = sorted(str(k) for k in info_obj.keys())
                for k in list(info_obj.keys()):
                    del info_obj[k]
            del pdf.trailer["/Info"]

        root = pdf.Root

        def _strip_obj(obj: Any, _loc: str) -> None:
            nonlocal removed_xmp_streams, removed_piece_info_count
            if opts.strip_xmp and "/Metadata" in obj:
                del obj["/Metadata"]
                removed_xmp_streams += 1
            if opts.strip_piece_info and "/PieceInfo" in obj:
                del obj["/PieceInfo"]
                removed_piece_info_count += 1
            if opts.strip_hidden_app_data:
                for hidden_key in ("/SpiderInfo", "/Measure", "/VP"):
                    if hidden_key in obj:
                        del obj[hidden_key]

        _strip_obj(root, "Catalog")

        if opts.strip_struct_tree:
            if "/StructTreeRoot" in root:
                del root["/StructTreeRoot"]
                removed_struct_tree = True
            if "/MarkInfo" in root:
                del root["/MarkInfo"]

        if opts.strip_embedded_files:
            names = root.get("/Names")
            if isinstance(names, pikepdf.Dictionary) and "/EmbeddedFiles" in names:
                del names["/EmbeddedFiles"]
            if "/AF" in root:
                del root["/AF"]

        visited: set[tuple[int, int]] = set()
        for idx, page in enumerate(pdf.pages):
            _strip_obj(page, f"Page[{idx}]")
            if opts.strip_embedded_files and "/AF" in page:
                del page["/AF"]
            res = page.get("/Resources")
            if isinstance(res, pikepdf.Dictionary):
                _walk_resources_dicts(res, f"Page[{idx}]/Resources", visited, _strip_obj)
            annots = page.get("/Annots")
            if isinstance(annots, pikepdf.Array):
                _walk_annots(annots, f"Page[{idx}]/Annots", visited, _strip_obj)

        acro = root.get("/AcroForm")
        if isinstance(acro, pikepdf.Dictionary):
            dr = acro.get("/DR")
            if isinstance(dr, pikepdf.Dictionary):
                _walk_resources_dicts(dr, "AcroForm/DR", visited, _strip_obj)

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

    if same_file:
        os.replace(tmp_target, out_path)

    post_audit = inspect_pdf_metadata(out_path)
    return SanitizeResult(
        output_path=out_path,
        removed_info_keys=removed_info_keys,
        removed_xmp_streams=removed_xmp_streams,
        removed_piece_info_count=removed_piece_info_count,
        removed_struct_tree=removed_struct_tree,
        remaining_info_keys=post_audit.info_keys,
        remaining_xmp=post_audit.has_xmp_metadata,
        remaining_piece_info=len(post_audit.piece_info_locations),
    )
