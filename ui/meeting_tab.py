"""Tab: record a web or in-person meeting and get its transcript."""

from __future__ import annotations

import html
import math
import time
from datetime import datetime

import gradio as gr

from meetings import store
from meetings.devices import list_microphones, suggest_microphone
from meetings.hotkey import probe_hotkey
from meetings.recorder import RecorderError, recorder, test_audio
from meetings.transcriber import transcription_queue
from settings import load_config, update_config
from ui.common import (
    COPY_JS,
    LANGUAGE_CHOICES,
    format_duration,
    format_size,
    notify_copied,
    open_folder,
    summary_prompt,
)

MODE_CHOICES = [
    ("💻 Reunión web (Teams, Meet, Zoom)", store.MODE_WEB),
    ("👥 Reunión presencial", store.MODE_IN_PERSON),
]

def _tips(mode: str) -> str:
    if mode == store.MODE_IN_PERSON:
        return (
            "**Antes de empezar**\n"
            "- 💻 Deja la laptop **al centro de la mesa**, con la tapa abierta.\n"
            "- 🎙️ Usa el **micrófono de la laptop**: capta todo el cuarto (el de los audífonos solo te capta a ti).\n"
            "- 🔌 Conecta el cargador si la reunión es larga.\n"
            "- 📢 Avisa a los participantes que vas a grabar."
        )
    hotkey = probe_hotkey()
    how = f"**{hotkey}** (desde cualquier ventana) o el botón *Silenciar*" if hotkey else "el botón *Silenciar*"
    return (
        "**Antes de empezar**\n"
        "- 🎧 **Usa audífonos**: con bocinas, tu micrófono también capta a los demás. "
        "Los de **cable** suenan mejor que los Bluetooth en llamadas.\n"
        "- 🎙️ Elige el **mismo micrófono que usas en Teams** (Teams → Configuración → Dispositivos) y pulsa *Probar audio*.\n"
        f"- 🔇 Silenciarte en Teams **no** silencia esta grabación: cuando no estés hablando usa {how}.\n"
        "- 🔕 Se graba todo lo que suena en la PC: silencia música y notificaciones.\n"
        "- 📢 Avisa a los participantes que vas a grabar."
    )

# Measured on this laptop (x times faster than real time); web meetings have two tracks.
_SPEED = {"base": 11.0, "small": 3.3, "medium": 1.0}

_NO_WATCH = {"id": None, "shown": False}


def _default_title() -> str:
    return f"Reunión {datetime.now():%d-%m-%Y %H:%M}"


def _estimate_minutes(meeting: store.Meeting) -> int:
    factor = 1.3 if meeting.mode == store.MODE_WEB else 1.0
    return max(1, round(meeting.duration * factor / _SPEED.get(meeting.model_size, 3.3) / 60))


# ---- HTML rendering -----------------------------------------------------------------


def _meter(label: str, level: float, style: str = "") -> str:
    db = 20 * math.log10(max(level, 1e-4))
    pct = max(0.0, min(100.0, (db + 60) / 60 * 100))
    cls = f"mk-bar mk-bar-{style}" if style else "mk-bar"
    return (
        f'<div class="mk-meter"><span class="mk-label">{html.escape(label)}</span>'
        f'<div class="{cls}"><div style="width:{pct:.0f}%"></div></div></div>'
    )


def _recording_html() -> str:
    st = recorder.status()
    if not st.recording:
        return ""
    head = "⏸ EN PAUSA" if st.paused else '<span class="mk-dot"></span> GRABANDO'
    parts = [
        '<div class="mk-rec">',
        f'<div class="mk-head">{head}<span class="mk-time">{format_duration(st.elapsed)}</span>'
        f'<span class="mk-size">{format_size(st.size_bytes)}</span></div>',
    ]
    if st.mic_muted:
        parts.append(_meter("🔇 Tú · silenciado (no se graba tu voz)", 0.0, "muted"))
    else:
        parts.append(
            _meter(f"Tú · {st.mic_name}", st.levels.get(store.TRACK_MIC, 0.0), "alert" if st.mic_silent else "")
        )
    if store.TRACK_PC in st.levels:
        parts.append(_meter("Participantes · audio de la PC", st.levels[store.TRACK_PC]))
    if st.mic_silent and not st.mic_muted:
        parts.append(
            '<div class="mk-alert">⚠️ Tu micrófono no está enviando sonido. Revisa que no esté apagado '
            "o silenciado en Windows, o cambia de micrófono abajo.</div>"
        )
    parts += [f'<div class="mk-warn">ℹ️ {html.escape(w)}</div>' for w in st.warnings]
    if st.hotkey_label:
        parts.append(
            f'<div class="mk-note">⌨️ Silencia o activa tu micrófono desde cualquier ventana con '
            f"<b>{html.escape(st.hotkey_label)}</b> (suena un tono).</div>"
        )
    parts.append(
        '<div class="mk-note">Puedes cerrar esta pestaña del navegador: la grabación continúa. '
        "<b>No cierres la ventana negra de la app.</b></div></div>"
    )
    return "".join(parts)


