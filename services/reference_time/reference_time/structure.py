"""Structure JSON parsing for section/anchor constraints.

Versioned structure format expected:

```json
{
  "version": "1.0",
  "sections": [
    {
      "label": "Verse 1",
      "source_measure": 0,
      "gp_measure": 0
    }
  ],
  "anchors": [
    {
      "source_measure": 4,
      "gp_measure": 8,
      "label": "Chorus start"
    }
  ]
}
```
"""

from __future__ import annotations

import json

from .mapping import AnchorConstraint
from .models import Warning, WarningCode

SUPPORTED_VERSIONS = {"1.0"}


class StructureParseResult:
    """Parsed structure with anchors and section evidence."""

    def __init__(
        self,
        anchors: list[AnchorConstraint],
        section_labels: dict[int, str],
        warnings: list[Warning],
    ):
        self.anchors = anchors
        self.section_labels = section_labels
        self.warnings = warnings


def parse_structure_json(
    data: bytes,
    n_source_measures: int,
    n_gp_measures: int,
) -> StructureParseResult:
    """Parse and validate structure JSON, producing anchor constraints.

    Validates:
    - JSON well-formedness
    - Version field
    - Anchor indices in range
    - No duplicate/conflicting anchors
    - Monotonicity
    """
    warnings: list[Warning] = []

    try:
        doc = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Malformed structure JSON: {e}") from e

    if not isinstance(doc, dict):
        raise ValueError("Structure JSON root must be an object")

    version = doc.get("version", "")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"Unsupported structure version '{version}'; "
            f"supported: {sorted(SUPPORTED_VERSIONS)}"
        )

    anchors: list[AnchorConstraint] = []
    section_labels: dict[int, str] = {}

    for section in doc.get("sections", []):
        if not isinstance(section, dict):
            warnings.append(Warning(
                code=WarningCode.ANCHOR_CONFLICT,
                message="Section entry is not an object; skipped",
            ))
            continue
        label = section.get("label", "")
        src = section.get("source_measure")
        gp = section.get("gp_measure")
        if src is not None and isinstance(src, int):
            section_labels[src] = str(label)
        if src is not None and gp is not None:
            if not isinstance(src, int) or not isinstance(gp, int):
                warnings.append(Warning(
                    code=WarningCode.ANCHOR_CONFLICT,
                    message=f"Section anchor has non-integer indices: src={src}, gp={gp}",
                ))
                continue
            anchors.append(AnchorConstraint(
                source_measure_index=src,
                gp_measure_index=gp,
                label=str(label) if label else None,
            ))

    for anchor_entry in doc.get("anchors", []):
        if not isinstance(anchor_entry, dict):
            warnings.append(Warning(
                code=WarningCode.ANCHOR_CONFLICT,
                message="Anchor entry is not an object; skipped",
            ))
            continue
        src = anchor_entry.get("source_measure")
        gp = anchor_entry.get("gp_measure")
        label = anchor_entry.get("label")
        if src is None or gp is None:
            warnings.append(Warning(
                code=WarningCode.ANCHOR_CONFLICT,
                message=f"Anchor missing source_measure or gp_measure: {anchor_entry}",
            ))
            continue
        if not isinstance(src, int) or not isinstance(gp, int):
            warnings.append(Warning(
                code=WarningCode.ANCHOR_CONFLICT,
                message=f"Anchor has non-integer indices: src={src}, gp={gp}",
            ))
            continue
        if src < 0 or src >= n_source_measures:
            raise ValueError(
                f"Anchor source_measure {src} out of range [0, {n_source_measures})"
            )
        if gp < 0 or gp >= n_gp_measures:
            raise ValueError(
                f"Anchor gp_measure {gp} out of range [0, {n_gp_measures})"
            )
        anchors.append(AnchorConstraint(
            source_measure_index=src,
            gp_measure_index=gp,
            label=str(label) if label else None,
        ))

    # Validate no duplicate source indices with conflicting GP
    seen_src: dict[int, int] = {}
    for a in anchors:
        if a.source_measure_index in seen_src:
            prev_gp = seen_src[a.source_measure_index]
            if prev_gp != a.gp_measure_index:
                raise ValueError(
                    f"Conflicting anchors for source measure {a.source_measure_index}: "
                    f"gp={prev_gp} vs gp={a.gp_measure_index}"
                )
        seen_src[a.source_measure_index] = a.gp_measure_index

    # Validate monotonicity
    sorted_anchors = sorted(anchors, key=lambda a: a.source_measure_index)
    for i in range(1, len(sorted_anchors)):
        prev = sorted_anchors[i - 1]
        curr = sorted_anchors[i]
        if curr.gp_measure_index <= prev.gp_measure_index:
            raise ValueError(
                f"Non-monotonic anchors: source {prev.source_measure_index}->gp "
                f"{prev.gp_measure_index}, source {curr.source_measure_index}->gp "
                f"{curr.gp_measure_index}"
            )

    # Deduplicate
    unique: dict[int, AnchorConstraint] = {}
    for a in anchors:
        unique[a.source_measure_index] = a
    anchors = list(unique.values())

    return StructureParseResult(
        anchors=anchors,
        section_labels=section_labels,
        warnings=warnings,
    )
