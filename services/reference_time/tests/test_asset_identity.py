"""Regression tests for the audited asset-identity and cache-scope defects.

Audit items B (cache authorization/isolation), C (caller-supplied GP SHA) and
D (asset role/type validation) from
`docs/cursor-phase-2-clean-context-prompt.md` section 4. Each test was observed
failing against the pre-fix implementation.
"""

from __future__ import annotations

import pytest
from reference_time.cache import compute_cache_key, compute_source_evidence_key
from reference_time.mapping import DEFAULT_PARAMS
from reference_time.validation import (
    AssetRef,
    AssetValidationError,
    resolve_analysis_inputs,
)

GP_SHA = "a" * 64
MIDI_SHA = "b" * 64
AUDIO_SHA = "c" * 64
STRUCT_SHA = "d" * 64


def _asset(link_id: str, role: str, sha: str, name: str, media: str) -> AssetRef:
    return AssetRef(
        link_id=link_id,
        role=role,
        sha256=sha,
        original_filename=name,
        media_type=media,
        size_bytes=1024,
    )


def _default_assets() -> dict[str, AssetRef]:
    return {
        a.link_id: a
        for a in [
            _asset("gp1", "guitar-pro", GP_SHA, "song.gp5", "application/octet-stream"),
            _asset("mid1", "suno-midi.mix", MIDI_SHA, "song.mid", "audio/midi"),
            _asset("aud1", "mix", AUDIO_SHA, "mix.wav", "audio/wav"),
            _asset("st1", "structure", STRUCT_SHA, "structure.json", "application/json"),
        ]
    }


# --------------------------------------------------------------------------
# B. Cache authorization/isolation
# --------------------------------------------------------------------------

class TestDefectBCacheProjectScope:
    def test_identical_assets_in_two_projects_get_different_cache_keys(self):
        """Two projects with byte-identical assets must not collide."""
        common = dict(
            gp_revision_sha256=GP_SHA,
            gp_revision_number=1,
            source_midi_sha256s=[MIDI_SHA],
            audio_sha256s=[AUDIO_SHA],
            structure_sha256=STRUCT_SHA,
            processor_versions={"reference_time": "0.3.0"},
            parameters=DEFAULT_PARAMS,
        )
        key_a = compute_cache_key(project_id="project-a", **common)
        key_b = compute_cache_key(project_id="project-b", **common)
        assert key_a != key_b

    def test_source_evidence_key_is_project_scoped(self):
        common = dict(
            source_midi_sha256s=[MIDI_SHA],
            audio_sha256s=[AUDIO_SHA],
            processor_versions={"reference_time": "0.3.0"},
            parameters=DEFAULT_PARAMS,
        )
        assert (
            compute_source_evidence_key(project_id="project-a", **common)
            != compute_source_evidence_key(project_id="project-b", **common)
        )

    def test_cache_key_changes_when_gp_revision_number_changes(self):
        common = dict(
            project_id="p",
            gp_revision_sha256=GP_SHA,
            source_midi_sha256s=[MIDI_SHA],
            audio_sha256s=[],
            structure_sha256=None,
            processor_versions={"reference_time": "0.3.0"},
            parameters=DEFAULT_PARAMS,
        )
        assert compute_cache_key(gp_revision_number=1, **common) != compute_cache_key(
            gp_revision_number=2, **common
        )


# --------------------------------------------------------------------------
# C. Caller-supplied GP SHA handling
# --------------------------------------------------------------------------

class TestDefectCGpRevisionMismatch:
    def test_mismatching_claimed_gp_sha_is_rejected(self):
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(
                project_id="p",
                gp_asset_link_id="gp1",
                claimed_gp_revision_sha256="f" * 64,
                source_midi_link_ids=["mid1"],
                audio_link_ids=[],
                structure_link_id=None,
                assets=_default_assets(),
                gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
            )
        assert exc.value.code == "gp_revision_mismatch"

    def test_resolved_gp_sha_comes_from_metadata_not_caller(self):
        resolved = resolve_analysis_inputs(
            project_id="p",
            gp_asset_link_id="gp1",
            claimed_gp_revision_sha256=None,
            source_midi_link_ids=["mid1"],
            audio_link_ids=[],
            structure_link_id=None,
            assets=_default_assets(),
            gp_revisions=[{"revision": 3, "sha256": GP_SHA}],
        )
        assert resolved.gp.sha256 == GP_SHA
        assert resolved.gp_revision_number == 3

    def test_gp_asset_without_registered_revision_is_rejected(self):
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(
                project_id="p",
                gp_asset_link_id="gp1",
                claimed_gp_revision_sha256=GP_SHA,
                source_midi_link_ids=["mid1"],
                audio_link_ids=[],
                structure_link_id=None,
                assets=_default_assets(),
                gp_revisions=[],
            )
        assert exc.value.code == "gp_revision_not_found"