def _mute_button(muted: bool, visible: bool = True, hotkey: str = "") -> gr.Button:
    suffix = f" ({hotkey})" if hotkey else ""
    if muted:
        return gr.Button(f"🎙️ Activar mi micrófono{suffix}", variant="primary", visible=visible)
    return gr.Button(f"🔇 Silenciar mi micrófono{suffix}", variant="secondary", visible=visible)


def _job_text(meeting: store.Meeting) -> str:
    progress = transcription_queue.progress()
    if progress and progress.meeting_id == meeting.id:
        eta = ""
        if progress.fraction > 0.03:
            elapsed = time.time() - progress.started
            remaining = elapsed * (1 - progress.fraction) / progress.fraction
            if remaining < 60:
                eta = f" · ≈ {max(5, round(remaining / 5) * 5)} s restantes"
            else:
                eta = f" · ≈ {round(remaining / 60)} min restantes"
        return f"⚙️ **{progress.stage}…** {progress.fraction:.0%}{eta}"
    if meeting.status == store.ERROR:
        return f"❌ **No se pudo transcribir:** {meeting.error}"
    queued = transcription_queue.queued_ids()
    ahead = queued.index(meeting.id) if meeting.id in queued else 0
    wait = f" (hay {ahead} antes)" if ahead else ""
    return f"⏳ **En cola para transcribir{wait}.** Tiempo estimado: ≈ {_estimate_minutes(meeting)} min."


# ---- event handlers -----------------------------------------------------------------


def _test_verdict(mode: str, levels: dict[str, float]) -> str:
    mic = levels.get(store.TRACK_MIC, 0.0)
    if mic == 0.0:
        lines = [
            "❌ **El micrófono no envía sonido** (apagado, silenciado o no está en uso). Elige otro o revísalo.  \n"
            "Si ningún micrófono funciona, revisa en Windows: *Configuración → Privacidad y seguridad → "
            "Micrófono →* activa **«Permitir que las aplicaciones de escritorio accedan al micrófono»**."
        ]
    elif mic < 0.01:
        lines = ["⚠️ **El micrófono se escucha muy bajo.** Habla durante la prueba o acércate."]
    else:
        lines = ["✅ **Micrófono OK.**"]
    if mode == store.MODE_WEB:
        if levels.get(store.TRACK_PC, 0.0) > 0.0:
            lines.append("✅ **Audio de la PC OK.**")
        else:
            lines.append("ℹ️ No sonó nada en la PC durante la prueba. Para verificarlo, pon un video y prueba de nuevo.")
    return "  \n".join(lines)


def on_test(mode, mic):
    if recorder.is_recording:
        raise gr.Error("Ya hay una grabación en curso.")
    if not mic:
        raise gr.Error("Elige un micrófono.")
    gr.Info("Probando 4 segundos… habla ahora.")
    return _test_verdict(mode, test_audio(mode, mic, 4.0))


def on_mode_change(mode):
    config = load_config()
    mics = list_microphones()
    return _tips(mode), gr.Dropdown(choices=mics, value=suggest_microphone(mode, config.last_mic)), ""


def on_refresh_mics(mode, current):
    mics = list_microphones()
    value = current if current in mics else suggest_microphone(mode, load_config().last_mic)
    return gr.Dropdown(choices=mics, value=value)


def on_start(mode, title, mic, language):
    if recorder.is_recording:
        raise gr.Error("Ya hay una grabación en curso.")
    if not mic:
        raise gr.Error("Elige un micrófono.")
    config = load_config()
    meeting = store.create((title or "").strip() or _default_title(), mode, mic, language, config.model_size)
    try:
        recorder.start(meeting)
    except RecorderError as exc:
        store.delete(meeting)
        raise gr.Error(str(exc))
    except Exception as exc:  # noqa: BLE001 - device errors come from the OS audio stack
        store.delete(meeting)
        raise gr.Error(f"No se pudo iniciar la grabación: {exc}")
    update_config(last_mode=mode, last_mic=mic, language=language)
    return (
        gr.Column(visible=False),
        gr.Column(visible=True),
        _recording_html(),
        gr.Button("⏸ Pausar"),
        gr.Column(visible=False),
        "",
        dict(_NO_WATCH),
        # Muting only makes sense when the PC track carries the other voices.
        _mute_button(False, visible=mode == store.MODE_WEB, hotkey=recorder.status().hotkey_label),
        False,
        gr.Dropdown(choices=list_microphones(), value=recorder.status().mic_name),
    )


