# Phase 3 drum candidate feasibility and licensing

Date: 2026-08-02

## Decision scope

This is the Task 5 pre-implementation gate. A repository, a public checkpoint,
or a successful dependency resolution by itself does not make a candidate
runnable. The gate requires an attributable permissive code and checkpoint
license, a pinned install, inference, parsed output and reproducible resource
and failure/success evidence. No candidate below passed all of those gates, so
no drum runtime or product-quality claim is retained from this session.

The fixed real-input identities discovered through the live Asset API are:

- Spring Melody project: `d11b0cab-54ae-49a9-b308-f8ccdab976ba`;
- `stem.drums` WAV: `debb6cf9bd3236d4a6ca1f89503cc3f7b7ea2577128d2596dd80f7bba105d186`, 58,243,644 bytes;
- original Suno drum MIDI: `034075af9b4752e80a61ba1f2d8bbedf97e3a0c8a571988aa6282b58edf3dfb8`, 10,769 bytes.

These assets were downloaded and read back with the declared hashes during the
feasibility investigation. They were not added to the branch because no
candidate reached the runnable gate.

## Omnizart 0.6.3

### Evidence

- Upstream: <https://github.com/Music-and-Culture-Technology-Lab/omnizart>
- inspected tag/commit: `v0.6.3` / `bcd8cb44d4da66ce87df10b6abee5c35a8cc2886` (2026-05-31);
- source license: MIT, repository `LICENSE` SHA-256
  `002bc4af354b6329ffe76a96996029e9d4af74be8c497c0aaa52db1a68e47626`;
- PyPI sdist: `omnizart-0.6.3.tar.gz`, SHA-256
  `66b96cd3a19c75e54339190cddd151aa113bbae6a0fde98792e7fb6812ac45c1`;
- historical checkpoint release/tag:
  <https://github.com/Music-and-Culture-Technology-Lab/omnizart/releases/tag/checkpoints-20211001>,
  commit `615539554ab86fbf29d137a5a7ce2f761db752c1`;
- drum checkpoint files:
  - `configurations.yaml`: `2f7b0b0b7f41f324b8ac38e8cdd53df949a965f09d4ee1f7fb35bd9d76ae6755`;
  - `saved_model.pb`: `6536f2fd67472cf8fcaea89888410eb6c09b4e3ac72664cb9fffe7441aa6559b`;
  - `variables.index`: `dde6e23dd1a5d2452a8c62cc08ea3e73b8a482d7f285103870587cf0ac958600`;
  - downloaded `variables.data-00000-of-00001` (31,090,686 bytes):
    `9e5105215ec8bdee5ebba57c2eef8655a310de99da9f3169b3f857e2185ab464`.

The checkpoint files and download mapping are in the MIT-licensed repository
and its own GitHub release. No conflicting checkpoint-specific terms were found.
That is sufficient to keep Omnizart as the most plausible permissive candidate,
but it is not by itself runtime acceptance.

### Reproducibility blocker

A RED contract suite was written and run before implementation; all seven tests
failed because the runtime/container/pipeline did not exist. The minimal Python
runtime then made the pure contract tests pass, but it was discarded when the
pinned dependency gate could not be made reproducible without redesigning the
upstream package.

Three isolated lock attempts established the root cause rather than guessing:

1. `uv pip compile` failed while reading `madmom==0.16.1` metadata because
   madmom imports undeclared build dependency `Cython`.
2. A pip-tools run with Cython/NumPy supplied failed because pip-tools 7.5.2 was
   incompatible with the selected pip API (`allow_editables`).
3. After pinning a compatible pip, Omnizart's own `setup.py` executed nested
   installers during metadata generation. It built and installed madmom/vamp,
   then attempted to compile PyAudio and failed because `portaudio.h` was not
   present. The setup script also attempts to install packages and system-bound
   audio dependencies as import-time/setup-time side effects.

The third failure is architectural for a hash-locked image: dependency metadata
resolution itself mutates the environment and invokes nested, non-hash-locked
installs. Adding `portaudio19-dev` would address only the observed symptom; it
would not produce the required auditable lock. After three failed approaches,
the implementation was stopped and all incomplete runtime/test/fixture files
were removed instead of committing a facade.

