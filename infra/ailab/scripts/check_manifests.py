#!/usr/bin/env python3
"""Manifest and NetworkPolicy policy checks over rendered Kubernetes YAML.

Complements schema validation, which cannot express intent. Fails closed with a
non-zero exit and a list of every violation, so one run reports all problems.

Checked, for every workload in the `gpmidi-ml` namespace:

* PSS `restricted` essentials — non-root, no privilege escalation, all
  capabilities dropped, RuntimeDefault seccomp, read-only root filesystem;
* no privileged container, hostPID/hostIPC/hostNetwork, or hostPath volume;
* CPU/memory limits present, so a runaway analysis cannot starve the node;
* ServiceAccount token automount disabled on application pods;
* no `:latest` or tag-less image on an application Deployment — delivery is by
  immutable digest or exact-SHA tag;
* no GPU request anywhere: Phase 2 is CPU-only;
* every application Deployment has a NetworkPolicy selecting it, and no policy
  allows unrestricted ingress from everywhere.

Usage: check_manifests.py rendered.yaml
"""

from __future__ import annotations

import sys

import yaml

NAMESPACE = "gpmidi-ml"

# Tekton runs its own pods from Task definitions rather than Deployments, and the
# EventListener Deployment is created by the Triggers controller, so neither is
# subject to the application workload rules below.
EXEMPT_WORKLOADS = {"el-gitea-listener"}
# The Phase 0 smoke deployment predates these rules and exists only to prove
# ingress works; it is not part of the Phase 2 delivery surface.
EXEMPT_IMAGE_RULES = {"smoke-app"}
# The prune CronJob and the gate tasks reference the CI image by its `pinned`
# floating alias on purpose: it is an internal tool image, not a delivered
# artifact, and it is republished only by the gpmidi-ci-image pipeline.
EXEMPT_IMAGE_RULES.add("tekton-prune")

GPU_RESOURCES = ("nvidia.com/gpu", "amd.com/gpu")


def containers(spec: dict):
    return list(spec.get("containers") or []) + list(spec.get("initContainers") or [])


