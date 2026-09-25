"""Meeting records: one folder per meeting with a self-describing meeting.json.

Layout: <MEETINGS_DIR>/<year>/<id>/{meeting.json, transcripcion.md, audio/}
No database: the history is rebuilt by reading the json files.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from settings import MEETINGS_DIR

MODE_WEB = "web"
MODE_IN_PERSON = "presencial"

RECORDING = "grabando"
PENDING = "pendiente"
TRANSCRIBING = "transcribiendo"
DONE = "lista"
ERROR = "error"

TRACK_MIC = "mic"
TRACK_PC = "pc"

_write_lock = threading.Lock()


@dataclass
class Meeting:
    id: str
    title: str
    mode: str
    started_at: str
    mic: str = ""
    language: str = "es-MX"
    model_size: str = "small"
    status: str = RECORDING
    duration: float = 0.0
    interrupted: bool = False
    audio_deleted: bool = False
    error: str = ""
    transcribed_at: str = ""

    @property
    def folder(self) -> Path:
        return MEETINGS_DIR / self.id[:4] / self.id

    @property
    def audio_dir(self) -> Path:
        return self.folder / "audio"

    @property
    def transcript_path(self) -> Path:
        return self.folder / "transcripcion.md"

    @property
    def started(self) -> datetime:
        return datetime.fromisoformat(self.started_at)

    def track_path(self, track: str) -> Path:
        return self.audio_dir / f"{track}.ogg"

    @property
    def listen_path(self) -> Path:
        """The file to play back: both voices mixed (web) or the only track (in person)."""
        if self.mode == MODE_WEB:
            return self.audio_dir / "reunion_completa.ogg"
        return self.track_path(TRACK_MIC)

    def segment_paths(self, track: str) -> list[Path]:
        return sorted(self.audio_dir.glob(f"{track}_*.ogg"))

    def read_transcript(self) -> str:
        try:
            return self.transcript_path.read_text(encoding="utf-8")
        except OSError:
            return ""


def _slug(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", title).strip(" .")
    return re.sub(r"\s+", " ", cleaned)[:50] or "Reunion"


def save(meeting: Meeting) -> None:
    with _write_lock:
        meeting.folder.mkdir(parents=True, exist_ok=True)
        path = meeting.folder / "meeting.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(meeting), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def create(title: str, mode: str, mic: str, language: str, model_size: str) -> Meeting:
    now = datetime.now().replace(microsecond=0)
    meeting = Meeting(
        id=f"{now:%Y-%m-%d_%H-%M-%S}_{_slug(title)}",
        title=title.strip() or "Reunión",
        mode=mode,
        started_at=now.isoformat(),
        mic=mic,
        language=language,
        model_size=model_size,
    )
    meeting.audio_dir.mkdir(parents=True, exist_ok=True)
    save(meeting)
    return meeting


def _load(path: Path) -> Optional[Meeting]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    known = {f.name for f in fields(Meeting)}
    try:
        return Meeting(**{k: v for k, v in data.items() if k in known})
    except TypeError:
        return None


def list_meetings() -> list[Meeting]:
    meetings = [m for p in MEETINGS_DIR.glob("*/*/meeting.json") if (m := _load(p))]
    return sorted(meetings, key=lambda m: m.started_at, reverse=True)


def get(meeting_id: str) -> Optional[Meeting]:
    return _load(MEETINGS_DIR / meeting_id[:4] / meeting_id / "meeting.json")


def delete(meeting: Meeting) -> None:
    shutil.rmtree(meeting.folder, ignore_errors=True)


def recover_unfinished() -> list[Meeting]:
    """Called at startup. Returns meetings that still need transcription.

    A meeting left in RECORDING means the app was closed mid-recording:
    the saved segments are kept and transcribed as an interrupted meeting.
    """
    pending = []
    for meeting in list_meetings():
        if meeting.status == RECORDING:
            meeting.interrupted = True
            meeting.status = PENDING
            save(meeting)
        elif meeting.status == TRANSCRIBING:
            meeting.status = PENDING
            save(meeting)
        if meeting.status == PENDING:
            pending.append(meeting)
    return sorted(pending, key=lambda m: m.started_at)


def apply_retention(days: int) -> int:
    """Deletes the audio of transcribed meetings older than `days`. Returns bytes freed."""
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    freed = 0
    for meeting in list_meetings():
        if meeting.status != DONE or meeting.audio_deleted or meeting.started >= cutoff:
            continue
        freed += folder_size(meeting.audio_dir)
        shutil.rmtree(meeting.audio_dir, ignore_errors=True)
        meeting.audio_deleted = True
        save(meeting)
    return freed


def folder_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
