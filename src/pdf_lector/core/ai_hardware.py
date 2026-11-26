"""Hardware capability probe for local generative LLMs vs lightweight BM25 / API / MCP."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
import shutil
import subprocess
import sys


@dataclass(frozen=True)
class LocalAiHardwareReport:
    """Hardware capability report governing local generative LLM eligibility."""

    ram_gb: float
    cpu_cores: int
    gpu_name: str
    gpu_vram_gb: float
    ollama_installed: bool
    eligible_for_local_llm: bool
    reason: str
    recommendation: str


def _detect_windows_ram_gb() -> float:
    if sys.platform != "win32":
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return round((pages * page_size) / (1024**3), 1)
        except Exception:
            return 0.0

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return round(stat.ullTotalPhys / (1024**3), 1)
    except Exception:
        pass
    return 0.0


def _detect_nvidia_vram() -> tuple[str, float]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return ("Integrada / Sin GPU dedicada", 0.0)
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        out = subprocess.check_output(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            creationflags=flags,
            text=True,
        )
        first = (out or "").strip().splitlines()[0]
        name_part, mem_part = [p.strip() for p in first.split(",", 1)]
        vram_gb = round(float(mem_part) / 1024.0, 1)
        return (name_part or "NVIDIA GPU", vram_gb)
    except Exception:
        return ("Integrada / Sin VRAM dedicada", 0.0)


def probe_local_ai_hardware(
    *,
    min_ram_gb: float = 16.0,
    min_vram_gb: float = 6.0,
    ram_gb_override: float | None = None,
    vram_gb_override: float | None = None,
    ollama_installed_override: bool | None = None,
) -> LocalAiHardwareReport:
    """Inspect system RAM, CPU cores, and GPU VRAM to gate local Ollama execution."""
    ram_gb = float(ram_gb_override) if ram_gb_override is not None else _detect_windows_ram_gb()
    cpu_cores = int(os.cpu_count() or 1)

    if vram_gb_override is not None:
        gpu_vram_gb = float(vram_gb_override)
        gpu_name = "GPU Dedicada" if gpu_vram_gb > 0 else "Integrada / Sin VRAM dedicada"
    else:
        gpu_name, gpu_vram_gb = _detect_nvidia_vram()

    if ollama_installed_override is not None:
        ollama_installed = bool(ollama_installed_override)
    else:
        ollama_installed = shutil.which("ollama") is not None

    has_ram = ram_gb >= min_ram_gb
    has_vram = gpu_vram_gb >= min_vram_gb
    eligible = has_ram or has_vram

    if not eligible:
        reason = (
            f"Este equipo reporta {ram_gb:.1f} GB RAM y {gpu_vram_gb:.1f} GB VRAM "
            f"(minimo recomendado para LLM local: {min_ram_gb:.0f} GB RAM o {min_vram_gb:.0f} GB VRAM)."
        )
        recommendation = (
            "Usa BM25 Local (0 GPU, incluido) o conecta una API oficial (OpenAI, Gemini, Claude) "
            "o tu Servidor MCP oficial respetando los terminos de cada proveedor."
        )
    elif not ollama_installed:
        reason = (
            f"Hardware compatible ({ram_gb:.1f} GB RAM, {gpu_vram_gb:.1f} GB VRAM), "
            "pero Ollama local no esta instalado o no esta en PATH."
        )
        recommendation = (
            "Instala Ollama localmente o usa una API oficial BYOK / Servidor MCP."
        )
    else:
        reason = f"Hardware compatible ({ram_gb:.1f} GB RAM, {gpu_vram_gb:.1f} GB VRAM) con Ollama disponible."
        recommendation = "Puedes ejecutar Ollama local o conectarte por API oficial / MCP."

    return LocalAiHardwareReport(
        ram_gb=ram_gb,
        cpu_cores=cpu_cores,
        gpu_name=gpu_name,
        gpu_vram_gb=gpu_vram_gb,
        ollama_installed=ollama_installed,
        eligible_for_local_llm=eligible,
        reason=reason,
        recommendation=recommendation,
    )