def on_pause():
    if recorder.status().paused:
        recorder.resume()
        return gr.Button("⏸ Pausar"), _recording_html()
    recorder.pause()
    return gr.Button("▶️ Reanudar"), _recording_html()


def on_mute():
    if not recorder.is_recording:
        raise gr.Error("No hay una grabación en curso.")
    muted = recorder.toggle_mic_mute()
    return _mute_button(muted, hotkey=recorder.status().hotkey_label), muted, _recording_html()


def on_switch_mic(name):
    if not recorder.is_recording or not name:
        return gr.skip(), gr.skip()
    try:
        used = recorder.change_microphone(name)
    except Exception as exc:  # noqa: BLE001 - device errors come from the OS audio stack
        raise gr.Error(f"No se pudo cambiar de micrófono: {exc}")
    gr.Info(f"Ahora se graba con: {used}")
    return _recording_html(), gr.Dropdown(value=used)


def on_refresh_switch():
    return gr.Dropdown(choices=list_microphones(), value=recorder.status().mic_name or None)


def on_stop():
    meeting = recorder.stop()
    if meeting is None:
        return gr.Column(visible=True), gr.Column(visible=False), "", dict(_NO_WATCH), ""
    transcription_queue.enqueue(meeting.id)
    gr.Info(f"Grabación guardada ({format_duration(meeting.duration)}). Se está transcribiendo en segundo plano.")
    return (
        gr.Column(visible=True),
        gr.Column(visible=False),
        _job_text(meeting),
        {"id": meeting.id, "shown": False},
        "",
    )


def _mute_sync(shown_muted):
    """The hotkey can toggle mute outside the browser: refresh the button only when it changed."""
    st = recorder.status()
    if not st.recording or st.mic_muted == shown_muted:
        return gr.skip(), gr.skip()
    return _mute_button(st.mic_muted, hotkey=st.hotkey_label), st.mic_muted


def on_tick(watch, shown_muted):
    rec_html = _recording_html() if recorder.is_recording else gr.skip()
    mute = _mute_sync(shown_muted)
    skip7 = (gr.skip(),) * 7
    meeting = store.get(watch["id"]) if watch and watch.get("id") else None
    if meeting is None or watch.get("shown"):
        return rec_html, gr.skip(), *skip7, *mute
    if meeting.status != store.DONE:
        return rec_html, _job_text(meeting), *skip7, *mute
    transcript = meeting.read_transcript()
    listen = meeting.listen_path
    return (
        rec_html,
        f"✅ **Transcripción lista.** También quedó guardada en el Historial y en `{meeting.folder}`.",
        gr.Column(visible=True),
        f"### 📝 {meeting.title}",
        transcript,
        summary_prompt(meeting, transcript),
        gr.DownloadButton(value=str(meeting.transcript_path)),
        gr.Audio(value=str(listen) if listen.exists() else None, visible=listen.exists()),
        {"id": meeting.id, "shown": True},
        *mute,
    )


def on_load():
    config = load_config()
    st = recorder.status()
    watch = dict(_NO_WATCH)
    job = ""
    for meeting in store.list_meetings()[:5]:
        if meeting.status in (store.PENDING, store.TRANSCRIBING):
            watch = {"id": meeting.id, "shown": False}
            job = _job_text(meeting)
            break
    mode = config.last_mode
    mics = list_microphones()
    web_recording = st.recording and store.TRACK_PC in st.levels
    return (
        gr.Column(visible=not st.recording),
        gr.Column(visible=st.recording),
        _recording_html(),
        gr.Button("▶️ Reanudar" if st.paused else "⏸ Pausar"),
        job,
        watch,
        gr.Dropdown(choices=mics, value=suggest_microphone(mode, config.last_mic)),
        _mute_button(st.mic_muted, visible=web_recording, hotkey=st.hotkey_label),
        st.mic_muted,
        gr.Dropdown(choices=mics, value=st.mic_name or None),
    )


def on_open_folder(watch):
    meeting = store.get(watch["id"]) if watch and watch.get("id") else None
    if meeting is None:
        raise gr.Error("No hay una reunión seleccionada.")
    open_folder(meeting.folder)


# ---- layout -------------------------------------------------------------------------