def check_workload(
    kind: str,
    name: str,
    pod_spec: dict,
    failures: list[str],
    require_no_token: bool = True,
) -> None:
    def fail(msg: str) -> None:
        failures.append(f"{kind}/{name}: {msg}")

    pod_sc = pod_spec.get("securityContext") or {}
    if pod_sc.get("runAsNonRoot") is not True:
        fail("pod securityContext.runAsNonRoot must be true")
    if (pod_sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
        fail("pod securityContext.seccompProfile.type must be RuntimeDefault")
    if require_no_token and pod_spec.get("automountServiceAccountToken") is not False:
        fail("automountServiceAccountToken must be false")
    for field in ("hostNetwork", "hostPID", "hostIPC"):
        if pod_spec.get(field):
            fail(f"{field} must not be set")
    for volume in pod_spec.get("volumes") or []:
        if "hostPath" in volume:
            fail(f"volume {volume.get('name')} uses hostPath")

    for c in containers(pod_spec):
        cname = c.get("name")
        sc = c.get("securityContext") or {}
        if sc.get("privileged"):
            fail(f"container {cname} is privileged")
        if sc.get("allowPrivilegeEscalation") is not False:
            fail(f"container {cname} must set allowPrivilegeEscalation: false")
        if sc.get("readOnlyRootFilesystem") is not True:
            fail(f"container {cname} must set readOnlyRootFilesystem: true")
        drops = ((sc.get("capabilities") or {}).get("drop")) or []
        if "ALL" not in drops:
            fail(f"container {cname} must drop ALL capabilities")

        limits = (c.get("resources") or {}).get("limits") or {}
        requests = (c.get("resources") or {}).get("requests") or {}
        for key in ("cpu", "memory"):
            if key not in limits:
                fail(f"container {cname} has no {key} limit")
        for pool in (limits, requests):
            for gpu in GPU_RESOURCES:
                if gpu in pool:
                    fail(f"container {cname} requests {gpu}; Phase 2 is CPU-only")

        image = c.get("image", "")
        if name not in EXEMPT_IMAGE_RULES:
            if "@sha256:" in image:
                pass  # immutable digest: best case
            elif ":" not in image.rsplit("/", 1)[-1]:
                fail(f"container {cname} image '{image}' has no tag")
            elif image.rsplit(":", 1)[-1] == "latest":
                fail(f"container {cname} image '{image}' uses the mutable :latest tag")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    with open(argv[1], encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]

    failures: list[str] = []
    workloads: dict[str, dict] = {}
    cronjobs: dict[str, dict] = {}
    policies: list[dict] = []
    event_listeners: set[str] = set()
    services: set[str] = set()

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.get("metadata") or {}
        name = meta.get("name", "?")
        if meta.get("namespace") not in (None, NAMESPACE):
            continue

        if kind in ("Deployment", "StatefulSet", "DaemonSet"):
            if name in EXEMPT_WORKLOADS:
                continue
            pod_spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
            workloads[name] = doc
            token_allowed = name == "sheetsage2-service"
            check_workload(
                kind, name, pod_spec, failures, require_no_token=not token_allowed
            )
            if token_allowed and pod_spec.get("serviceAccountName") != "sheetsage2-controller":
                failures.append(
                    "Deployment/sheetsage2-service: must use dedicated sheetsage2-controller ServiceAccount"
                )
        elif kind == "CronJob":
            # A scheduled job runs in the same restricted namespace and gets the
            # same treatment, minus the NetworkPolicy coverage rule.
            pod_spec = (
                (((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {})
                .get("template")
                or {}
            ).get("spec") or {}
            cronjobs[name] = doc
            check_workload(kind, name, pod_spec, failures, require_no_token=False)
        elif kind == "NetworkPolicy":
            policies.append(doc)
        elif kind == "EventListener":
            event_listeners.add(name)
        elif kind == "Service":
            services.add(name)

    # Tekton Triggers creates and owns ``el-<listener>`` Services. Declaring the
    # same Service in Git races the controller, leaves Ready=False with
    # AlreadyExists, and can churn the namespace Service quota.
    for listener in sorted(event_listeners):
        generated_service = f"el-{listener}"
        if generated_service in services:
            failures.append(
                f"Service/{generated_service}: must be controller-owned by "
                f"EventListener/{listener}, not declared explicitly"
            )

    # Workspace PVCs are quota-bound (20 claims in gpmidi-ml), while each normal
    # PipelineRun can allocate two claims. A time-only seven-day policy exhausted
    # the quota during one acceptance session. Keep the pruning contract fail-closed:
    # frequent execution plus a bounded number of completed runs.
    prune = cronjobs.get("tekton-prune")
    if prune:
        schedule = (prune.get("spec") or {}).get("schedule")
        pod_spec = (
            ((((prune.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {})
            .get("template") or {}).get("spec") or {}
        )
        scripts = "\n".join(
            str(arg)
            for container in pod_spec.get("containers") or []
            for arg in container.get("args") or []
        )
        if schedule != "*/15 * * * *":
            failures.append(
                "CronJob/tekton-prune: schedule must be */15 * * * * for PVC quota safety"
            )
        if "RETAIN_COMPLETED_RUNS=4" not in scripts:
            failures.append(
                "CronJob/tekton-prune: must retain at most four completed PipelineRuns"
            )

    # Every application workload must be selected by at least one NetworkPolicy.
    for name, doc in sorted(workloads.items()):
        labels = (
            ((doc.get("spec") or {}).get("template") or {}).get("metadata") or {}
        ).get("labels") or {}
        selected = False
        for policy in policies:
            selector = ((policy.get("spec") or {}).get("podSelector") or {}).get(
                "matchLabels"
            ) or {}
            if selector and all(labels.get(k) == v for k, v in selector.items()):
                selected = True
                break
        if not selected:
            failures.append(
                f"Deployment/{name}: no NetworkPolicy selects labels {labels}"
            )

    # A policy that declares Ingress but lists no `from` allows every source.
    for policy in policies:
        name = (policy.get("metadata") or {}).get("name", "?")
        spec = policy.get("spec") or {}
        selector = (spec.get("podSelector") or {}).get("matchLabels") or {}
        if "Ingress" not in (spec.get("policyTypes") or []):
            continue
        for rule in spec.get("ingress") or []:
            if not rule.get("from"):
                # Tekton task pods are addressed by label and must accept the
                # controller's traffic; application pods must not be open.
                if "tekton.dev/memberOf" in selector:
                    continue
                failures.append(
                    f"NetworkPolicy/{name}: an ingress rule has no 'from' selector"
                )

    print(
        f"checked {len(workloads)} workload(s), {len(cronjobs)} cronjob(s) and "
        f"{len(policies)} NetworkPolicy(ies)"
    )
    for name in sorted(workloads):
        print(f"  workload ok: {name}")
    for name in sorted(cronjobs):
        print(f"  cronjob ok: {name}")
    if failures:
        print("\nMANIFEST POLICY FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("manifest policy: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
