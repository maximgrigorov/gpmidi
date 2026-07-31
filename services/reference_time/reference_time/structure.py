"""Structure JSON parsing for section/anchor constraints.

Strict, versioned and fail-closed: every distinct malformation raises
`StructureError` with its own stable code rather than collapsing into one
generic message. Nothing here is best-effort — an anchor the user supplied but
which cannot be honoured must stop the analysis, not silently degrade it.

Supported document (version "1.0"):

```json
{
  "version": "1.0",
  "sections": [
    {"label": "Verse 1", "source_measure": 0, "gp_measure": 0}
  ],
  "anchors": [
    {"source_measure": 4, "gp_measure": 8, "label": "Chorus start"},
    {"source_seconds": 32.5, "gp_measure": 16}
  ]
}
```

An anchor identifies its source position either by `source_measure` (integer
index) or by `source_seconds` (a timestamp, converted to the measure containing
that instant). `gp_measure` is always an integer index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

SUPPORTED_VERSIONS = frozenset({"1.0"})


class StructureError(ValueError):
    """Structure/anchor contract violation with a stable code.

    Subclasses ValueError so that callers which fail closed on bad input keep
    working, while the `code` attribute carries the stable machine-readable id.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StructureSection:
    """A named section of the source timeline."""

    label: str
    source_measure_index: int | None = None
    gp_measure_index: int | None = None


@dataclass(frozen=True)
class StructureParseResult:
    anchors: tuple  # tuple[AnchorConstraint, ...]
    sections: tuple[StructureSection, ...]
    version: str


def _require_object(entry: object, where: str) -> dict:
    if not isinstance(entry, dict):
        raise StructureError(
            "structure_entry_not_object", f"{where} entry must be a JSON object"
        )
    return entry


def _require_int(value: object, field: str, where: str) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise StructureError(
            "structure_index_not_integer",
            f"{where}: field '{field}' must be an integer, got {value!r}",
        )
    return value


