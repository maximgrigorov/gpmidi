"""Request/asset validation against trusted Asset API metadata.

Everything the caller sends is untrusted. The only authoritative facts are the
asset links the Asset API reports for the given project, their roles, media
types, digests and the project's registered GP revisions.

All failures raise `AssetValidationError` carrying a stable machine-readable
code so the API can answer with a structured, request-ID-bearing error and
create neither a job nor a cache entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

# Eligible roles per input slot. These mirror `asset_api.roles.AssetRole` but are
# duplicated deliberately: the analyzer must not silently widen its accepted
# inputs when a new role is added to the Asset API.
GP_ROLES = frozenset({"guitar-pro"})
SOURCE_MIDI_ROLES = frozenset(
    {
        "suno-midi.mix",
        "suno-midi.drums",
        "suno-midi.bass",
        "suno-midi.guitar",
        "suno-midi.other",
    }
)
AUDIO_ROLES = frozenset(
    {
        "mix",
        "stem.drums",
        "stem.bass",
        "stem.guitar",
        "stem.vocals",
        "stem.other",
    }
)
STRUCTURE_ROLES = frozenset({"structure"})

GP_EXTENSIONS = frozenset({".gp", ".gp3", ".gp4", ".gp5", ".gpx"})
MIDI_EXTENSIONS = frozenset({".mid", ".midi"})
AUDIO_EXTENSIONS = frozenset({".wav", ".flac"})
STRUCTURE_EXTENSIONS = frozenset({".json", ".txt", ".md"})

_SHA256_LENGTH = 64


class AssetValidationError(ValueError):
    """Fail-closed validation error with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AssetRef:
    """Trusted description of one project asset link."""

    link_id: str
    role: str
    sha256: str
    original_filename: str
    media_type: str = ""
    size_bytes: int = 0

    @classmethod
    def from_api(cls, payload: dict) -> AssetRef:
        return cls(
            link_id=str(payload.get("id") or payload.get("link_id") or ""),
            role=str(payload.get("role") or ""),
            sha256=str(payload.get("sha256") or ""),
            original_filename=str(payload.get("original_filename") or ""),
            media_type=str(payload.get("media_type") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
        )


@dataclass(frozen=True)
class ResolvedInputs:
    """Validated, trusted analysis inputs."""

    project_id: str
    gp: AssetRef
    gp_revision_number: int | None
    source_midi: tuple[AssetRef, ...]
    audio: tuple[AssetRef, ...]
    structure: AssetRef | None

    @property
    def source_midi_sha256s(self) -> list[str]:
        return [a.sha256 for a in self.source_midi]

    @property
    def audio_sha256s(self) -> list[str]:
        return [a.sha256 for a in self.audio]

    @property
    def structure_sha256(self) -> str | None:
        return self.structure.sha256 if self.structure else None

    def input_identities(self) -> dict[str, str]:
        identities = {
            "gp_revision": self.gp.sha256,
            "gp_asset_link": self.gp.link_id,
        }
        for a in self.source_midi:
            identities[f"source_midi_{a.link_id}"] = a.sha256
        for a in self.audio:
            identities[f"audio_{a.link_id}"] = a.sha256
        if self.structure:
            identities["structure"] = self.structure.sha256
        return identities


def _extension(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def _require_in_project(
    link_id: str, assets: dict[str, AssetRef], project_id: str, slot: str
) -> AssetRef:
    if not link_id:
        raise AssetValidationError(
            "asset_link_missing", f"No asset link supplied for {slot}"
        )
    asset = assets.get(link_id)
    if asset is None:
        raise AssetValidationError(
            "asset_not_in_project",
            f"Asset link {link_id} ({slot}) does not belong to project {project_id}",
        )
    return asset


def _require_role(asset: AssetRef, allowed: frozenset[str], slot: str) -> None:
    if asset.role not in allowed:
        raise AssetValidationError(
            "asset_role_invalid",
            f"Asset {asset.link_id} has role '{asset.role}' which is not eligible "
            f"for {slot}; expected one of {sorted(allowed)}",
        )


def _require_extension(asset: AssetRef, allowed: frozenset[str], slot: str) -> None:
    ext = _extension(asset.original_filename)
    if ext not in allowed:
        raise AssetValidationError(
            "asset_type_invalid",
            f"Asset {asset.link_id} ({slot}) has file type '{ext or 'none'}' which is "
            f"not eligible; expected one of {sorted(allowed)}",
        )


def _require_sha(asset: AssetRef, slot: str) -> None:
    if not asset.sha256 or len(asset.sha256) != _SHA256_LENGTH:
        raise AssetValidationError(
            "asset_missing_sha",
            f"Asset {asset.link_id} ({slot}) has no usable SHA-256 in Asset API metadata",
        )


def _dedupe(link_ids: list[str]) -> list[str]:
    """Deterministic de-duplication preserving first occurrence."""
    seen: set[str] = set()
    out: list[str] = []
    for lid in link_ids:
        if lid not in seen:
            seen.add(lid)
            out.append(lid)
    return out


def resolve_analysis_inputs(
    project_id: str,
    gp_asset_link_id: str,
    claimed_gp_revision_sha256: str | None,
    source_midi_link_ids: list[str],
    audio_link_ids: list[str],
    structure_link_id: str | None,
    assets: dict[str, AssetRef],
    gp_revisions: list[dict],
) -> ResolvedInputs:
    """Validate every requested input and return trusted identities.

    Raises `AssetValidationError` for wrong-project, missing, wrong-role,
    wrong-type, missing-SHA and mismatching-GP-revision inputs.
    """
    if not _dedupe(source_midi_link_ids):
        raise AssetValidationError(
            "no_source_midi", "At least one source MIDI asset is required"
        )

    gp = _require_in_project(gp_asset_link_id, assets, project_id, "guitar-pro input")
    _require_role(gp, GP_ROLES, "the guitar-pro input")
    _require_extension(gp, GP_EXTENSIONS, "the guitar-pro input")
    _require_sha(gp, "the guitar-pro input")

    # The GP asset must correspond to a revision the project actually registered.
    revision_number: int | None = None
    for rev in gp_revisions:
        rev_sha = str(rev.get("sha256") or rev.get("asset_sha256") or "")
        if rev_sha == gp.sha256:
            revision_number = int(rev.get("revision")) if rev.get("revision") is not None else None
            break
    if revision_number is None:
        raise AssetValidationError(
            "gp_revision_not_found",
            f"GP asset {gp.link_id} is not registered as a GP revision of project {project_id}",
        )

    # A claimed digest is only ever an assertion to be checked, never authority.
    if claimed_gp_revision_sha256 and claimed_gp_revision_sha256 != gp.sha256:
        raise AssetValidationError(
            "gp_revision_mismatch",
            f"Claimed gp_revision_sha256 does not match the resolved GP revision "
            f"for asset {gp.link_id}",
        )

    source_midi: list[AssetRef] = []
    for lid in _dedupe(source_midi_link_ids):
        asset = _require_in_project(lid, assets, project_id, "source_midi input")
        _require_role(asset, SOURCE_MIDI_ROLES, "a source_midi input")
        _require_extension(asset, MIDI_EXTENSIONS, "a source_midi input")
        _require_sha(asset, "a source_midi input")
        source_midi.append(asset)

    audio: list[AssetRef] = []
    for lid in _dedupe(audio_link_ids):
        asset = _require_in_project(lid, assets, project_id, "audio input")
        _require_role(asset, AUDIO_ROLES, "an audio input")
        _require_extension(asset, AUDIO_EXTENSIONS, "an audio input")
        _require_sha(asset, "an audio input")
        audio.append(asset)

    structure: AssetRef | None = None
    if structure_link_id:
        structure = _require_in_project(
            structure_link_id, assets, project_id, "structure input"
        )
        _require_role(structure, STRUCTURE_ROLES, "the structure input")
        _require_extension(structure, STRUCTURE_EXTENSIONS, "the structure input")
        _require_sha(structure, "the structure input")

    # Deterministic ordering: source MIDI and audio are sorted by digest so the
    # analysis body and cache identity do not depend on request ordering.
    return ResolvedInputs(
        project_id=project_id,
        gp=gp,
        gp_revision_number=revision_number,
        source_midi=tuple(sorted(source_midi, key=lambda a: (a.sha256, a.link_id))),
        audio=tuple(sorted(audio, key=lambda a: (a.sha256, a.link_id))),
        structure=structure,
    )
