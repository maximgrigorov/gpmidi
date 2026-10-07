from __future__ import annotations

MUSCRIPTOR_INSTRUMENTS = (
    "acoustic_piano",
    "electric_piano",
    "chromatic_percussion",
    "organ",
    "acoustic_guitar",
    "clean_electric_guitar",
    "distorted_electric_guitar",
    "acoustic_bass",
    "electric_bass",
    "violin",
    "viola",
    "cello",
    "contrabass",
    "orchestral_harp",
    "timpani",
    "string_ensemble",
    "synth_strings",
    "voice",
    "orchestra_hit",
    "trumpet",
    "trombone",
    "tuba",
    "french_horn",
    "brass_section",
    "soprano_and_alto_sax",
    "tenor_sax",
    "baritone_sax",
    "oboe",
    "english_horn",
    "bassoon",
    "clarinet",
    "flutes",
    "synth_lead",
    "synth_pad",
    "drums",
)
MUSCRIPTOR_INSTRUMENT_SET = frozenset(MUSCRIPTOR_INSTRUMENTS)


def validate_instruments(transcriber: str, instruments: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(item.strip() for item in instruments if item.strip()))
    unknown = [item for item in normalized if item not in MUSCRIPTOR_INSTRUMENT_SET]
    if unknown:
        raise ValueError(f"unknown MuScriptor instrument: {unknown[0]}")
    if normalized and transcriber != "muscriptor-large":
        raise ValueError("instrument filtering is only supported by MuScriptor")
    return normalized
