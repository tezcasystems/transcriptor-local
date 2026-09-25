"""Meeting recorder: microphone + PC audio (loopback) -> Opus segments on disk.

Each track is clocked by wall time: capture threads push audio into buffers and
a mixer thread pulls exactly the samples due every 100 ms (padding silence).
That keeps the mic and PC tracks aligned even if a device disconnects or a
Bluetooth headset switches profile mid-meeting. Audio goes straight to ffmpeg,
which writes 60 s Opus segments, so memory stays flat and a crash loses at most
the segment in progress.
"""

from __future__ import annotations

import ctypes
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import soundcard as sc

from diagnostics import find_ffmpeg
from meetings import store
from meetings.devices import open_microphone
from meetings.hotkey import GlobalHotkey
from settings import MEETINGS_DIR

SAMPLE_RATE = 48000
BLOCK = SAMPLE_RATE // 10  # 100 ms
SEGMENT_SECONDS = 60
MAX_BUFFER_SAMPLES = SAMPLE_RATE  # drop backlog beyond 1 s (device clock drift)
RESCAN_SECONDS = 3.0
MIC_SILENT_ALERT_SAMPLES = 15 * SAMPLE_RATE
OPUS_BITRATE = 48_000  # natural-sounding speech; ~22 MB per hour per track
OPUS_BYTES_PER_SECOND = OPUS_BITRATE // 8
MIN_FREE_BYTES = 2 * 1024**3
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Soft limiter instead of hard clipping: loopback audio can exceed full scale.
_LIMITER = "alimiter=limit=0.95:level=disabled"
# Levels tuned on a real recording: the laptop mic averaged -35 dB vs -23 dB for
# the PC audio; dynaudnorm m=5 brings it to about -25 dB without pumping noise.
_FILTERS = {
    (store.MODE_WEB, store.TRACK_MIC): f"highpass=f=80,dynaudnorm=m=5,{_LIMITER}",
    (store.MODE_WEB, store.TRACK_PC): f"highpass=f=60,{_LIMITER}",
    # Stronger leveling so far voices around a table are as loud as near ones.
    (store.MODE_IN_PERSON, store.TRACK_MIC): f"highpass=f=80,dynaudnorm=m=10,{_LIMITER}",
}


log = logging.getLogger("mk.recorder")
# Loopback devices that fail are retried every rescan; log each one only once per recording.
_logged_failures: set[str] = set()


class RecorderError(Exception):
    pass


def _com_init() -> None:
    if sys.platform == "win32":
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED, like soundcard


