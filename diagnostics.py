"""Detects which optional pieces of the local environment are available.

Pure, side-effect-free checks used by the diagnostics panel at startup
and to locate external tools.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import shutil
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def find_ffmpeg() -> Optional[str]:
    """PATH first, then the winget install folder (PATH changes need a new terminal)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"
    )
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def check_ffmpeg() -> CheckResult:
    if find_ffmpeg():
        return CheckResult("ffmpeg", True, "Instalado.")
    return CheckResult(
        "ffmpeg",
        False,
        "No encontrado. Necesario para GRABAR reuniones. "
        "Instala con 'winget install --id Gyan.FFmpeg -e' y reinicia esta app.",
    )


def check_whisper(model_size: str) -> CheckResult:
    if importlib.util.find_spec("faster_whisper") is None:
        return CheckResult(
            "Whisper", False, "Falta. Ejecuta '.venv\\Scripts\\pip install faster-whisper'."
        )
    from transcription.engine import model_cached

    if model_cached(model_size):
        return CheckResult("Whisper", True, f"Modelo '{model_size}' listo (transcripción local, sin internet).")
    return CheckResult(
        "Whisper",
        True,
        f"Instalado. El modelo '{model_size}' se descargará la primera vez que transcribas (una sola vez).",
    )


def check_audio_devices() -> CheckResult:
    if importlib.util.find_spec("soundcard") is None:
        return CheckResult(
            "Audio", False, "Falta 'soundcard'. Ejecuta '.venv\\Scripts\\pip install soundcard'."
        )
    from meetings.devices import list_microphones, list_outputs

    mics, outputs = list_microphones(), list_outputs()
    if not mics:
        return CheckResult("Audio", False, "No se detectó ningún micrófono.")
    return CheckResult(
        "Audio",
        True,
        f"{len(mics)} micrófono(s): {', '.join(mics)}. {len(outputs)} salida(s) de audio.",
    )


def check_exiftool() -> CheckResult:
    if shutil.which("exiftool"):
        return CheckResult("exiftool", True, "Instalado.")
    return CheckResult(
        "exiftool",
        False,
        "Opcional. Sin él, las imágenes se convierten sin metadatos EXIF. "
        "Instala con 'winget install --id OliverBetz.ExifTool -e'.",
    )


def check_ocr_plugin() -> CheckResult:
    if importlib.util.find_spec("markitdown_ocr") is not None:
        return CheckResult(
            "plugin OCR",
            True,
            "Instalado. Para usarlo marca 'Habilitar plugins' y pon tu API key en Opciones.",
        )
    return CheckResult(
        "plugin OCR",
        False,
        "Opcional. Lee texto de PDFs escaneados e imágenes dentro de Word/PowerPoint/Excel "
        "(usa OpenAI, con costo). Instala con '.venv\\Scripts\\pip install -e packages/markitdown-ocr'.",
    )


# extra name -> (module to try importing, human label)
_EXTRA_MODULES = {
    "pdf": ("pdfminer", "PDF"),
    "docx": ("mammoth", "Word (.docx)"),
    "xlsx": ("openpyxl", "Excel (.xlsx)"),
    "xls": ("xlrd", "Excel antiguo (.xls)"),
    "pptx": ("pptx", "PowerPoint (.pptx)"),
    "outlook": ("olefile", "Outlook (.msg)"),
    "youtube-transcription": ("youtube_transcript_api", "Transcripción de YouTube"),
}


def check_extras() -> list[CheckResult]:
    results = []
    for extra, (module_name, label) in _EXTRA_MODULES.items():
        found = importlib.util.find_spec(module_name) is not None
        detail = (
            f"{label}: instalado."
            if found
            else f"{label}: falta. Ejecuta 'pip install markitdown[{extra}]'."
        )
        results.append(CheckResult(extra, found, detail))
    return results


def run_all_checks(model_size: str = "small") -> list[CheckResult]:
    return [
        check_whisper(model_size),
        check_ffmpeg(),
        check_audio_devices(),
        check_exiftool(),
        check_ocr_plugin(),
        *check_extras(),
    ]
