"""Dual-profile e-invoice tools (4 tools): create_visual_invoice, create_einvoice, validate_einvoice, pdf_to_einvoice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..invoice_engine import (
    build_invoice_payload,
    calculate_dian_cufe,
    create_einvoice,
    create_visual_invoice_pdf,
    pdf_to_einvoice,
    validate_einvoice_file,
)
from ..win32_bridge import atomic_write_text
from . import (
    ToolDefinition,
    ToolParamSpec,
    ToolRegistry,
    ToolResult,
    dpi_param_spec,
    emit_progress,
    source_lang_param_spec,
    validate_output_any_path,
    validate_output_pdf_path,
)


def _error_result(tool_id: str, reason: str, code: str = "tool_error") -> ToolResult:
    return ToolResult(
        success=False,
        output_paths=[],
        summary=f"{tool_id}: {reason}",
        details={"error": code, "reason": reason},
    )


def _handle_create_visual_invoice(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    del inputs
    ok_out, out_path, err_out = validate_output_pdf_path(output)
    if not ok_out or out_path is None:
        return _error_result("create_visual_invoice", err_out, "invalid_output")

    emit_progress(progress_cb, 20, "Validating visual invoice fields")
    try:
        payload = build_invoice_payload(params)
        cufe = calculate_dian_cufe(payload) if payload.profile == "dian_ubl21" else ""
        saved = create_visual_invoice_pdf(
            payload,
            out_path,
            cufe=cufe,
            logo_path=params.get("logo_path"),
            source_lang=str(params["source_lang"]),
        )
    except Exception as exc:
        return _error_result("create_visual_invoice", str(exc), "invoice_creation_failed")

    emit_progress(progress_cb, 100, "Visual invoice PDF created")
    return ToolResult(
        success=True,
        output_paths=[saved],
        summary=f"Created visual invoice PDF {saved.name}",
        details={
            "profile": payload.profile,
            "invoice_number": payload.invoice_number,
            "subtotal": payload.subtotal,
            "tax_total": payload.tax_total,
            "total": payload.grand_total,
            "currency": payload.currency,
            "dpi": int(params["dpi"]),
            "source_lang": str(params["source_lang"]),
        },
    )


def _handle_create_einvoice(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    del inputs
    ok_out, out_path, err_out = validate_output_any_path(output)
    if not ok_out or out_path is None:
        return _error_result("create_einvoice", err_out, "invalid_output")

    emit_progress(progress_cb, 20, "Building e-invoice payload")
    try:
        outputs, details = create_einvoice(params, out_path)
    except Exception as exc:
        return _error_result("create_einvoice", str(exc), "einvoice_creation_failed")

    details["dpi"] = int(params["dpi"])
    emit_progress(progress_cb, 100, "E-invoice generated")
    return ToolResult(
        success=True,
        output_paths=outputs,
        summary=f"Generated {details.get('profile')} e-invoice ({len(outputs)} file(s))",
        details=details,
    )


def _handle_validate_einvoice(
    inputs: list[Path],
    output: Path | None,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    in_path = inputs[0]
    profile = str(params.get("profile") or "auto")
    emit_progress(progress_cb, 25, f"Validating {in_path.name}")

    try:
        report = validate_einvoice_file(in_path, profile=profile)
    except Exception as exc:
        return _error_result("validate_einvoice", str(exc), "validation_failed")

    out_paths: list[Path] = []
    if output is not None:
        ok_out, out_p, err_out = validate_output_any_path(output)
        if not ok_out or out_p is None:
            return _error_result("validate_einvoice", err_out, "invalid_output")
        rep_file = (out_p / "einvoice_validation.json") if (out_p.exists() and out_p.is_dir()) else out_p
        payload = {
            "valid": report.valid,
            "profile": report.profile,
            "errors": report.errors,
            "warnings": report.warnings,
            "summary": report.summary,
        }
        atomic_write_text(rep_file, json.dumps(payload, indent=2, ensure_ascii=False))
        out_paths.append(rep_file)

    emit_progress(progress_cb, 100, "Validation complete")
    details = {
        "valid": report.valid,
        "profile": report.profile,
        "errors": report.errors,
        "warnings": report.warnings,
        **report.summary,
    }
    return ToolResult(
        success=report.valid,
        output_paths=out_paths,
        summary=f"E-invoice validation {'PASSED' if report.valid else 'FAILED'} ({report.profile})",
        details=details,
    )


def _handle_pdf_to_einvoice(
    inputs: list[Path],
    output: Path,
    params: dict[str, Any],
    progress_cb: Any = None,
) -> ToolResult:
    ok_out, out_path, err_out = validate_output_any_path(output)
    if not ok_out or out_path is None:
        return _error_result("pdf_to_einvoice", err_out, "invalid_output")

    in_path = inputs[0]
    mode = str(params["mode"])
    profile = str(params["profile"])
    dpi = int(params["dpi"])
    source_lang = str(params["source_lang"])
    consent_granted = bool(params["consent_granted"])
    endpoint_url = str(params.get("endpoint_url") or "")

    emit_progress(progress_cb, 20, f"Extracting invoice fields ({mode})")
    try:
        outputs, details = pdf_to_einvoice(
            in_path,
            out_path,
            mode=mode,
            profile=profile,
            dpi=dpi,
            source_lang=source_lang,
            consent_granted=consent_granted,
            endpoint_url=endpoint_url,
            ocr_provider=params.get("ocr_provider"),
            http_post_fn=params.get("http_post_fn"),
        )
    except PermissionError as exc:
        return _error_result("pdf_to_einvoice", str(exc), "consent_required")
    except Exception as exc:
        return _error_result("pdf_to_einvoice", str(exc), "extraction_failed")

    emit_progress(progress_cb, 100, "E-invoice extracted")
    return ToolResult(
        success=True,
        output_paths=outputs,
        summary=f"Extracted PDF invoice to {profile} ({mode})",
        details=details,
    )


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        id="create_visual_invoice",
        category="invoices",
        params=[
            dpi_param_spec(150),
            source_lang_param_spec("auto"),
            ToolParamSpec("profile", "choice", "dian_ubl21", choices=("dian_ubl21", "facturx_en16931", "dian", "facturx")),
            ToolParamSpec("invoice_number", "str", "INV-2026-001"),
            ToolParamSpec("currency", "str", "COP"),
            ToolParamSpec("emitter", "str", ""),
            ToolParamSpec("nit", "str", ""),
            ToolParamSpec("customer", "str", ""),
            ToolParamSpec("customer_nit", "str", ""),
            ToolParamSpec("items", "list", []),
            ToolParamSpec("logo_path", "path", ""),
        ],
        handler=_handle_create_visual_invoice,
        icon="invoice",
        min_inputs=0,
        max_inputs=0,
        input_validator="none",
        output_kind="file",
    ),
    ToolDefinition(
        id="create_einvoice",
        category="invoices",
        params=[
            ToolParamSpec(
                "profile",
                "choice",
                "dian_ubl21",
                choices=("dian_ubl21", "facturx_en16931", "dian", "facturx", "zugferd", "xrechnung"),
            ),
            dpi_param_spec(150),
            source_lang_param_spec("auto"),
            ToolParamSpec("invoice_number", "str", ""),
            ToolParamSpec("currency", "str", ""),
            ToolParamSpec("emitter_name", "str", ""),
            ToolParamSpec("emitter_nit", "str", ""),
            ToolParamSpec("emitter_vat", "str", ""),
            ToolParamSpec("customer_name", "str", ""),
            ToolParamSpec("customer_nit", "str", ""),
            ToolParamSpec("customer_vat", "str", ""),
            ToolParamSpec("items", "list", []),
        ],
        handler=_handle_create_einvoice,
        icon="invoice",
        badge="DIAN/EN",
        min_inputs=0,
        max_inputs=0,
        input_validator="none",
        output_kind="any",
    ),
    ToolDefinition(
        id="validate_einvoice",
        category="invoices",
        params=[
            ToolParamSpec("profile", "choice", "auto", choices=("auto", "dian_ubl21", "facturx_en16931", "dian", "facturx")),
            ToolParamSpec("strict", "bool", True),
        ],
        handler=_handle_validate_einvoice,
        icon="check",
        badge="UBL/XML",
        min_inputs=1,
        max_inputs=1,
        input_validator="none",
        output_kind="any",
        allow_empty_output=True,
    ),
    ToolDefinition(
        id="pdf_to_einvoice",
        category="invoices",
        params=[
            ToolParamSpec("mode", "choice", "local", choices=("local", "online")),
            ToolParamSpec("profile", "choice", "dian_ubl21", choices=("dian_ubl21", "facturx_en16931", "dian", "facturx")),
            dpi_param_spec(200),
            source_lang_param_spec("auto"),
            ToolParamSpec("consent_granted", "bool", False),
            ToolParamSpec("endpoint_url", "str", ""),
        ],
        handler=_handle_pdf_to_einvoice,
        icon="ocr",
        badge="LOCAL/API",
        min_inputs=1,
        max_inputs=1,
        input_validator="pdf",
        output_kind="any",
    ),
)


def register_tools(registry: type[ToolRegistry] = ToolRegistry) -> None:
    """Register all 4 Dual-Profile Invoice tools in ToolRegistry."""
    for defn in TOOL_DEFINITIONS:
        registry.register_tool(defn, overwrite=True)
