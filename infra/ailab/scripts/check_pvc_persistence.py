#!/usr/bin/env python3
"""Prove the data-bearing PVCs were not replaced by a deploy.

Compares the `before` and `after` snapshots taken by the pvc-persistence-check
Tekton task. A deploy or rollback that silently recreated a claim would give the
same name but a new UID and a new PersistentVolume, which is exactly the failure
this catches.

Usage: check_pvc_persistence.py pvc-before.json pvc-after.json
"""

from __future__ import annotations

import json
import sys

COMPARED_FIELDS = ("uid", "volume", "capacity")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    with open(argv[1], encoding="utf-8") as fh:
        before = json.load(fh)
    with open(argv[2], encoding="utf-8") as fh:
        after = json.load(fh)

    by_name = {p["name"]: p for p in before}
    failures: list[str] = []

    if not after:
        return _fail(["no PVCs were found in the 'after' snapshot"])

    for claim in after:
        name = claim["name"]
        prior = by_name.get(name)
        if prior is None:
            failures.append(f"{name} did not exist before the deploy")
            continue
        for field in COMPARED_FIELDS:
            if prior.get(field) != claim.get(field):
                failures.append(
                    f"{name}.{field} changed: {prior.get(field)} -> {claim.get(field)}"
                )
        if claim.get("phase") != "Bound":
            failures.append(f"{name} is {claim.get('phase')}, not Bound")
        print(
            f"  {name}: uid={claim['uid']} volume={claim['volume']} "
            f"capacity={claim['capacity']} phase={claim['phase']}"
        )

    missing = sorted(set(by_name) - {c["name"] for c in after})
    for name in missing:
        failures.append(f"{name} disappeared during the deploy")

    if failures:
        return _fail(failures)

    print(
        f"PVC persistence verified for {len(after)} claim(s); "
        "UID, PersistentVolume and capacity all unchanged"
    )
    return 0


def _fail(failures: list[str]) -> int:
    print("PVC PERSISTENCE FAILED:")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
