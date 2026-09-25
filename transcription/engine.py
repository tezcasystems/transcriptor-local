"""Shared local speech-to-text engine (faster-whisper) for files and meetings."""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Union

log = logging.getLogger("mk.engine")

MODEL_CHOICES = [
    ("Rápido (base) · ~6 min por hora de audio", "base"),
    ("Equilibrado (small) · ~18 min por hora", "small"),
    ("Máxima precisión (medium) · ~1 h por hora · la 1ª vez descarga ~1.5 GB", "medium"),
]
MODEL_ORDER = ["base", "small", "medium"]
DEFAULT_MODEL = "small"
# Measured on a 14-thread laptop: 4 threads are as fast as 14. Never take more
# than half the machine, so a 4-core teammate can keep working during a job.
CPU_THREADS = max(1, min(4, (os.cpu_count() or 2) // 2))
# The small model holds ~365 MB; free it when the app sits idle all day.
IDLE_UNLOAD_SECONDS = 600
# With VAD, Whisper can merge utterances minutes apart into one segment (a mic
# where the user only says a word now and then). Word timestamps (+8% time)
# let us split them back at real pauses so each keeps its true time.
SPLIT_PAUSE_SECONDS = 1.5


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


def whisper_language(code: Optional[str]) -> Optional[str]:
    """'es-MX' -> 'es'; empty or 'auto' -> None (auto-detect)."""
    if not code or code.lower() == "auto":
        return None
    return code.split("-")[0].lower()


# Phrases Whisper invents on non-speech audio (learned from subtitled videos).
_HALLUCINATIONS = re.compile(r"amara\.org|subt[ií]tulos (realizados )?por la comunidad", re.IGNORECASE)


def _split_at_pauses(seg) -> list[Segment]:
    if _HALLUCINATIONS.search(seg.text):
        return []
    words = [w for w in (seg.words or []) if w.word.strip()]
    if not words:
        text = seg.text.strip()
        return [Segment(seg.start, seg.end, text)] if text else []
    groups = [[words[0]]]
    for prev, word in zip(words, words[1:]):
        if word.start - prev.end > SPLIT_PAUSE_SECONDS:
            groups.append([])
        groups[-1].append(word)
    return [
        Segment(group[0].start, group[-1].end, "".join(w.word for w in group).strip())
        for group in groups
    ]


def model_cached(size: str) -> bool:
    from huggingface_hub.constants import HF_HUB_CACHE

    snapshots = Path(HF_HUB_CACHE) / f"models--Systran--faster-whisper-{size}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def better_model(size: str) -> str:
    """The next more accurate model (for re-transcribing), or the same if it's the top one."""
    i = MODEL_ORDER.index(size) if size in MODEL_ORDER else 0
    return MODEL_ORDER[min(i + 1, len(MODEL_ORDER) - 1)]


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    _kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]


@contextmanager
def _background_priority():
    """Transcription is heavy but never urgent: let Teams and the user win the CPU."""
    if sys.platform != "win32":
        yield
        return
    BELOW_NORMAL, NORMAL = 0x4000, 0x0020
    process = _kernel32.GetCurrentProcess()
    previous = _kernel32.GetPriorityClass(process) or NORMAL
    _kernel32.SetPriorityClass(process, BELOW_NORMAL)
    try:
        yield
    finally:
        _kernel32.SetPriorityClass(process, previous)


class WhisperEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._model_size: Optional[str] = None
        self._unload_timer: Optional[threading.Timer] = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def is_loaded(self, size: str) -> bool:
        return self._model is not None and self._model_size == size

    def _get_model(self, size: str):
        if self._model is None or self._model_size != size:
            from faster_whisper import WhisperModel

            self._model = None
            gc.collect()
            started = time.perf_counter()
            # Once downloaded, never contact Hugging Face again: works offline and
            # nothing leaves the PC (otherwise every load checks for updates online).
            self._model = WhisperModel(
                size, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS,
                local_files_only=model_cached(size),
            )
            self._model_size = size
            log.info("Modelo '%s' cargado en %.1fs (%d hilos)", size, time.perf_counter() - started, CPU_THREADS)
        return self._model

    def _schedule_unload(self) -> None:
        if self._unload_timer:
            self._unload_timer.cancel()
        self._unload_timer = threading.Timer(IDLE_UNLOAD_SECONDS, self._unload_if_idle)
        self._unload_timer.daemon = True
        self._unload_timer.start()

    def _unload_if_idle(self) -> None:
        if not self._lock.acquire(blocking=False):
            return  # a new job started; it reschedules the unload when it ends
        try:
            if self._model is not None:
                self._model = None
                self._model_size = None
                gc.collect()
                log.info("Modelo liberado de la memoria tras %d min sin uso", IDLE_UNLOAD_SECONDS // 60)
        finally:
            self._lock.release()

    def transcribe(
        self,
        audio: Union[str, BinaryIO],
        language: Optional[str] = None,
        model_size: str = DEFAULT_MODEL,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> tuple[list[Segment], float]:
        """Returns (segments, audio duration in seconds). One transcription at a time."""
        with self._lock, _background_priority():
            try:
                model = self._get_model(model_size)
                segments, info = model.transcribe(
                    audio,
                    language=whisper_language(language),
                    beam_size=5,
                    vad_filter=True,
                    condition_on_previous_text=False,
                    word_timestamps=True,
                )
                duration = info.duration or 0.0
                result = []
                for seg in segments:
                    result.extend(_split_at_pauses(seg))
                    if on_progress and duration:
                        on_progress(min(seg.end / duration, 1.0))
                if on_progress:
                    on_progress(1.0)
                return result, duration
            finally:
                self._schedule_unload()


engine = WhisperEngine()
