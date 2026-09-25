"""Constants and helpers shared by the UI tabs."""

from __future__ import annotations

import os
from pathlib import Path

import gradio as gr

from meetings import store

LANGUAGE_CHOICES = [
    ("Español (México)", "es-MX"),
    ("Español (España)", "es-ES"),
    ("Español (EE. UU.)", "es-US"),
    ("Inglés (EE. UU.)", "en-US"),
    ("Inglés (Reino Unido)", "en-GB"),
    ("Portugués (Brasil)", "pt-BR"),
    ("Francés", "fr-FR"),
    ("Alemán", "de-DE"),
    ("Italiano", "it-IT"),
    ("Detectar automáticamente", "auto"),
]

STATUS_LABELS = {
    store.RECORDING: "🔴 Grabando",
    store.PENDING: "⏳ En cola",
    store.TRANSCRIBING: "⚙️ Transcribiendo",
    store.DONE: "✅ Lista",
    store.ERROR: "❌ Error",
}

# Runs in the browser: copies the (hidden) textbox value and passes it on to Python.
COPY_JS = "(text) => { navigator.clipboard.writeText(text || ''); return text; }"

_SUMMARY_PROMPT = """Resume la siguiente transcripción de una reunión. Entrega:
1. Resumen ejecutivo (3 a 5 líneas).
2. Temas tratados.
3. Decisiones tomadas.
4. Tareas y acuerdos (con responsable y fecha, si se mencionan).
5. Pendientes y preguntas abiertas.
{speakers}Si algo de la transcripción no se entiende, indícalo en lugar de inventarlo.

TRANSCRIPCIÓN:

"""


def summary_prompt(meeting: store.Meeting, transcript: str) -> str:
    speakers = (
        'En la transcripción, "Tú" soy yo y "Participantes" son las demás personas de la llamada.\n'
        if meeting.mode == store.MODE_WEB
        else ""
    )
    return _SUMMARY_PROMPT.format(speakers=speakers) + transcript


def notify_copied(_text: str) -> None:
    gr.Info("Copiado al portapapeles. Pégalo en tu IA con Ctrl+V.")


def open_folder(path: Path) -> None:
    if not path.exists():
        raise gr.Error("La carpeta ya no existe.")
    os.startfile(path)  # noqa: S606 - local desktop app, opens Explorer


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024**2:
        return f"{num_bytes / 1024:.0f} KB"
    if num_bytes < 1024**3:
        return f"{num_bytes / 1024**2:.1f} MB"
    return f"{num_bytes / 1024**3:.2f} GB"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
