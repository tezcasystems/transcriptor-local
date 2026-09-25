"""Background queue that turns recorded meetings into transcripts.

Jobs never run while a recording is in progress (keeps the call smooth), and
their state lives in meeting.json, so they resume after the app restarts.
"""

from __future__ import annotations

import difflib
import logging
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from diagnostics import find_ffmpeg
from meetings import store
from meetings.recorder import recorder
from settings import load_config
from transcription.engine import Segment, engine
from transcription.text import paragraphs, timestamp

log = logging.getLogger("mk.transcriber")

SPEAKERS = {store.TRACK_MIC: "Tú", store.TRACK_PC: "Participantes"}
ECHO_OVERLAP_SECONDS = 1.5
ECHO_MIN_MATCH = 0.6
ECHO_MIN_WORDS = 4
TURN_MAX_SECONDS = 90.0
TURN_MAX_GAP = 8.0
MIN_SEGMENT_BYTES = 200
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class JobProgress:
    meeting_id: str
    title: str
    stage: str
    fraction: float
    started: float


# ---- audio preparation ----------------------------------------------------------


def _join_segments(meeting: store.Meeting, track: str) -> None:
    """Concatenates the 60 s recording segments into one file per track.
    Safe to re-run: segments are deleted only after a successful join."""
    segments = [p for p in meeting.segment_paths(track) if p.stat().st_size >= MIN_SEGMENT_BYTES]
    if not segments:
        return
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError("Falta ffmpeg para unir la grabación.")
    list_file = meeting.audio_dir / f"{track}_lista.txt"
    list_file.write_text("".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")
    output = meeting.track_path(track)
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    for codec in (["-c", "copy"], ["-c:a", "libopus", "-b:a", "48k"]):
        proc = subprocess.run([*base, *codec, str(output)], capture_output=True, creationflags=_NO_WINDOW)
        if proc.returncode == 0:
            break
    else:
        raise RuntimeError("No se pudo unir la grabación: " + proc.stderr.decode(errors="replace")[-300:])
    # Best effort: OneDrive or an antivirus may briefly lock a file; the joined
    # track is already safe, and leftovers are simply re-joined on a retry.
    for leftover in [list_file, *meeting.segment_paths(track)]:
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            log.warning("No se pudo borrar %s (bloqueado); se reintentará después", leftover.name)


def ensure_listen_file(meeting: store.Meeting) -> None:
    """Web meetings are recorded as two tracks (needed to tell 'Tú' from
    'Participantes'); this writes one file with both, which is what people
    expect to hear when they play the meeting back."""
    mic, pc, output = meeting.track_path(store.TRACK_MIC), meeting.track_path(store.TRACK_PC), meeting.listen_path
    ffmpeg = find_ffmpeg()
    if ffmpeg is None or meeting.mode != store.MODE_WEB or output.exists() or not (mic.exists() and pc.exists()):
        return
    partial = output.with_name(output.stem + ".tmp.ogg")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(mic), "-i", str(pc),
        "-filter_complex", "amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95:level=disabled",
        "-c:a", "libopus", "-b:a", "48k", str(partial),
    ]
    # Listening copy only: a failure here must not block the transcript. Written
    # under a temp name so an interrupted mix is never mistaken for a finished one.
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode == 0:
        partial.replace(output)
    else:
        partial.unlink(missing_ok=True)
        log.warning(
            "No se pudo crear el audio para escuchar de %s: %s",
            meeting.id, proc.stderr.decode(errors="replace")[-300:],
        )


# ---- transcript building --------------------------------------------------------


def _words(text: str) -> list[str]:
    # Accent-insensitive: the two tracks may spell the same word "video" / "vídeo".
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return re.findall(r"\w+", plain)


