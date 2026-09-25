"""Audio device discovery (Windows WASAPI via soundcard)."""

from __future__ import annotations

import warnings

import soundcard as sc

from meetings.store import MODE_IN_PERSON

warnings.filterwarnings("ignore", message="data discontinuity in recording")

_HEADSET_WORDS = ("headset", "hands-free", "manos libres", "auricular")


def list_microphones() -> list[str]:
    return [m.name for m in sc.all_microphones()]


def list_outputs() -> list[str]:
    return [s.name for s in sc.all_speakers()]


def default_microphone() -> str:
    try:
        return sc.default_microphone().name
    except Exception:  # noqa: BLE001 - no default device configured
        mics = list_microphones()
        return mics[0] if mics else ""


def _is_headset(name: str) -> bool:
    return any(word in name.lower() for word in _HEADSET_WORDS)


def suggest_microphone(mode: str, last_mic: str = "") -> str:
    """In person: the laptop's built-in mic picks up the whole room.
    Web: the last mic used, else the Windows default. A Bluetooth headset mic
    only sends sound while it's in hands-free mode, so it is never assumed."""
    mics = list_microphones()
    if not mics:
        return ""
    if mode == MODE_IN_PERSON:
        built_in = [m for m in mics if not _is_headset(m)]
        return built_in[0] if built_in else default_microphone()
    return last_mic if last_mic in mics else default_microphone()


def open_microphone(name: str):
    """Returns the soundcard microphone by name, or the default one if it's gone."""
    for mic in sc.all_microphones():
        if mic.name == name:
            return mic
    return sc.default_microphone()
