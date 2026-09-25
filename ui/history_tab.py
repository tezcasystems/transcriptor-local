"""Tab: browse, search and manage past meetings."""

from __future__ import annotations

from datetime import timedelta

import gradio as gr

from meetings import store
from meetings.recorder import recorder
from meetings.transcriber import ensure_listen_file, transcription_queue
from settings import MEETINGS_DIR, load_config
from transcription.engine import MODEL_CHOICES, better_model
from ui.common import (
    COPY_JS,
    STATUS_LABELS,
    format_duration,
    format_size,
    notify_copied,
    open_folder,
    summary_prompt,
)


def _matches(meeting: store.Meeting, query: str) -> bool:
    return query in meeting.title.lower() or query in meeting.read_transcript().lower()


def _row(meeting: store.Meeting) -> list[str]:
    status = STATUS_LABELS.get(meeting.status, meeting.status)
    if meeting.interrupted:
        status += " · interrumpida"
    return [
        f"{meeting.started:%d/%m/%Y %H:%M}",
        meeting.title,
        "Web" if meeting.mode == store.MODE_WEB else "Presencial",
        format_duration(meeting.duration),
        status,
    ]


def _footer(count: int) -> str:
    days = load_config().retention_days
    policy = f"el audio se borra a los {days} días" if days else "el audio se conserva siempre"
    return (
        f"<small>📁 `{MEETINGS_DIR}` · {count} reuniones · "
        f"{format_size(store.folder_size(MEETINGS_DIR))} usados · {policy} "
        "(las transcripciones se conservan siempre).</small>"
    )


def on_refresh(query):
    query = (query or "").strip().lower()
    meetings = [m for m in store.list_meetings() if not query or _matches(m, query)]
    rows = [_row(m) for m in meetings]
    listing = {"rows": rows, "ids": [m.id for m in meetings]}
    return gr.Dataframe(value=rows), listing, _footer(len(meetings))


def _busy(meeting_id: str) -> bool:
    progress = transcription_queue.progress()
    return (
        recorder.status().meeting_id == meeting_id
        or (progress is not None and progress.meeting_id == meeting_id)
        or meeting_id in transcription_queue.queued_ids()
    )


def _detail(meeting_id: str):
    meeting = store.get(meeting_id)
    if meeting is None:
        raise gr.Error("Esa reunión ya no existe. Pulsa Actualizar.")
    days = load_config().retention_days
    if meeting.audio_deleted:
        audio = "🔇 Audio borrado (política de retención)"
    elif days:
        audio = f"🔊 Audio disponible hasta el {meeting.started + timedelta(days=days):%d/%m/%Y}"
    else:
        audio = "🔊 Audio disponible"
    info = (
        f"**{meeting.started:%d/%m/%Y %H:%M}** · {'Web' if meeting.mode == store.MODE_WEB else 'Presencial'} · "
        f"{format_duration(meeting.duration)} · {STATUS_LABELS.get(meeting.status, meeting.status)} · "
        f"Modelo: {meeting.model_size} · {audio}"
    )
    if meeting.status == store.ERROR:
        info += f"\n\n❌ **Error:** {meeting.error}"
    elif meeting.status != store.DONE:
        info += "\n\n⏳ Aún no está lista; la transcripción aparecerá aquí cuando termine."
    transcript = meeting.read_transcript()
    can_retranscribe = not meeting.audio_deleted and meeting.status in (store.DONE, store.ERROR)
    listen = None
    if not meeting.audio_deleted and meeting.status == store.DONE:
        ensure_listen_file(meeting)
        listen = str(meeting.listen_path) if meeting.listen_path.exists() else None
    return (
        gr.Audio(value=listen, visible=bool(listen)),
        gr.Column(visible=True),
        f"### {meeting.title}",
        info,
        transcript,
        summary_prompt(meeting, transcript) if transcript else "",
        gr.DownloadButton(value=str(meeting.transcript_path) if transcript else None, visible=bool(transcript)),
        gr.Row(visible=can_retranscribe),
        gr.Dropdown(value=better_model(meeting.model_size)),
        meeting.id,
        gr.Button(visible=False),
    )


def on_select(evt: gr.SelectData, listing):
    rows, ids = listing["rows"], listing["ids"]
    # The user can sort the table by clicking a header, so the clicked position
    # may not match our list order; the clicked row's values always do.
    if evt.row_value in rows:
        return _detail(ids[rows.index(evt.row_value)])
    return _detail(ids[evt.index[0]])