def _is_echo(seg: Segment, pc: list[Segment]) -> bool:
    words = _words(seg.text)
    if not words:
        return False
    pc_words = [
        w
        for p in pc
        if p.start < seg.end + ECHO_OVERLAP_SECONDS and p.end > seg.start - ECHO_OVERLAP_SECONDS
        for w in _words(p.text)
    ]
    if not pc_words:
        return False
    matcher = difflib.SequenceMatcher(None, words, pc_words, autojunk=False)
    if len(words) < ECHO_MIN_WORDS:
        # A short reply ("sí", "ok") is only echo if the PC said exactly that at that moment.
        return matcher.find_longest_match(0, len(words), 0, len(pc_words)).size == len(words)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(words) >= ECHO_MIN_MATCH


def _remove_echo(mic: list[Segment], pc: list[Segment]) -> list[Segment]:
    """Drops mic segments that just repeat what the PC played (speakers instead of headphones)."""
    return [seg for seg in mic if not _is_echo(seg, pc)]


def _turns(labeled: list[tuple[str, Segment]]) -> list[tuple[str, float, str]]:
    """Merges consecutive segments from the same speaker into (speaker, start, text) turns."""
    turns: list[tuple[str, float, float, list[str]]] = []
    for speaker, seg in sorted(labeled, key=lambda item: item[1].start):
        if turns:
            last_speaker, start, end, texts = turns[-1]
            if (
                last_speaker == speaker
                and seg.start - end <= TURN_MAX_GAP
                and seg.end - start <= TURN_MAX_SECONDS
            ):
                turns[-1] = (speaker, start, max(end, seg.end), [*texts, seg.text])
                continue
        turns.append((speaker, seg.start, seg.end, [seg.text]))
    return [(speaker, start, " ".join(texts)) for speaker, start, _, texts in turns]


def build_transcript(meeting: store.Meeting, results: dict[str, list[Segment]]) -> str:
    hours = meeting.duration >= 3600
    mode_label = "Reunión web" if meeting.mode == store.MODE_WEB else "Reunión presencial"
    lines = [
        f"# {meeting.title}",
        "",
        f"**Fecha:** {meeting.started:%d/%m/%Y %H:%M} · **Duración:** {timestamp(meeting.duration, hours)} · "
        f"**Tipo:** {mode_label} · **Idioma:** {meeting.language} · **Modelo:** {meeting.model_size}",
    ]
    if meeting.interrupted:
        lines += ["", "> ⚠️ La grabación se interrumpió (la app se cerró antes de detenerla). Se transcribió lo que quedó guardado."]
    lines += ["", "---", ""]

    if meeting.mode == store.MODE_WEB:
        mic = _remove_echo(results.get(store.TRACK_MIC, []), results.get(store.TRACK_PC, []))
        labeled = [(SPEAKERS[store.TRACK_MIC], s) for s in mic]
        labeled += [(SPEAKERS[store.TRACK_PC], s) for s in results.get(store.TRACK_PC, [])]
        body = [f"**[{timestamp(start, hours)}] {speaker}:** {text}" for speaker, start, text in _turns(labeled)]
    else:
        body = [f"**[{timestamp(start, hours)}]** {text}" for start, text in paragraphs(results.get(store.TRACK_MIC, []))]

    lines += ["\n\n".join(body) if body else "*No se detectó voz en la grabación.*", ""]
    return "\n".join(lines)


# ---- queue ----------------------------------------------------------------------


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    network_hints = (
        "huggingface", "connection", "resolve", "timed out", "offline",
        "ssl", "certificate", "proxy", "http error", "403", "407",
    )
    if any(k in lowered for k in network_hints):
        return (
            "No se pudo descargar el modelo de voz. Se necesita internet solo la primera vez; "
            "conéctate y usa «Re-transcribir» en el Historial."
        )
    if isinstance(exc, MemoryError):
        return "No hubo memoria suficiente. Cierra otras apps o usa el modelo 'Rápido' y re-transcribe."
    return text


