"""Transcriptor Local: graba y transcribe reuniones en tu PC, y convierte archivos a texto."""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import shutil
import sys
import threading
import warnings
import webbrowser
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Meeting data must stay on this machine: no usage statistics to Gradio's servers.
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

# Self-contained install: the voice model and ffmpeg live inside the project folder
# (see instalar.bat), not in the user's cache or the system PATH. Set before any
# import, because huggingface_hub reads HF_HOME when it is first imported.
APP_DIR = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(APP_DIR / "models"))
os.environ["PATH"] = str(APP_DIR / "tools" / "ffmpeg" / "bin") + os.pathsep + os.environ.get("PATH", "")

import gradio as gr  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning, module="pydub")

from meetings import store  # noqa: E402
from meetings.recorder import recorder  # noqa: E402
from meetings.transcriber import transcription_queue  # noqa: E402
from settings import MEETINGS_DIR, load_config  # noqa: E402
from ui import converter_tab, history_tab, meeting_tab, settings_tab  # noqa: E402

load_dotenv(Path(__file__).with_name(".env"))

CSS = """
.mk-rec { border: 1px solid var(--border-color-primary); border-radius: 12px; padding: 16px 18px;
          background: var(--background-fill-secondary); }
.mk-head { display: flex; align-items: center; gap: 14px; font-weight: 700; font-size: 1.05rem;
           color: var(--body-text-color); margin-bottom: 12px; }
.mk-time { font-variant-numeric: tabular-nums; font-size: 1.6rem; }
.mk-size { margin-left: auto; font-weight: 400; color: var(--body-text-color-subdued); }
.mk-dot { width: 12px; height: 12px; border-radius: 50%; background: #ef4444; display: inline-block;
          animation: mk-blink 1.2s infinite; }
@keyframes mk-blink { 50% { opacity: .25; } }
.mk-meter { display: grid; grid-template-columns: minmax(120px, 38%) 1fr; align-items: center; gap: 10px;
            margin: 6px 0; }
.mk-label { font-size: .9rem; color: var(--body-text-color); overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap; }
.mk-bar { height: 12px; border-radius: 6px; background: var(--border-color-primary); overflow: hidden; }
.mk-bar > div { height: 100%; background: #22c55e; transition: width .25s linear; }
.mk-bar-alert > div { background: #f59e0b; }
.mk-bar-muted { opacity: .45; }
.mk-alert { margin-top: 10px; padding: 8px 12px; border-radius: 8px; background: rgba(245, 158, 11, .15);
            color: var(--body-text-color); font-weight: 600; }
.mk-warn { margin-top: 6px; font-size: .9rem; color: var(--body-text-color); }
.mk-note { margin-top: 12px; font-size: .85rem; color: var(--body-text-color-subdued); }
@media (max-width: 640px) { .mk-meter { grid-template-columns: 1fr; gap: 4px; } }
"""


def build_ui() -> gr.Blocks:
    # Every audio played or file uploaded is copied into Gradio's cache in %TEMP%;
    # without this it grows forever (a 2 h meeting is ~80 MB per listen).
    with gr.Blocks(title="Transcriptor Local", delete_cache=(3600, 86400)) as demo:
        gr.Markdown("# 🎙️ Transcriptor Local")
        with gr.Tabs():
            with gr.Tab("🎙️ Grabar reunión"):
                meeting_tab.build(demo)
            with gr.Tab("📚 Historial") as history:
                history_tab.build(history)
            with gr.Tab("📄 Convertir archivos"):
                converter_tab.build()
            with gr.Tab("⚙️ Configuración") as settings:
                settings_tab.build(settings)
    return demo


APP_PORT = 7860
APP_URL = f"http://127.0.0.1:{APP_PORT}"
_instance_mutex = None


def _already_running() -> bool:
    """A second copy would run its own recorder and transcription queue over the
    same files (and mark the first one's live recording as interrupted)."""
    global _instance_mutex
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\TranscriptorLocal")
    ERROR_ALREADY_EXISTS = 183
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


log = logging.getLogger("mk.app")
_console_handler = None


def _setup_logging() -> None:
    """One rotating log per user (max ~4 MB): the first thing to ask for when
    a teammate reports a problem."""
    logs_dir = MEETINGS_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs_dir / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger("mk")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    threading.excepthook = lambda args: log.error(
        "Error no controlado en el hilo %s", args.thread.name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _stop_recording_on_exit() -> None:
    if recorder.is_recording:
        log.info("La app se está cerrando: se detiene la grabación en curso")
        recorder.stop()


def _stop_recording_when_console_closes() -> None:
    """Closing the black window kills the process in ~5 s without running atexit.
    Stopping here finalizes the recording as a normal (not interrupted) meeting."""
    if sys.platform != "win32":
        return
    global _console_handler
    CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN = 2, 5, 6

    def handler(event: int) -> bool:
        if event in (CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN):
            _stop_recording_on_exit()
        return False  # let the default handler continue (terminate / Ctrl+C)

    _console_handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)


def startup() -> None:
    _setup_logging()
    log.info("Iniciando Transcriptor Local (datos en %s)", MEETINGS_DIR)
    shutil.rmtree(converter_tab.OUTPUT_ROOT, ignore_errors=True)
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    for meeting in store.recover_unfinished():
        log.info("Reunión pendiente%s: %s", " (interrumpida)" if meeting.interrupted else "", meeting.id)
        transcription_queue.enqueue(meeting.id)
    store.apply_retention(load_config().retention_days)
    transcription_queue.start()
    atexit.register(_stop_recording_on_exit)
    _stop_recording_when_console_closes()


if __name__ == "__main__":
    if _already_running():
        print(f"Transcriptor Local ya está abierto. Abriendo {APP_URL} en el navegador…")
        webbrowser.open(APP_URL)
        sys.exit(0)
    startup()
    try:
        build_ui().launch(
            inbrowser=True,
            server_name="127.0.0.1",
            server_port=APP_PORT,
            theme=gr.themes.Soft(),
            css=CSS,
            allowed_paths=[str(converter_tab.OUTPUT_ROOT), str(MEETINGS_DIR)],
        )
    except OSError:
        log.exception("No se pudo abrir el puerto %d", APP_PORT)
        print(
            f"\nNo se pudo iniciar: otro programa está usando el puerto {APP_PORT}.\n"
            "Cierra ese programa (u otra app hecha con Gradio) y vuelve a abrir Transcriptor Local."
        )
        sys.exit(1)
