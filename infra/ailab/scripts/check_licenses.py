#!/usr/bin/env python3
"""Dependency and license review gate.

Reads `pip-licenses --format=json` output for each CI environment and fails when
a dependency carries a license that is not on the permissive allowlist, or no
license at all. The point is to catch a copyleft or unknown-license dependency
entering the delivery surface before it ships, not to audit transitively vendored
source.

Known exceptions are declared explicitly with a reason, so adding one is a
reviewable change rather than a silent widening of the allowlist.

Usage: check_licenses.py licenses-root.json licenses-svc.json [...]
"""

from __future__ import annotations

import json
import sys

ALLOWED_SUBSTRINGS = (
    # Trove classifiers spell these as e.g.
    # "OSI Approved :: BSD License", so substring matching is deliberate.
    "mit",
    "bsd license",
    "apache software license",
    "python software foundation license",
    "the unlicense",
    "zope public license",
    "historical permission notice and disclaimer",
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
)

# Package -> reason. Each entry is a deliberate, reviewed decision.
DECLARED_EXCEPTIONS = {
    # LGPL: dynamically linked, not modified, and only used for audio decode.
    "soundfile": "LGPL-2.1 (libsndfile binding), dynamically linked, unmodified",
    "SoundFile": "LGPL-2.1 (libsndfile binding), dynamically linked, unmodified",
    # GPL-compatible font/tooling metadata that ships no code into our images.
    "chardet": "LGPL-2.1, transitive, not redistributed in service images",
    "text-unidecode": "Artistic-1.0/GPL dual license, transitive only",
}

# `pip-licenses` reports these when metadata is missing; treat as unknown.
UNKNOWN = {"", "unknown", "unknown license", "none", "null"}


def classify(name: str, license_text: str) -> str | None:
    """Return a failure reason, or None when acceptable."""
    normalized = (license_text or "").strip().lower()
    if name in DECLARED_EXCEPTIONS:
        return None
    if normalized in UNKNOWN:
        return f"no license metadata (reported {license_text!r})"
    if any(token in normalized for token in ALLOWED_SUBSTRINGS):
        return None
    return f"license {license_text!r} is not on the permissive allowlist"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    seen: dict[str, str] = {}
    for path in argv[1:]:
        with open(path, encoding="utf-8") as fh:
            for entry in json.load(fh):
                name = entry.get("Name", "?")
                seen[name] = entry.get("License", "")

    failures: list[str] = []
    for name in sorted(seen):
        reason = classify(name, seen[name])
        status = "EXCEPTION" if name in DECLARED_EXCEPTIONS else "ok"
        if reason:
            failures.append(f"{name}: {reason}")
            status = "FAIL"
        print(f"  {status:9} {name:32} {seen[name]}")

    print(f"\nreviewed {len(seen)} distinct dependency license(s)")
    for name, reason in sorted(DECLARED_EXCEPTIONS.items()):
        if name in seen:
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
