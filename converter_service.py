"""Wraps the MarkItDown library: builds the converter from UI settings,
runs a conversion, and translates exceptions into friendly messages.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from markitdown import (
    PRIORITY_SPECIFIC_FILE_FORMAT,
    FileConversionException,
    MarkItDown,
    MissingDependencyException,
    UnsupportedFormatException,
)
from markitdown.converters import AudioConverter

from media_converter import MEDIA_EXTENSIONS, MediaTranscriptionConverter
from transcription.engine import DEFAULT_MODEL


@dataclass
class Settings:
    enable_plugins: bool = False
    api_key: Optional[str] = None
    model: str = "gpt-4o-mini"
    language: str = "es-MX"
    model_size: str = DEFAULT_MODEL


@dataclass
class ConversionOutcome:
    source_name: str
    ok: bool
    markdown: str = ""
    title: Optional[str] = None
    warning: str = ""
    error_message: str = ""
    error_detail: str = ""


def build_markitdown(settings: Settings) -> MarkItDown:
    kwargs = {"enable_plugins": settings.enable_plugins}
    if settings.api_key:
        from openai import OpenAI

        kwargs["llm_client"] = OpenAI(api_key=settings.api_key, max_retries=3)
        kwargs["llm_model"] = settings.model
    md = MarkItDown(**kwargs)
    # The built-in AudioConverter is MarkItDown's fallback when our converter fails,
    # and it uploads the audio to Google (transcribing in English). Removing it keeps
    # audio 100% local and shows the real error instead of a wrong "success".
    # MarkItDown has no public unregister API, hence the private list.
    md._converters = [r for r in md._converters if not isinstance(r.converter, AudioConverter)]
    md.register_converter(
        MediaTranscriptionConverter(language=settings.language, model_size=settings.model_size),
        priority=PRIORITY_SPECIFIC_FILE_FORMAT - 1,
    )
    return md


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _describe_failure(exc: FileConversionException) -> str:
    attempts = [a for a in (exc.attempts or []) if a.exc_info]

    for attempt in attempts:
        if isinstance(attempt.exc_info[1], MissingDependencyException):
            return "Falta una dependencia. " + _first_line(attempt.exc_info[1])

    for attempt in attempts:
        if isinstance(attempt.converter, MediaTranscriptionConverter):
            return "No se pudo transcribir: " + _first_line(attempt.exc_info[1])

    tried = ", ".join(type(a.converter).__name__ for a in attempts) or "ninguno"
    return f"Ningún conversor pudo procesar el archivo (se intentó: {tried})."


def _empty_result_warning(extension: str) -> str:
    if extension in MEDIA_EXTENSIONS:
        return "No se obtuvo texto del audio/video."
    if extension in {".jpg", ".jpeg", ".png"}:
        return (
            "No se obtuvo texto. Para extraer texto o descripción de imágenes, "
            "configura una API key de LLM en Opciones (y/o instala exiftool para metadatos)."
        )
    return "La conversión terminó pero no se encontró texto (¿documento escaneado o vacío?)."


def convert_one(
    source: str,
    md: MarkItDown,
    is_url: bool = False,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> ConversionOutcome:
    name = source if is_url else Path(source).name
    extension = "" if is_url else Path(source).suffix.lower()
    try:
        if is_url:
            result = md.convert_url(source)
        else:
            result = md.convert_local(source, progress_callback=progress_callback)
    except FileConversionException as exc:
        return ConversionOutcome(
            source_name=name,
            ok=False,
            error_message=_describe_failure(exc),
            error_detail=str(exc),
        )
    except UnsupportedFormatException as exc:
        return ConversionOutcome(
            source_name=name,
            ok=False,
            error_message="Tipo de archivo no soportado por ningún conversor instalado.",
            error_detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
        return ConversionOutcome(
            source_name=name,
            ok=False,
            error_message=f"Error inesperado: {_first_line(exc)}",
            error_detail="".join(traceback.format_exception(*sys.exc_info())),
        )

    markdown = result.markdown or ""
    warning = "" if markdown.strip() else _empty_result_warning(extension)
    return ConversionOutcome(
        source_name=name, ok=True, markdown=markdown, title=result.title, warning=warning
    )
