#!/usr/bin/env python3
"""Dependency and license review gate.

Resolves each installed distribution's license from its own metadata rather than
trusting a third-party summariser: `pip-licenses` reads the legacy `License:`
field, so under PEP 639 — which moved the declaration to `License-Expression` and
trove classifiers — a majority of modern packages come back as UNKNOWN and the
gate becomes noise.

For every environment given on the command line, each distribution is resolved in
this order:

1. `License-Expression` (PEP 639);
2. `Classifier: License :: ...` entries;
3. the legacy `License` field, when it is short enough to be a name rather than an
   embedded licence text.

A distribution whose license is not on the permissive allowlist, or which has no
license metadata at all, fails the gate. Known exceptions are declared explicitly
with a reason, so adding one is a reviewable change rather than a silent widening.

Usage: check_licenses.py /path/to/python [/path/to/another/python ...]
"""

from __future__ import annotations

import json
import subprocess
import sys

ALLOWED_SUBSTRINGS = (
    "mit",
    "bsd",
    "apache",
    "isc",
    "python software foundation",
    "psf",
    "zpl",
    "zope public",
    "unlicense",
    "public domain",
    "mpl-2.0",
    "mozilla public license 2.0",
    "historical permission notice",
    "hpnd",
    "cc0",
    "postgresql",
    "0bsd",
    "wtfpl",
)

# Package -> reason. Each entry is a deliberate, reviewed decision.
DECLARED_EXCEPTIONS = {
    # LGPL: a binding to libsndfile, dynamically linked and unmodified, used only
    # for bounded audio decode in the analyzer.
    "soundfile": "LGPL-2.1 (libsndfile binding), dynamically linked, unmodified",
    "SoundFile": "LGPL-2.1 (libsndfile binding), dynamically linked, unmodified",
    # LGPL: a ctypes binding to FluidSynth. Present only as a transitive dependency
    # of pretty_midi; the converter never synthesises audio.
    "pyfluidsynth": "LGPL-2.1 (FluidSynth ctypes binding), unused transitive dep",
    # The vendored gtrsnipe core is PolyForm Noncommercial and is not a Python
    # distribution; its licence and required notices live in the repository.
    "gtrsnipe": "PolyForm Noncommercial, vendored with its notices, non-commercial use",
    # LGPL-3.0: the Guitar Pro parser. Installed unmodified from PyPI and imported
    # dynamically, never modified or statically linked, which is the standard LGPL
    # position for a Python dependency. It is the project's core parser and cannot
    # be replaced.
    "PyGuitarPro": "LGPL-3.0-only, installed unmodified, dynamically imported",
    "pyguitarpro": "LGPL-3.0-only, installed unmodified, dynamically imported",
    # LGPL-3.0: PDF generation for printable tabs. Same position — unmodified,
    # dynamically imported.
    "fpdf2": "LGPL-3.0-only, installed unmodified, dynamically imported",
    # LGPL-2.1: resampling, pulled in transitively by the audio stack.
    "soxr": "LGPL-2.1-or-later, transitive, installed unmodified",
    # No PyPI license metadata. The upstream project is MIT-licensed; it is
    # installed with --no-deps and used only for ASCII tab layout.
    "tuttut": "MIT upstream, no PyPI metadata, installed --no-deps",
    # No PyPI license metadata. Guitar Pro 7/8 parsing helper used by gp_import.
    "ApolloTab": "no PyPI metadata; permissive upstream, unmodified import only",
    # setuptools 79 publishes neither License-Expression nor classifiers, only a
    # LICENSE file. It is MIT, and is present because tuttut imports pkg_resources.
    "setuptools": "MIT; setuptools 79 ships a LICENSE file with no metadata field",
}

UNKNOWN = {"", "unknown", "unknown license", "none", "null", "unspecified"}

# Beyond this length the legacy License field is the licence *text*, not a name.
MAX_LICENSE_NAME_LENGTH = 120

_PROBE = r"""
import json
from importlib import metadata

out = {}
for dist in metadata.distributions():
    try:
        meta = dist.metadata
        name = meta["Name"] or (dist._path.name if hasattr(dist, "_path") else "?")
    except Exception:
        continue
    if not name:
        continue
    expression = meta.get("License-Expression") or ""
    classifiers = [
        c.split("::", 1)[1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    legacy = (meta.get("License") or "").strip()
    out[name] = {
        "expression": expression.strip(),
        "classifiers": classifiers,
        "legacy": legacy,
        "version": meta.get("Version") or "",
    }
print(json.dumps(out))
"""


def probe(python: str) -> dict:
    proc = subprocess.run(
        [python, "-c", _PROBE], capture_output=True, text=True, check=True
    )
    return json.loads(proc.stdout)


def resolve(info: dict) -> tuple[str, str]:
    """Return (license_text, source) using the most authoritative field present."""
    if info["expression"]:
        return info["expression"], "License-Expression"
    if info["classifiers"]:
        return "; ".join(info["classifiers"]), "Classifier"
    legacy = info["legacy"]
    if legacy and len(legacy) <= MAX_LICENSE_NAME_LENGTH:
        return legacy, "License"
    if legacy:
        return "<embedded license text>", "License(text)"
    return "", "none"


def classify(name: str, license_text: str) -> str | None:
    """Return a failure reason, or None when acceptable."""
    if name in DECLARED_EXCEPTIONS:
        return None
    normalized = (license_text or "").strip().lower()
    if normalized in UNKNOWN:
        return "no license metadata in License-Expression, classifiers or License"
    if normalized == "<embedded license text>":
        return "only an embedded license text, no machine-readable name"
    if any(token in normalized for token in ALLOWED_SUBSTRINGS):
        return None
    return f"license {license_text!r} is not on the permissive allowlist"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    resolved: dict[str, tuple[str, str, str]] = {}
    for python in argv[1:]:
        print(f"probing {python}")
        for name, info in probe(python).items():
            text, source = resolve(info)
            # Prefer the most informative answer when both environments have it.
            if name not in resolved or (
                resolved[name][0] in UNKNOWN and text not in UNKNOWN
            ):
                resolved[name] = (text, source, info["version"])

    failures: list[str] = []
    for name in sorted(resolved, key=str.lower):
        text, source, version = resolved[name]
        reason = classify(name, text)
        status = "EXCEPTION" if name in DECLARED_EXCEPTIONS else "ok"
        if reason:
            failures.append(f"{name} {version}: {reason}")
            status = "FAIL"
        print(f"  {status:9} {name:30} {version:12} [{source}] {text[:70]}")

    print(f"\nreviewed {len(resolved)} distinct dependency license(s)")
    for name, reason in sorted(DECLARED_EXCEPTIONS.items()):
        if name in resolved:
            print(f"  declared exception: {name} — {reason}")

    if failures:
        print("\nLICENSE REVIEW FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("license review: all dependencies permissive or explicitly excepted")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
