"""Asset roles registry and validation helpers."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import PurePosixPath


class AssetRole(str, Enum):
    MIX = "mix"
    STEM_DRUMS = "stem.drums"
    STEM_BASS = "stem.bass"
    STEM_GUITAR = "stem.guitar"
    STEM_VOCALS = "stem.vocals"
    STEM_OTHER = "stem.other"
    SUNO_MIDI_MIX = "suno-midi.mix"
    SUNO_MIDI_DRUMS = "suno-midi.drums"
    SUNO_MIDI_BASS = "suno-midi.bass"
    SUNO_MIDI_GUITAR = "suno-midi.guitar"
    SUNO_MIDI_OTHER = "suno-midi.other"
    LYRICS = "lyrics"
    STRUCTURE = "structure"
    GUITAR_PRO = "guitar-pro"


AUDIO_ROLES = {AssetRole.MIX, AssetRole.STEM_DRUMS, AssetRole.STEM_BASS,
               AssetRole.STEM_GUITAR, AssetRole.STEM_VOCALS, AssetRole.STEM_OTHER}
MIDI_ROLES = {AssetRole.SUNO_MIDI_MIX, AssetRole.SUNO_MIDI_DRUMS,
              AssetRole.SUNO_MIDI_BASS, AssetRole.SUNO_MIDI_GUITAR,
              AssetRole.SUNO_MIDI_OTHER}
TEXT_ROLES = {AssetRole.LYRICS, AssetRole.STRUCTURE}
GP_ROLES = {AssetRole.GUITAR_PRO}

AUDIO_EXTENSIONS = {".wav", ".flac"}
MIDI_EXTENSIONS = {".mid", ".midi"}
GP_EXTENSIONS = {".gp", ".gp3", ".gp4", ".gp5", ".gpx"}
TEXT_EXTENSIONS = {".txt", ".md", ".json"}

# Magic bytes for cheap header validation
SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    ".wav": [(0, b"RIFF"), (8, b"WAVE")],
    ".flac": [(0, b"fLaC")],
    ".mid": [(0, b"MThd")],
    ".midi": [(0, b"MThd")],
    ".gp3": [(0, b"FICHIER GUITAR PRO v3")],
    ".gp4": [(0, b"FICHIER GUITAR PRO v4")],
    ".gp5": [(0, b"FICHIER GUITAR PRO v5")],
    ".gpx": [(0, b"BCFZ")],
    ".gp": [(0, b"BCFZ")],
}

_UNSAFE_CHARS = re.compile(r'[/\\:\x00-\x1f\x7f]')
_TRAVERSAL = re.compile(r'(^|[\\/])\.\.($|[\\/])')


def allowed_extensions_for_role(role: AssetRole) -> set[str]:
    if role in AUDIO_ROLES:
        return AUDIO_EXTENSIONS
    if role in MIDI_ROLES:
        return MIDI_EXTENSIONS
    if role in GP_ROLES:
        return GP_EXTENSIONS
    if role in TEXT_ROLES:
        return TEXT_EXTENSIONS
    return set()


def validate_extension(filename: str, role: AssetRole) -> str | None:
    """Return normalized lowercase extension or None if invalid."""
    ext = PurePosixPath(filename).suffix.lower()
    if ext in allowed_extensions_for_role(role):
        return ext
    return None


def validate_signature(header: bytes, extension: str) -> bool:
    """Cheap magic-bytes check. Returns True if valid or unknown extension."""
    sigs = SIGNATURES.get(extension)
    if not sigs:
        return True
    for offset, magic in sigs:
        if len(header) < offset + len(magic):
            return False
        if header[offset:offset + len(magic)] != magic:
            return False
    return True


def sanitize_filename(name: str) -> str:
    """Strip dangerous characters; raise ValueError if traversal detected."""
    if not name or len(name) > 255:
        raise ValueError("filename too long or empty")
    if _TRAVERSAL.search(name):
        raise ValueError("path traversal")
    if _UNSAFE_CHARS.search(name):
        raise ValueError("unsafe characters")
    if name.startswith("."):
        raise ValueError("hidden file")
    return name


def max_bytes_for_role(role: AssetRole) -> int:
    from .config import MAX_AUDIO_BYTES, MAX_MIDI_BYTES, MAX_GP_BYTES, MAX_TEXT_BYTES
    if role in AUDIO_ROLES:
        return MAX_AUDIO_BYTES
    if role in MIDI_ROLES:
        return MAX_MIDI_BYTES
    if role in GP_ROLES:
        return MAX_GP_BYTES
    if role in TEXT_ROLES:
        return MAX_TEXT_BYTES
    return MAX_TEXT_BYTES