# --------------------------------------------------------------------------
# D. Asset role/type validation
# --------------------------------------------------------------------------

class TestDefectDRoleValidation:
    def test_wrong_project_asset_is_rejected(self):
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(
                project_id="p",
                gp_asset_link_id="gp1",
                claimed_gp_revision_sha256=GP_SHA,
                source_midi_link_ids=["not-in-project"],
                audio_link_ids=[],
                structure_link_id=None,
                assets=_default_assets(),
                gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
            )
        assert exc.value.code == "asset_not_in_project"

    @pytest.mark.parametrize(
        ("field", "link", "expected_role_family"),
        [
            ("gp_asset_link_id", "mid1", "guitar-pro"),
            ("source_midi_link_ids", "aud1", "source_midi"),
            ("audio_link_ids", "mid1", "audio"),
            ("structure_link_id", "aud1", "structure"),
        ],
    )
    def test_wrong_role_is_rejected(self, field, link, expected_role_family):
        kwargs = dict(
            project_id="p",
            gp_asset_link_id="gp1",
            claimed_gp_revision_sha256=GP_SHA,
            source_midi_link_ids=["mid1"],
            audio_link_ids=[],
            structure_link_id=None,
            assets=_default_assets(),
            gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
        )
        if field in ("source_midi_link_ids", "audio_link_ids"):
            kwargs[field] = [link]
        else:
            kwargs[field] = link
        if field == "gp_asset_link_id":
            kwargs["claimed_gp_revision_sha256"] = None
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(**kwargs)
        assert exc.value.code == "asset_role_invalid"
        assert expected_role_family in exc.value.message

    def test_missing_sha_is_rejected(self):
        assets = _default_assets()
        assets["mid1"] = _asset("mid1", "suno-midi.mix", "", "song.mid", "audio/midi")
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(
                project_id="p",
                gp_asset_link_id="gp1",
                claimed_gp_revision_sha256=GP_SHA,
                source_midi_link_ids=["mid1"],
                audio_link_ids=[],
                structure_link_id=None,
                assets=assets,
                gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
            )
        assert exc.value.code == "asset_missing_sha"

    def test_wrong_extension_for_role_is_rejected(self):
        assets = _default_assets()
        assets["mid1"] = _asset("mid1", "suno-midi.mix", MIDI_SHA, "song.wav", "audio/wav")
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(
                project_id="p",
                gp_asset_link_id="gp1",
                claimed_gp_revision_sha256=GP_SHA,
                source_midi_link_ids=["mid1"],
                audio_link_ids=[],
                structure_link_id=None,
                assets=assets,
                gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
            )
        assert exc.value.code == "asset_type_invalid"

    def test_duplicate_source_midi_links_are_deduplicated_deterministically(self):
        resolved = resolve_analysis_inputs(
            project_id="p",
            gp_asset_link_id="gp1",
            claimed_gp_revision_sha256=GP_SHA,
            source_midi_link_ids=["mid1", "mid1"],
            audio_link_ids=[],
            structure_link_id=None,
            assets=_default_assets(),
            gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
        )
        assert [a.link_id for a in resolved.source_midi] == ["mid1"]

    def test_empty_source_midi_is_rejected(self):
        with pytest.raises(AssetValidationError) as exc:
            resolve_analysis_inputs(
                project_id="p",
                gp_asset_link_id="gp1",
                claimed_gp_revision_sha256=GP_SHA,
                source_midi_link_ids=[],
                audio_link_ids=[],
                structure_link_id=None,
                assets=_default_assets(),
                gp_revisions=[{"revision": 1, "sha256": GP_SHA}],
            )
        assert exc.value.code == "no_source_midi"