def build(demo: gr.Blocks) -> None:
    config = load_config()
    watch_state = gr.State(dict(_NO_WATCH))
    muted_state = gr.State(False)

    with gr.Column(visible=True) as setup_col:
        with gr.Row(equal_height=False):
            with gr.Column(scale=3):
                mode_in = gr.Radio(MODE_CHOICES, value=config.last_mode, label="Tipo de reunión")
                title_in = gr.Textbox(
                    label="Título (opcional)",
                    placeholder="Ej.: Junta semanal de ventas — si lo dejas vacío se usa la fecha y hora",
                )
                with gr.Row():
                    mic_in = gr.Dropdown(
                        label="🎙️ Micrófono",
                        choices=list_microphones(),
                        value=suggest_microphone(config.last_mode, config.last_mic),
                        scale=5,
                    )
                    refresh_btn = gr.Button("🔄", scale=0, min_width=50)
                language_in = gr.Dropdown(LANGUAGE_CHOICES, value=config.language, label="Idioma de la reunión")
                with gr.Row():
                    test_btn = gr.Button("🔈 Probar audio (4 s)", scale=1)
                    start_btn = gr.Button("⏺ Iniciar grabación", variant="primary", size="lg", scale=2)
                test_out = gr.Markdown()
            with gr.Column(scale=2):
                tips_out = gr.Markdown(_tips(config.last_mode))

    with gr.Column(visible=False) as rec_col:
        rec_html = gr.HTML()
        mute_btn = _mute_button(False)
        with gr.Row():
            pause_btn = gr.Button("⏸ Pausar", size="lg")
            stop_btn = gr.Button("⏹ Detener y transcribir", variant="stop", size="lg")
        with gr.Accordion("🎙️ Cambiar de micrófono sin detener", open=False):
            with gr.Row():
                switch_mic_in = gr.Dropdown(
                    label="Micrófono en uso",
                    info="Si conectas otros audífonos, pulsa 🔄 para verlos y elígelos aquí.",
                    choices=list_microphones(),
                    scale=5,
                )
                switch_refresh_btn = gr.Button("🔄", scale=0, min_width=50)

    job_out = gr.Markdown()

    with gr.Column(visible=False) as result_col:
        result_title = gr.Markdown()
        result_audio = gr.Audio(label="🎧 Escuchar la reunión", type="filepath", interactive=False, visible=False)
        transcript_out = gr.Textbox(label="Transcripción", lines=18, max_lines=30, buttons=["copy"])
        # "hidden" keeps it in the page so the copy button's JS can read its value;
        # visible=False removes it from the DOM in Gradio 6.
        prompt_hidden = gr.Textbox(visible="hidden")
        with gr.Row():
            copy_prompt_btn = gr.Button("🤖 Copiar con instrucción de resumen", variant="primary")
            download_btn = gr.DownloadButton("⬇️ Descargar .md")
            open_btn = gr.Button("📂 Abrir carpeta")

    timer = gr.Timer(1.0)

    mode_in.change(on_mode_change, inputs=[mode_in], outputs=[tips_out, mic_in, test_out])
    refresh_btn.click(on_refresh_mics, inputs=[mode_in, mic_in], outputs=[mic_in])
    test_btn.click(on_test, inputs=[mode_in, mic_in], outputs=[test_out])
    start_btn.click(
        on_start,
        inputs=[mode_in, title_in, mic_in, language_in],
        outputs=[
            setup_col, rec_col, rec_html, pause_btn, result_col, job_out, watch_state,
            mute_btn, muted_state, switch_mic_in,
        ],
    )
    pause_btn.click(on_pause, outputs=[pause_btn, rec_html])
    mute_btn.click(on_mute, outputs=[mute_btn, muted_state, rec_html])
    switch_mic_in.input(on_switch_mic, inputs=[switch_mic_in], outputs=[rec_html, switch_mic_in])
    switch_refresh_btn.click(on_refresh_switch, outputs=[switch_mic_in])
    stop_btn.click(on_stop, outputs=[setup_col, rec_col, job_out, watch_state, title_in])
    timer.tick(
        on_tick,
        inputs=[watch_state, muted_state],
        outputs=[
            rec_html, job_out, result_col, result_title, transcript_out, prompt_hidden,
            download_btn, result_audio, watch_state, mute_btn, muted_state,
        ],
        show_progress="hidden",
    )
    copy_prompt_btn.click(notify_copied, inputs=[prompt_hidden], js=COPY_JS)
    open_btn.click(on_open_folder, inputs=[watch_state])
    demo.load(
        on_load,
        outputs=[
            setup_col, rec_col, rec_html, pause_btn, job_out, watch_state, mic_in,
            mute_btn, muted_state, switch_mic_in,
        ],
    )