def _keep_awake(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


class _SourceBuffer:
    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._size = 0
        self._lock = threading.Lock()

    def push(self, data: np.ndarray) -> None:
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            excess = self._size - MAX_BUFFER_SAMPLES
            while excess > 0 and self._chunks:
                first = self._chunks[0]
                if len(first) <= excess:
                    self._chunks.pop(0)
                    self._size -= len(first)
                    excess -= len(first)
                else:
                    self._chunks[0] = first[excess:]
                    self._size -= excess
                    excess = 0

    def take(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < n and self._chunks:
                first = self._chunks[0]
                k = min(n - filled, len(first))
                out[filled : filled + k] = first[:k]
                filled += k
                self._size -= k
                if k == len(first):
                    self._chunks.pop(0)
                else:
                    self._chunks[0] = first[k:]
        return out


class _Capture(threading.Thread):
    """Reads one device (mic or loopback) into a buffer until stopped or the device fails."""

    def __init__(self, device, name: str) -> None:
        super().__init__(daemon=True, name=f"capture:{name}")
        self.device = device
        self.device_name = name
        self.buffer = _SourceBuffer()
        self.failed = False
        self.peak = 0.0
        self._stop_event = threading.Event()

    def run(self) -> None:
        _com_init()
        try:
            with self.device.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK) as rec:
                while not self._stop_event.is_set():
                    data = np.ascontiguousarray(rec.record(numframes=BLOCK)[:, 0], dtype=np.float32)
                    self.peak = max(self.peak, float(np.abs(data).max()))
                    self.buffer.push(data)
        except Exception:  # noqa: BLE001 - device unplugged/disabled; the track pads silence
            self.failed = True
            if self.device_name not in _logged_failures:
                _logged_failures.add(self.device_name)
                log.warning("Falló la captura de '%s'", self.device_name, exc_info=True)

    def stop(self) -> None:
        self._stop_event.set()


class _Track:
    """One output file series (mic or pc) fed by one or more capture sources."""

    def __init__(self, ffmpeg: str, meeting: store.Meeting, track: str) -> None:
        self.name = track
        self.sources: dict[str, _Capture] = {}
        self.level = 0.0
        # Exact zeros = device muted/inactive; a quiet room is never exactly 0.
        self.silent_samples = 0
        pattern = meeting.audio_dir / f"{track}_%05d.ogg"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-af", _FILTERS[(meeting.mode, track)],
            "-c:a", "libopus", "-b:a", str(OPUS_BITRATE), "-application", "audio",
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
            str(pattern),
        ]
        self._log = open(meeting.folder / f"ffmpeg_{track}.log", "ab")
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=self._log, stderr=self._log, creationflags=_NO_WINDOW
        )
        self.broken = False

    def add_source(self, key: str, capture: _Capture) -> None:
        self.sources[key] = capture
        capture.start()

    def mix(self, n: int) -> np.ndarray:
        block = np.zeros(n, dtype=np.float32)
        for capture in list(self.sources.values()):
            block += capture.buffer.take(n)
        peak = float(np.abs(block).max()) if n else 0.0
        self.level = max(peak, self.level * 0.85)
        self.silent_samples = self.silent_samples + n if peak == 0.0 else 0
        return block

    def write(self, block: np.ndarray) -> None:
        if self.broken:
            return
        try:
            self.process.stdin.write(block.tobytes())
        except (BrokenPipeError, OSError):
            self.broken = True

    def close(self) -> None:
        for capture in self.sources.values():
            capture.stop()
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._log.close()


@dataclass
class RecorderStatus:
    recording: bool = False
    paused: bool = False
    meeting_id: str = ""
    elapsed: float = 0.0
    levels: dict[str, float] = field(default_factory=dict)
    size_bytes: int = 0
    mic_name: str = ""
    mic_silent: bool = False
    mic_muted: bool = False
    hotkey_label: str = ""
    warnings: list[str] = field(default_factory=list)


