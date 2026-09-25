"""Formatting helpers shared by file and meeting transcripts."""

from __future__ import annotations

from transcription.engine import Segment


def timestamp(seconds: float, force_hours: bool = False) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h or force_hours:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def paragraphs(
    segments: list[Segment], max_seconds: float = 60.0, pause_seconds: float = 2.0
) -> list[tuple[float, str]]:
    """Groups segments into (start, text) paragraphs, breaking on long pauses or length."""
    result: list[tuple[float, str]] = []
    start = None
    parts: list[str] = []
    last_end = 0.0
    for seg in segments:
        new_paragraph = start is None or (
            seg.start - last_end > pause_seconds or seg.end - start > max_seconds
        )
        if new_paragraph and parts:
            result.append((start, " ".join(parts)))
            parts = []
        if new_paragraph:
            start = seg.start
        parts.append(seg.text)
        last_end = seg.end
    if parts:
        result.append((start, " ".join(parts)))
    return result