**Interim decision: no-go as the first minimal runnable path.** Omnizart may be
revisited only as a separately scoped, test-first repack that installs an exact
source archive with `--no-deps` after independently hash-locking the minimal drum
runtime dependency graph and proves that the repack is behaviorally equivalent.
That is not a small Phase 3 continuation.

## ADTLib 2.1.2

### Evidence

- Upstream: <https://github.com/CarlSouthall/ADTLib>
- inspected commit: `8e429f38736e7c8ef94e06314eaa1d1ac7dd30fe` (2018-01-17);
- PyPI sdist SHA-256:
  `0b5f84dd9bfd27c71b294f718b6e249cb0d220127552c741e1a448b7e9d18c43`;
- repository `LICENSE.txt` is BSD-2-Clause, SHA-256
  `662d27736e72b63c988153754fbf8b9e04b3668d8a5fd60f6a7a6f5eb503cd3d`;
- setup metadata simultaneously declares both `License :: OSI Approved :: BSD
  License` and `License :: Free for non-commercial use`;
- package metadata targets Python 2.7;
- runtime imports TensorFlow 1 `tensorflow.contrib.rnn`, removed from modern
  TensorFlow;
- bundled kick/snare/hihat TensorFlow checkpoint files have no separate model
  card, training-data provenance or checkpoint-specific license in the
  repository.

The bundled files are identifiable and hashable, but code licensing metadata is
internally contradictory and the checkpoint rights/provenance are not explicit.
Modern execution would also require a legacy TensorFlow/Python compatibility
port rather than a pinned install of the published package.

**Decision: no-go as a permissive product candidate.** Do not infer checkpoint
rights from the BSD source file, and do not port it before the author clarifies
the non-commercial classifier and model rights.

## ADTOF boundary

ADTOF remains a research comparator only under CC BY-NC-SA 4.0. It was not used
as a fallback product candidate and no production runtime was started.

## Phase 3 interim conclusion

- Basic Pitch bass: reproducible infrastructure, **musical-quality no-go** at the
  tested defaults (exact-pitch F1 0.205882 at 50 ms).
- Omnizart drums: plausible MIT source/checkpoint distribution, but **runtime
  no-go for the current minimal path** because a reproducible hash-locked install
  was not achieved.
- ADTLib drums: **license/provenance and runtime no-go**.
- Phase 4 remains blocked. No drum pipeline, image digest or real-input metrics
  are claimed.

The next product step is a new Task 5 candidate search with a modern permissive
package and explicitly licensed pretrained weights, or an explicitly approved
Omnizart drum-only repack spike. The candidate must pass pinned install, model
hash verification, inference, parsed MIDI and per-class real-input evaluation
before it counts.

## Subsequent runnable candidate: Inverse Drum Machine

After the initial no-go review, Inverse Drum Machine (IDM) was selected as a
modern candidate whose Apache-2.0 repository includes the pretrained
checkpoint. The runtime is pinned to upstream commit
`456656868538205ef756912c7cf5b0fd936de8af`; the checkpoint SHA-256 is
`5856a9bee7c6d503842795756d238dc8470f6f3e010e9e4f33ede0362850cb4c`.

AILab PipelineRun `phase3-idm-drum-gsb98` built exact gpmidi commit
`9bfed03d1073743a1fb602b1ebce6164d4ffb1e3` and completed clone, build and
inference successfully. The immutable image digest is
`sha256:99c007476bb946fc728dc804b31b1e3a1520eda57bc7475b820085f194acaf72`.
On the upstream demo audio it emitted 78 source-second events. Independent
PipelineRun `phase3-idm-readback-ln7cj` verified the event evidence SHA-256
`e0817ebedc40774a15cc9ab5d4448bc8c3ea4ca5e66f6c5a9949e53e8a5e85fb`
and checked event ordering, instrument classes and MIDI velocity bounds.

This changes the drum status from "no runnable candidate" to "one technically
runnable candidate". It does not change the product decision: the upstream
demo has no matching reference MIDI, so transcription precision and recall are
unknown. The next gate is inference on the fixed Spring Melody drum WAV and
evaluation against the corresponding original Suno drum MIDI in source seconds.