def _beep(muted: bool) -> None:
    if sys.platform != "win32":
        return
    import winsound

    tones = (700, 450) if muted else (450, 700)
    for freq in tones:
        winsound.Beep(freq, 90)


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Guards capture sources: changed by the UI (switch mic) and by the rescan.
        self._devices_lock = threading.Lock()
        self._meeting: Optional[store.Meeting] = None
        self._tracks: dict[str, _Track] = {}
        self._paused = False
        self._mic_muted = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recorded_samples = 0
        self._warnings: list[str] = []
        self._mic_name = ""
        self._hotkey = GlobalHotkey(self._on_hotkey)

    # ---- public API -------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._meeting is not None

    def start(self, meeting: store.Meeting) -> None:
        with self._lock:
            if self._meeting is not None:
                raise RecorderError("Ya hay una grabación en curso.")
            ffmpeg = find_ffmpeg()
            if ffmpeg is None:
                raise RecorderError(
                    "Falta ffmpeg. Instálalo con 'winget install --id Gyan.FFmpeg -e' y reinicia la app."
                )
            MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(MEETINGS_DIR).free < MIN_FREE_BYTES:
                raise RecorderError("Hay menos de 2 GB libres en el disco. Libera espacio antes de grabar.")

            self._meeting = meeting
            self._paused = False
            self._mic_muted = False
            self._recorded_samples = 0
            self._warnings = []
            self._stop_event.clear()
            _logged_failures.clear()
            try:
                self._tracks = {store.TRACK_MIC: _Track(ffmpeg, meeting, store.TRACK_MIC)}
                if meeting.mode == store.MODE_WEB:
                    self._tracks[store.TRACK_PC] = _Track(ffmpeg, meeting, store.TRACK_PC)

                mic = open_microphone(meeting.mic)
                self._mic_name = mic.name
                if mic.name != meeting.mic:
                    self._warnings.append(f"No se encontró '{meeting.mic}'; se usa '{mic.name}'.")
                self._tracks[store.TRACK_MIC].add_source(mic.name, _Capture(mic, mic.name))
                self._rescan_outputs()

                self._thread = threading.Thread(target=self._run, daemon=True, name="recorder")
                self._thread.start()
            except Exception:
                # Without this the recorder stays "recording" with orphan ffmpeg
                # processes and refuses every new recording until the app restarts.
                for track in self._tracks.values():
                    track.close()
                self._tracks = {}
                self._meeting = None
                log.exception("No se pudo iniciar la grabación de %s", meeting.id)
                raise
            if meeting.mode == store.MODE_WEB and not self._hotkey.start():
                self._warnings.append(
                    "Otras apps ya usan todos los atajos de teclado disponibles; usa el botón para silenciarte."
                )
            pc = self._tracks.get(store.TRACK_PC)
            log.info(
                "Grabación iniciada %s | modo=%s | mic=%s | salidas=%s | atajo=%s",
                meeting.id, meeting.mode, self._mic_name,
                [c.device_name for c in pc.sources.values()] if pc else "-", self._hotkey.label or "-",
            )

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def set_mic_muted(self, muted: bool) -> None:
        """Muted = the mic track records silence (timeline stays aligned); PC audio keeps recording."""
        self._mic_muted = muted

    def toggle_mic_mute(self) -> bool:
        self._mic_muted = not self._mic_muted
        return self._mic_muted

    def _on_hotkey(self) -> None:
        if self.is_recording:
            _beep(self.toggle_mic_mute())

    def change_microphone(self, name: str) -> str:
        """Switches the mic without stopping; returns the name actually in use."""
        with self._devices_lock:
            meeting = self._meeting
            if meeting is None:
                raise RecorderError("No hay una grabación en curso.")
            track = self._tracks[store.TRACK_MIC]
            mic = open_microphone(name)
            for capture in track.sources.values():
                capture.stop()
            track.sources.clear()
            track.silent_samples = 0
            track.add_source(mic.name, _Capture(mic, mic.name))
            self._mic_name = mic.name
            meeting.mic = mic.name
            store.save(meeting)
            return mic.name

    def stop(self) -> Optional[store.Meeting]:
        with self._lock:
            meeting = self._meeting
            if meeting is None:
                return None
            self._hotkey.stop()
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=10)
            for track in self._tracks.values():
                track.close()
            meeting.duration = self._recorded_samples / SAMPLE_RATE
            meeting.status = store.PENDING
            store.save(meeting)
            self._meeting = None
            self._tracks = {}
            log.info("Grabación detenida %s | duración=%.0fs", meeting.id, meeting.duration)
            return meeting

    def status(self) -> RecorderStatus:
        # Read both once: stop() may clear them from another thread at any moment.
        meeting, tracks = self._meeting, self._tracks
        mic_track = tracks.get(store.TRACK_MIC)
        if meeting is None or mic_track is None:
            return RecorderStatus()
        return RecorderStatus(
            recording=True,
            paused=self._paused,
            meeting_id=meeting.id,
            elapsed=self._recorded_samples / SAMPLE_RATE,
            levels={name: track.level for name, track in tracks.items()},
            # Windows reports 0 bytes for the segment ffmpeg still has open.
            size_bytes=max(
                store.folder_size(meeting.audio_dir),
                int(self._recorded_samples / SAMPLE_RATE * OPUS_BYTES_PER_SECOND * len(tracks)),
            ),
            mic_name=self._mic_name,
            mic_silent=mic_track.silent_samples > MIC_SILENT_ALERT_SAMPLES,
            mic_muted=self._mic_muted,
            hotkey_label=self._hotkey.label,
            warnings=list(self._warnings[-3:]),
        )

    # ---- internals --------------------------------------------------------

    def _rescan_outputs(self) -> None:
        """Attach loopback capture to every active output (new ones appear when a
        Bluetooth headset switches to its hands-free profile for a call)."""
        track = self._tracks.get(store.TRACK_PC)
        if track is None:
            return
        for key, capture in list(track.sources.items()):
            if capture.failed:
                del track.sources[key]
        try:
            speakers = sc.all_speakers()
        except Exception:  # noqa: BLE001 - enumeration can fail transiently while devices change
            return
        for speaker in speakers:
            if speaker.id in track.sources:
                continue
            try:
                loopback = sc.get_microphone(id=speaker.id, include_loopback=True)
            except Exception:  # noqa: BLE001
                continue
            track.add_source(speaker.id, _Capture(loopback, speaker.name))

    def _check_microphone(self) -> None:
        track = self._tracks[store.TRACK_MIC]
        (key, capture), = track.sources.items()
        if not capture.failed:
            return
        del track.sources[key]
        try:
            mic = open_microphone(self._meeting.mic)
        except Exception:  # noqa: BLE001 - no microphone at all right now; retry next tick
            track.sources[key] = capture
            return
        if mic.name != self._mic_name:
            self._warnings.append(f"Se desconectó '{self._mic_name}'; ahora se usa '{mic.name}'.")
            log.warning("Micrófono '%s' desconectado; se cambia a '%s'", self._mic_name, mic.name)
            self._mic_name = mic.name
        track.add_source(mic.name, _Capture(mic, mic.name))

    def _run(self) -> None:
        _com_init()
        _keep_awake(True)
        start = time.monotonic()
        pulled = 0
        next_rescan = start + RESCAN_SECONDS
        try:
            while not self._stop_event.is_set():
                time.sleep(0.1)
                now = time.monotonic()
                due = int((now - start) * SAMPLE_RATE) - pulled
                if due <= 0:
                    continue
                if due > MAX_BUFFER_SAMPLES:
                    # The PC was suspended or badly stalled: skip the gap instead of
                    # allocating (and writing) all of it as silence at once.
                    pulled += due - MAX_BUFFER_SAMPLES
                    due = MAX_BUFFER_SAMPLES
                pulled += due
                for track in self._tracks.values():
                    block = track.mix(due)
                    if track.name == store.TRACK_MIC and self._mic_muted:
                        block = np.zeros(due, dtype=np.float32)
                    if not self._paused:
                        track.write(block)
                if not self._paused:
                    self._recorded_samples += due
                if any(t.broken for t in self._tracks.values()):
                    msg = "El proceso de grabación (ffmpeg) se detuvo inesperadamente."
                    if msg not in self._warnings:
                        self._warnings.append(msg)
                        log.error("ffmpeg dejó de aceptar audio (¿disco lleno?); ver ffmpeg_*.log de la reunión")
                if now >= next_rescan:
                    next_rescan = now + RESCAN_SECONDS
                    with self._devices_lock:
                        self._check_microphone()
                        self._rescan_outputs()
        finally:
            _keep_awake(False)


def test_audio(mode: str, mic_name: str, seconds: float = 4.0) -> dict[str, float]:
    """Records a few seconds without saving; returns peak level per track."""
    _com_init()
    captures = {store.TRACK_MIC: [_Capture(open_microphone(mic_name), mic_name)]}
    if mode == store.MODE_WEB:
        captures[store.TRACK_PC] = []
        for speaker in sc.all_speakers():
            try:
                loopback = sc.get_microphone(id=speaker.id, include_loopback=True)
            except Exception:  # noqa: BLE001
                continue
            captures[store.TRACK_PC].append(_Capture(loopback, speaker.name))
    for group in captures.values():
        for capture in group:
            capture.start()
    time.sleep(seconds)
    levels = {}
    for track, group in captures.items():
        for capture in group:
            capture.stop()
        levels[track] = max((capture.peak for capture in group), default=0.0)
    return levels


recorder = Recorder()
