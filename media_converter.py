"""Audio/video transcription converter backed by local Whisper.

Replaces MarkItDown's built-in AudioConverter, which only transcribes in
English through Google's online service and accepts few formats.
"""

from __future__ import annotations

from typing import Any, BinaryIO

from markitdown import DocumentConverter, DocumentConverterResult, StreamInfo

from transcription.engine import DEFAULT_MODEL, engine
from transcription.text import paragraphs, timestamp

AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wma", ".aiff", ".aif", ".amr",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm",
    ".wmv", ".flv", ".3gp", ".mpeg", ".mpg", ".ts",
}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def _check_audio_stream(file_stream: BinaryIO) -> None:
    import av

    try:
        with av.open(file_stream) as container:
            has_audio = bool(container.streams.audio)
    except av.error.FFmpegError as exc:
        raise ValueError("No se pudo leer el archivo (¿está dañado o incompleto?).") from exc
    finally:
        file_stream.seek(0)
    if not has_audio:
        raise ValueError("El archivo no tiene pista de audio.")


class MediaTranscriptionConverter(DocumentConverter):
    def __init__(self, language: str = "es-MX", model_size: str = DEFAULT_MODEL):
        super().__init__()
        self.language = language
        self.model_size = model_size

    def accepts(self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any) -> bool:
        extension = (stream_info.extension or "").lower()
        mimetype = (stream_info.mimetype or "").lower()
        return extension in MEDIA_EXTENSIONS or mimetype.startswith(("audio/", "video/"))

    def convert(
        self, file_stream: BinaryIO, stream_info: StreamInfo, **kwargs: Any
    ) -> DocumentConverterResult:
        _check_audio_stream(file_stream)
        segments, duration = engine.transcribe(
            file_stream,
            language=self.language,
            model_size=self.model_size,
            on_progress=kwargs.get("progress_callback"),
        )
        long_audio = duration >= 3600
        body = "\n\n".join(
            f"**[{timestamp(start, long_audio)}]** {text}" for start, text in paragraphs(segments)
        ) or "*No se detectó voz en el audio.*"
        header = (
            f"## Transcripción\n\n*Idioma: {self.language} · Duración: "
            f"{timestamp(duration, long_audio)} · Modelo: {self.model_size}*"
        )
        return DocumentConverterResult(markdown=f"{header}\n\n{body}")
