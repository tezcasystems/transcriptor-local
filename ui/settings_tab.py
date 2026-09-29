"""Tab: preferences, storage and system diagnostics."""

from __future__ import annotations

import gradio as gr

from diagnostics import run_all_checks
from meetings import store
from settings import MEETINGS_DIR, RETENTION_CHOICES, load_config, update_config
from transcription.engine import MODEL_CHOICES, VOCABULARY_MODELS
from ui.common import format_size, open_folder


def _diagnostics_md() -> str:
    checks = run_all_checks(load_config().model_size)
    rows = "\n".join(f"| {'✅' if c.ok else '⚠️'} | **{c.name}** | {c.detail} |" for c in checks)
    return (
        "| | Componente | Detalle |\n|---|---|---|\n"
        + rows
        + "\n\n_Después de instalar algo, cierra y vuelve a abrir la app para que se detecte._"
    )


def _storage_md() -> str:
    return (
        f"**Carpeta:** `{MEETINGS_DIR}`  \n"
        f"**Espacio usado:** {format_size(store.folder_size(MEETINGS_DIR))} · "
        f"{len(store.list_meetings())} reuniones"
    )


def on_open_folder():
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    open_folder(MEETINGS_DIR)


def on_model_change(model_size):
    update_config(model_size=model_size)
    gr.Info(f"Listo: las próximas transcripciones usarán el modelo '{model_size}'.")
    return _diagnostics_md()


def on_vocabulary_save(vocabulary):
    vocabulary = (vocabulary or "").strip()
    update_config(vocabulary=vocabulary)
    if vocabulary and load_config().model_size not in VOCABULARY_MODELS:
        gr.Info("Vocabulario guardado. Se aplicará cuando uses el modelo 'Máxima precisión (turbo)'.")
    elif vocabulary:
        gr.Info("Vocabulario guardado: se usará en las próximas transcripciones y al re-transcribir.")
    else:
        gr.Info("Vocabulario vacío: se transcribirá sin palabras guía.")
    return vocabulary


def on_retention_change(days):
    # Not applied on the spot: a misclick on "7 días" must not wipe weeks of audio.
    update_config(retention_days=days)
    if days == 0:
        gr.Info("El audio de las reuniones se conservará siempre.")
    else:
        gr.Info(
            f"El audio con más de {days} días se borrará automáticamente al terminar la próxima "
            "transcripción o al abrir la app. Puedes cambiarlo antes si fue un error."
        )


def build(tab: gr.Tab) -> None:
    config = load_config()
    with gr.Row(equal_height=False):
        with gr.Column():
            gr.Markdown("### 🧠 Transcripción (Whisper, 100% local)")
            model_in = gr.Radio(
                MODEL_CHOICES,
                value=config.model_size,
                label="Modelo",
                info="Tiempos medidos en esta laptop. 'Equilibrado' es el recomendado para reuniones.",
            )
            vocabulary_in = gr.Textbox(
                value=config.vocabulary,
                label="Vocabulario (nombres, siglas y términos de tus reuniones)",
                info=(
                    "Solo con el modelo 'Máxima precisión (turbo)'. Uno por línea o separados por comas: "
                    "nombres de personas, clientes, productos o siglas. Mejor pocos y específicos (hasta ~50)."
                ),
                placeholder="María González\nProyecto Atlas\nCRM\nRecursos Humanos",
                lines=4,
                max_lines=10,
                max_length=650,  # Whisper keeps ~220 tokens of hints (~700 chars in Spanish)
            )
            vocabulary_btn = gr.Button("💾 Guardar vocabulario", size="sm")
            gr.Markdown("### 🗂️ Almacenamiento")
            retention_in = gr.Radio(
                RETENTION_CHOICES,
                value=config.retention_days,
                label="Borrar el audio de las reuniones después de",
                info="Solo se borra el audio; las transcripciones se conservan siempre.",
            )
            storage_out = gr.Markdown(_storage_md())
            open_btn = gr.Button("📂 Abrir carpeta de reuniones")
        with gr.Column():
            gr.Markdown("### 🩺 Diagnóstico del sistema")
            diag_out = gr.Markdown(_diagnostics_md())
            recheck_btn = gr.Button("🔄 Revisar de nuevo")

    tab.select(_storage_md, outputs=[storage_out])
    model_in.change(on_model_change, inputs=[model_in], outputs=[diag_out])
    vocabulary_btn.click(on_vocabulary_save, inputs=[vocabulary_in], outputs=[vocabulary_in])
    retention_in.change(on_retention_change, inputs=[retention_in])
    open_btn.click(on_open_folder)
    recheck_btn.click(_diagnostics_md, outputs=[diag_out])