def _require_number(value: object, field: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructureError(
            "structure_index_not_integer",
            f"{where}: field '{field}' must be a number, got {value!r}",
        )
    return float(value)


def _check_source_range(idx: int, n_source_measures: int) -> None:
    if idx < 0 or idx >= n_source_measures:
        raise StructureError(
            "structure_source_index_out_of_range",
            f"source_measure {idx} out of range [0, {n_source_measures})",
        )


def _check_gp_range(idx: int, n_gp_measures: int) -> None:
    if idx < 0 or idx >= n_gp_measures:
        raise StructureError(
            "structure_gp_index_out_of_range",
            f"gp_measure {idx} out of range [0, {n_gp_measures})",
        )


def _measure_index_for_seconds(seconds: float, source_measures: list) -> int:
    for m in source_measures:
        if m.seconds_start <= seconds < m.seconds_end:
            return m.index
    # Exactly on the final boundary still belongs to the last measure.
    if source_measures and seconds == source_measures[-1].seconds_end:
        return source_measures[-1].index
    raise StructureError(
        "structure_source_index_out_of_range",
        f"Anchor source_seconds {seconds} falls outside the source timeline",
    )


def _reduce_anchors(
    candidates: list[tuple[int, int, str | None, str]],
) -> list[tuple[int, int, str | None]]:
    """Collapse identical duplicates, reject genuine conflicts."""
    by_source: dict[int, tuple[int, str | None]] = {}
    by_gp: dict[int, int] = {}

    for src, gp, label, _origin in candidates:
        if src in by_source and by_source[src][0] != gp:
            raise StructureError(
                "structure_conflicting_anchor",
                f"Conflicting anchors: source measure {src} is mapped to both GP "
                f"{by_source[src][0]} and GP {gp}",
            )
        if gp in by_gp and by_gp[gp] != src:
            raise StructureError(
                "structure_conflicting_anchor",
                f"Conflicting anchors: GP measure {gp} is mapped from both source "
                f"{by_gp[gp]} and source {src}",
            )
        by_source.setdefault(src, (gp, label))
        by_gp.setdefault(gp, src)

    return [(src, gp, label) for src, (gp, label) in sorted(by_source.items())]


def validate_anchor_monotonicity(anchors: list[tuple[int, int]]) -> None:
    """Anchors must be strictly increasing in both source and GP index."""
    ordered = sorted(anchors, key=lambda a: a[0])
    for i in range(1, len(ordered)):
        prev_src, prev_gp = ordered[i - 1][0], ordered[i - 1][1]
        curr_src, curr_gp = ordered[i][0], ordered[i][1]
        if curr_gp <= prev_gp:
            raise StructureError(
                "structure_non_monotonic_anchors",
                f"Non-monotonic anchors: source {prev_src}->GP {prev_gp} then "
                f"source {curr_src}->GP {curr_gp}",
            )


def parse_structure_json(
    data: bytes,
    source_measures: list,
    n_gp_measures: int,
) -> StructureParseResult:
    """Parse and validate structure JSON, producing hard anchor constraints."""
    from .mapping import AnchorConstraint

    n_source_measures = len(source_measures)

    try:
        doc = json.loads(data)
    except UnicodeDecodeError as e:
        raise StructureError(
            "structure_malformed_json", f"Structure input is not valid UTF-8: {e}"
        ) from e
    except json.JSONDecodeError as e:
        raise StructureError(
            "structure_malformed_json", f"Malformed structure JSON: {e}"
        ) from e

    if not isinstance(doc, dict):
        raise StructureError(
            "structure_root_not_object", "Structure JSON root must be an object"
        )

    version = doc.get("version")
    if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
        raise StructureError(
            "structure_unsupported_version",
            f"Unsupported structure version {version!r}; supported: "
            f"{sorted(SUPPORTED_VERSIONS)}",
        )

    raw_sections = doc.get("sections", [])
    if not isinstance(raw_sections, list):
        raise StructureError(
            "structure_entry_not_object", "'sections' must be a JSON array"
        )
    raw_anchors = doc.get("anchors", [])
    if not isinstance(raw_anchors, list):
        raise StructureError(
            "structure_entry_not_object", "'anchors' must be a JSON array"
        )

    sections: list[StructureSection] = []
    # (source_index, gp_index, label, origin)
    candidates: list[tuple[int, int, str | None, str]] = []

    for raw in raw_sections:
        entry = _require_object(raw, "sections")
        label = entry.get("label", entry.get("name", ""))
        if not isinstance(label, str):
            raise StructureError(
                "structure_index_not_integer",
                f"sections: field 'label' must be a string, got {label!r}",
            )
        src_raw = entry.get("source_measure")
        gp_raw = entry.get("gp_measure")

        src_idx: int | None = None
        if src_raw is not None:
            src_idx = _require_int(src_raw, "source_measure", "sections")
            _check_source_range(src_idx, n_source_measures)
        gp_idx: int | None = None
        if gp_raw is not None:
            gp_idx = _require_int(gp_raw, "gp_measure", "sections")
            _check_gp_range(gp_idx, n_gp_measures)

        sections.append(
            StructureSection(
                label=label, source_measure_index=src_idx, gp_measure_index=gp_idx
            )
        )
        if src_idx is not None and gp_idx is not None:
            candidates.append((src_idx, gp_idx, label or None, "section"))

    for raw in raw_anchors:
        entry = _require_object(raw, "anchors")
        label = entry.get("label")
        if label is not None and not isinstance(label, str):
            raise StructureError(
                "structure_index_not_integer",
                f"anchors: field 'label' must be a string, got {label!r}",
            )

        has_measure = "source_measure" in entry
        has_seconds = "source_seconds" in entry
        if not has_measure and not has_seconds:
            raise StructureError(
                "structure_field_missing",
                "anchors: each anchor needs 'source_measure' or 'source_seconds'",
            )
        if has_measure and has_seconds:
            raise StructureError(
                "structure_conflicting_anchor",
                "anchors: 'source_measure' and 'source_seconds' are mutually exclusive",
            )
        if "gp_measure" not in entry:
            raise StructureError(
                "structure_field_missing", "anchors: each anchor needs 'gp_measure'"
            )

        if has_measure:
            src_idx = _require_int(entry["source_measure"], "source_measure", "anchors")
            _check_source_range(src_idx, n_source_measures)
        else:
            seconds = _require_number(
                entry["source_seconds"], "source_seconds", "anchors"
            )
            src_idx = _measure_index_for_seconds(seconds, source_measures)

        gp_idx = _require_int(entry["gp_measure"], "gp_measure", "anchors")
        _check_gp_range(gp_idx, n_gp_measures)
        candidates.append((src_idx, gp_idx, label, "anchor"))

    reduced = _reduce_anchors(candidates)
    validate_anchor_monotonicity([(src, gp) for src, gp, _ in reduced])

    return StructureParseResult(
        anchors=tuple(
            AnchorConstraint(source_measure_index=src, gp_measure_index=gp, label=label)
            for src, gp, label in reduced
        ),
        sections=tuple(sections),
        version=version,
    )
