"""Tab: convert files and URLs to Markdown."""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path

import gradio as gr

from converter_service import ConversionOutcome, Settings, build_markitdown, convert_one
from settings import load_config
from transcription.engine import engine
from ui.common import LANGUAGE_CHOICES

OUTPUT_ROOT = Path(tempfile.gettempdir()) / "markitdown_gui"
PREVIEW_LIMIT = 150_000
VISION_MODEL_CHOICES = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"]

INTRO = """
Convierte documentos, hojas de cálculo, presentaciones, imágenes, audio, video y páginas web a texto/Markdown.
**PDF · Word · PowerPoint · Excel · CSV · JSON · XML · HTML · EPUB · ZIP · Outlook (.msg) · Jupyter · Imágenes (jpg/png) · YouTube**
**Audio:** mp3 · wav · m4a · aac · ogg · opus · flac · wma · aiff — **Video:** mp4 · mov · mkv · avi · webm · wmv · flv · 3gp · mpeg
"""


def _safe_filename(name: str) -> str:
    name = re.sub(r"^https?://", "", name)
    stem = Path(name).stem if "." in Path(name).name else name
    stem = re.sub(r"[^\w\-. ]+", "_", stem).strip("._ ")
    return (stem or "resultado")[:80]


def _write_outputs(outcomes: list[ConversionOutcome]) -> tuple[list[str | None], str | None]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(dir=OUTPUT_ROOT))
    used: set[str] = set()
    paths: list[str | None] = []
    for outcome in outcomes:
        if not outcome.ok:
            paths.append(None)
            continue
        base = _safe_filename(outcome.source_name)
        candidate, n = base, 2
        while candidate.lower() in used:
            candidate, n = f"{base}_{n}", n + 1
        used.add(candidate.lower())
        path = run_dir / f"{candidate}.md"
        path.write_text(outcome.markdown, encoding="utf-8")
        paths.append(str(path))

    written = [p for p in paths if p]
    zip_path = None
    if len(outcomes) > 1 and written:
        zip_path = str(run_dir / "resultados_markitdown.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in written:
                zf.write(p, arcname=Path(p).name)
    return paths, zip_path


def _truncate(text: str) -> str:
    if len(text) <= PREVIEW_LIMIT:
        return text
    return (
        text[:PREVIEW_LIMIT]
        + f"\n\n---\n*Vista previa recortada ({len(text):,} caracteres en total). "
        "Descarga el archivo .md para verlo completo.*"
    )


def _show_record(record: dict):
    name = record["source_name"]
    if record["ok"]:
        text = record["markdown"]
        notice = f"⚠️ {record['warning']}" if record["warning"] else ""
        return (
            f"### ✅ {name}",
            _truncate(text) or "*(sin contenido)*",
            _truncate(text),
            gr.DownloadButton(value=record["md_path"], visible=True),
            gr.Markdown(value=notice, visible=bool(notice)),
            gr.Accordion(visible=False),
            "",
        )
    return (
        f"### ❌ {name}",
        "",
        "",
        gr.DownloadButton(value=None, visible=False),
        gr.Markdown(value=f"❌ **{record['error_message']}**", visible=True),
        gr.Accordion(visible=True),
        record["error_detail"],
    )


def on_convert(files, urls_text, language, enable_plugins, api_key, model, progress=gr.Progress()):
    sources = [(f, False) for f in (files or [])]
    sources += [(u.strip(), True) for u in (urls_text or "").splitlines() if u.strip()]
    if not sources:
        raise gr.Error("Agrega al menos un archivo o una URL.")

    settings = Settings(
        enable_plugins=enable_plugins,
        api_key=(api_key or "").strip() or os.environ.get("OPENAI_API_KEY"),
        model=(model or VISION_MODEL_CHOICES[0]).strip(),
        language=(language or LANGUAGE_CHOICES[0][1]).strip(),
        model_size=load_config().model_size,
    )
    try:
        md = build_markitdown(settings)
    except Exception as exc:  # noqa: BLE001
        raise gr.Error(f"No se pudo inicializar MarkItDown: {exc}")

    outcomes: list[ConversionOutcome] = []
    for i, (source, is_url) in enumerate(sources):
        label = source if is_url else Path(source).name
        progress(i / len(sources), desc=f"Convirtiendo {label} ({i + 1}/{len(sources)})")
        if is_url and not re.match(r"^https?://", source, re.IGNORECASE):
            outcomes.append(
                ConversionOutcome(
                    source_name=source,
                    ok=False,
                    error_message="La URL debe empezar con http:// o https://",
                )
            )
            continue
        if engine.busy:
            progress(i / len(sources), desc="Esperando a que termine la transcripción de una reunión…")

        def report(fraction, i=i, label=label):
            progress((i + fraction) / len(sources), desc=f"Transcribiendo {label}: {fraction:.0%}")

        outcomes.append(convert_one(source, md, is_url=is_url, progress_callback=report))
    progress(1.0, desc="Listo")

    md_paths, zip_path = _write_outputs(outcomes)
    records = [{**asdict(o), "md_path": p} for o, p in zip(outcomes, md_paths)]

    ok_count = sum(o.ok for o in outcomes)
    multi = len(outcomes) > 1
    summary = f"**{ok_count} de {len(outcomes)}** convertidos correctamente."
    if multi:
        summary += " Haz clic en una fila de la tabla para ver su resultado."

    rows = [
        [
            r["source_name"],
            "✅ OK" if r["ok"] else "❌ Error",
            len(r["markdown"]) if r["ok"] else 0,
            r["warning"] if r["ok"] else r["error_message"],
        ]
        for r in records
    ]
    first = next((r for r in records if r["ok"]), records[0])

    return (
        gr.Column(visible=True),
        summary,
        gr.Dataframe(value=rows, visible=multi),
        gr.DownloadButton(value=zip_path, visible=bool(multi and zip_path)),
        records,
        *_show_record(first),
    )


def on_select_row(evt: gr.SelectData, records):
    if not records:
        raise gr.Error("No hay resultados.")
    return _show_record(records[evt.index[0]])


def on_clear():
    return None, "", gr.Column(visible=False), []


def build() -> None:
    records_state = gr.State([])
    gr.Markdown(INTRO)

    with gr.Row(equal_height=False):
        with gr.Column(scale=3):
            with gr.Tabs():
                with gr.Tab("📂 Archivo(s)"):
                    files_in = gr.File(
                        label="Arrastra aquí uno o varios archivos, o haz clic para elegirlos",
                        file_count="multiple",
                        type="filepath",
                        height=220,
                    )
                with gr.Tab("🌐 URL / YouTube"):
                    urls_in = gr.Textbox(
                        label="Una URL por línea",
                        placeholder="https://www.youtube.com/watch?v=...\nhttps://es.wikipedia.org/wiki/...",
                        lines=6,
                    )
            gr.Markdown(
                "<small>Se convierten todos los archivos **y** URLs que agregues. "
                "Con uno verás el resultado directo; con varios, una tabla.</small>"
            )
            with gr.Row():
                convert_btn = gr.Button("🔄 Convertir", variant="primary", size="lg", scale=3)
                clear_btn = gr.Button("🧹 Limpiar", size="lg", scale=1)

        with gr.Column(scale=2):
            language_in = gr.Dropdown(
                label="🎙️ Idioma del audio / video",
                choices=LANGUAGE_CHOICES,
                value=load_config().language,
                allow_custom_value=True,
                info="Se usa solo para transcribir audio y video (Whisper local, sin internet).",
            )
            with gr.Accordion("⚙️ Opciones", open=False):
                plugins_in = gr.Checkbox(label="Habilitar plugins de terceros instalados", value=False)
                api_key_in = gr.Textbox(
                    label="🔑 API key de OpenAI (opcional)",
                    info="Activa OCR/descripción de imágenes y diapositivas. "
                    "Si la dejas vacía se usa OPENAI_API_KEY del archivo gui/.env, si existe. "
                    "Usarla envía las imágenes a OpenAI.",
                    type="password",
                )
                model_in = gr.Dropdown(
                    label="Modelo con visión",
                    choices=VISION_MODEL_CHOICES,
                    value=VISION_MODEL_CHOICES[0],
                    allow_custom_value=True,
                )

    with gr.Column(visible=False) as results_col:
        gr.Markdown("## Resultados")
        summary_out = gr.Markdown()
        table_out = gr.Dataframe(
            headers=["Archivo", "Estado", "Caracteres", "Nota"],
            datatype=["str", "str", "number", "str"],
            interactive=False,
            wrap=True,
            visible=False,
        )
        zip_btn = gr.DownloadButton("⬇️ Descargar todo (.zip)", visible=False)

        selected_title = gr.Markdown()
        notice_out = gr.Markdown(visible=False)
        with gr.Accordion("Detalle técnico del error", open=False, visible=False) as error_acc:
            error_detail_out = gr.Textbox(show_label=False, lines=10, buttons=["copy"])

        with gr.Tabs():
            with gr.Tab("👁️ Vista previa"):
                preview_out = gr.Markdown(max_height=600)
            with gr.Tab("📝 Texto / Markdown"):
                raw_out = gr.Textbox(show_label=False, lines=20, max_lines=30, buttons=["copy"])
        md_btn = gr.DownloadButton("⬇️ Descargar .md", visible=False)

    detail_outputs = [selected_title, preview_out, raw_out, md_btn, notice_out, error_acc, error_detail_out]

    convert_btn.click(
        on_convert,
        inputs=[files_in, urls_in, language_in, plugins_in, api_key_in, model_in],
        outputs=[results_col, summary_out, table_out, zip_btn, records_state, *detail_outputs],
    )
    table_out.select(on_select_row, inputs=[records_state], outputs=detail_outputs)
    clear_btn.click(on_clear, outputs=[files_in, urls_in, results_col, records_state])