class TranscriptionQueue:
    def __init__(self) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._queued: list[str] = []
        self._progress: Optional[JobProgress] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="transcriber")
            self._thread.start()

    def enqueue(self, meeting_id: str) -> None:
        if meeting_id not in self._queued:
            self._queued.append(meeting_id)
            self._queue.put(meeting_id)

    def retranscribe(self, meeting_id: str, model_size: str) -> None:
        meeting = store.get(meeting_id)
        if meeting is None or meeting.audio_deleted:
            raise ValueError("El audio de esta reunión ya no existe.")
        meeting.status = store.PENDING
        meeting.model_size = model_size
        store.save(meeting)
        self.enqueue(meeting_id)

    def progress(self) -> Optional[JobProgress]:
        return self._progress

    def queued_ids(self) -> list[str]:
        return list(self._queued)

    def _set(self, meeting: store.Meeting, stage: str, fraction: float, started: float) -> None:
        self._progress = JobProgress(meeting.id, meeting.title, stage, fraction, started)

    def _run(self) -> None:
        # Nothing here may raise out of the loop: a dead thread would silently stop all transcriptions.
        while True:
            meeting_id = self._queue.get()
            try:
                self._process(meeting_id)
            except Exception as exc:  # noqa: BLE001 - recorded on the meeting, queue keeps going
                log.exception("Falló la transcripción de %s", meeting_id)
                try:
                    meeting = store.get(meeting_id)
                    if meeting:
                        meeting.status = store.ERROR
                        meeting.error = _friendly_error(exc)
                        store.save(meeting)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self._progress = None
                if meeting_id in self._queued:
                    self._queued.remove(meeting_id)
                try:
                    freed = store.apply_retention(load_config().retention_days)
                    if freed:
                        log.info("Retención: se liberaron %.1f MB de audio antiguo", freed / 1e6)
                except Exception:  # noqa: BLE001 - cleanup retries after the next job
                    log.exception("Falló la limpieza de audio antiguo")

    def _wait_for_recording(self, meeting: store.Meeting, started: float, fraction: float) -> None:
        while recorder.is_recording:
            self._set(meeting, "En espera: hay una grabación en curso", fraction, started)
            time.sleep(2)

    def _process(self, meeting_id: str) -> None:
        meeting = store.get(meeting_id)
        if meeting is None or meeting.status != store.PENDING:
            return
        started = time.time()
        self._wait_for_recording(meeting, started, 0.0)
        meeting.status = store.TRANSCRIBING
        meeting.error = ""
        store.save(meeting)

        tracks = [store.TRACK_MIC] + ([store.TRACK_PC] if meeting.mode == store.MODE_WEB else [])
        self._set(meeting, "Preparando audio", 0.0, started)
        for track in tracks:
            _join_segments(meeting, track)
        ensure_listen_file(meeting)

        results: dict[str, list[Segment]] = {}
        for i, track in enumerate(tracks):
            path = meeting.track_path(track)
            if not path.exists():
                results[track] = []
                continue
            self._wait_for_recording(meeting, started, i / len(tracks))
            label = SPEAKERS[track] if meeting.mode == store.MODE_WEB else "audio"
            stage = f"Transcribiendo {label}"
            if not engine.is_loaded(meeting.model_size):
                stage = "Cargando el modelo de voz"
            self._set(meeting, stage, i / len(tracks), started)
            segments, duration = engine.transcribe(
                str(path),
                language=meeting.language,
                model_size=meeting.model_size,
                on_progress=lambda f, i=i, label=label: self._set(
                    meeting, f"Transcribiendo {label}", (i + f) / len(tracks), started
                ),
            )
            results[track] = segments
            meeting.duration = max(meeting.duration, duration)

        meeting.transcript_path.write_text(build_transcript(meeting, results), encoding="utf-8")
        meeting.status = store.DONE
        meeting.transcribed_at = datetime.now().replace(microsecond=0).isoformat()
        store.save(meeting)
        log.info(
            "Transcripción lista %s | audio=%.0fs | tardó=%.0fs | modelo=%s",
            meeting.id, meeting.duration, time.time() - started, meeting.model_size,
        )


transcription_queue = TranscriptionQueue()