def on_retranscribe(meeting_id, model_size):
    if not meeting_id:
        raise gr.Error("Elige una reunión.")
    if _busy(meeting_id):
        raise gr.Error("Esta reunión ya se está procesando.")
    try:
        transcription_queue.retranscribe(meeting_id, model_size)
    except ValueError as exc:
        raise gr.Error(str(exc))
    gr.Info("En cola para re-transcribir. Verás el resultado aquí cuando termine.")
    return _detail(meeting_id)


def on_ask_delete():
    return gr.Button(visible=True)


def on_confirm_delete(meeting_id, query):
    meeting = store.get(meeting_id) if meeting_id else None
    if meeting is None:
        raise gr.Error("Elige una reunión.")
    if _busy(meeting_id):
        raise gr.Error("No se puede eliminar mientras se graba o transcribe.")
    store.delete(meeting)
    gr.Info(f"Se eliminó «{meeting.title}».")
    table, listing, footer = on_refresh(query)
    return table, listing, footer, gr.Column(visible=False), "", gr.Button(visible=False)


def on_open(meeting_id):
    meeting = store.get(meeting_id) if meeting_id else None
    if meeting is None:
        raise gr.Error("Elige una reunión.")
    open_folder(meeting.folder)


def build(tab: gr.Tab) -> None:
    listing_state = gr.State({"rows": [], "ids": []})
    selected_state = gr.State("")

    with gr.Row():
        search_in = gr.Textbox(
            placeholder="Buscar por título o por lo que se dijo en la reunión…",
            show_label=False,
            scale=5,
        )
        refresh_btn = gr.Button("🔄 Actualizar", scale=1)
    table = gr.Dataframe(
        headers=["Fecha", "Título", "Tipo", "Duración", "Estado"],
        datatype=["str", "str", "str", "str", "str"],
        interactive=False,
        wrap=True,
        max_height=320,
    )
    footer = gr.Markdown()

    with gr.Column(visible=False) as detail_col:
        title_out = gr.Markdown()
        info_out = gr.Markdown()
        audio_out = gr.Audio(label="🎧 Escuchar la reunión", type="filepath", interactive=False, visible=False)
        transcript_out = gr.Textbox(label="Transcripción", lines=16, max_lines=30, buttons=["copy"])
        prompt_hidden = gr.Textbox(visible="hidden")  # must stay in the DOM for the copy JS
        with gr.Row():
            copy_prompt_btn = gr.Button("🤖 Copiar con instrucción de resumen", variant="primary")
            download_btn = gr.DownloadButton("⬇️ Descargar .md")
            open_btn = gr.Button("📂 Abrir carpeta")
        with gr.Row(visible=False) as retranscribe_row:
            model_in = gr.Dropdown(MODEL_CHOICES, value="small", label="Re-transcribir con el modelo", scale=3)
            retranscribe_btn = gr.Button("🔁 Re-transcribir", scale=1)
        with gr.Row():
            delete_btn = gr.Button("🗑️ Eliminar reunión", variant="secondary", size="sm")
            confirm_btn = gr.Button(
                "⚠️ Sí, eliminar audio y transcripción", variant="stop", size="sm", visible=False
            )

    detail_outputs = [
        audio_out,
        detail_col,
        title_out,
        info_out,
        transcript_out,
        prompt_hidden,
        download_btn,
        retranscribe_row,
        model_in,
        selected_state,
        confirm_btn,
    ]
    list_outputs = [table, listing_state, footer]

    tab.select(on_refresh, inputs=[search_in], outputs=list_outputs)
    refresh_btn.click(on_refresh, inputs=[search_in], outputs=list_outputs)
    search_in.submit(on_refresh, inputs=[search_in], outputs=list_outputs)
    table.select(on_select, inputs=[listing_state], outputs=detail_outputs)
    copy_prompt_btn.click(notify_copied, inputs=[prompt_hidden], js=COPY_JS)
    open_btn.click(on_open, inputs=[selected_state])
    retranscribe_btn.click(on_retranscribe, inputs=[selected_state, model_in], outputs=detail_outputs)
    delete_btn.click(on_ask_delete, outputs=[confirm_btn])
    confirm_btn.click(
        on_confirm_delete,
        inputs=[selected_state, search_in],
        outputs=[*list_outputs, detail_col, selected_state, confirm_btn],
    )
