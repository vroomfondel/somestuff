#!/usr/bin/env python3
"""Find Keel-tracked workloads whose running image is out of date.

On every poll Keel compares ONLY the registry digest of right now against the
digest it memorised during its previous poll (``trigger/poll/
single_tag_watcher.go``). That memo lives in memory alone and is seeded from the
registry at startup (``trigger/poll/watcher.go``, ``addJob``).

Which means: what actually runs in the cluster never enters Keel's decision. If
a tag is moved while Keel restarts, Keel sets its baseline to the new digest
without ever touching the corresponding deployment — the change is invisible for
good, until the next push.

This tool makes exactly the comparison Keel does not: running pod digest against
the digest the tag currently points at.

Usage::

    python3 -m k3shelperstuff.keel_drift                          # all tracked workloads
    python3 -m k3shelperstuff.keel_drift --namespace somestuff    # a single namespace
    python3 -m k3shelperstuff.keel_drift --drift-only --quiet     # only deviations, terse
    python3 -m k3shelperstuff.keel_drift --fix-command            # emit rollout-restart commands

Every option can also be given as a ``KEEL_*`` environment variable (the CLI
option wins), so the same container image can be pointed at another cluster or
namespace without changing the command line.

Exit codes:

* ``0`` — nothing stale (or no tracked workloads at all).
* ``1`` — at least one container is stale, so the tool doubles as a pipeline gate.
* ``2`` — no usable kubeconfig / in-cluster context.

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/k3shelperstuff/keel_drift.py
"""

import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import requests
import typer
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from k3shelperstuff import configure_logging, print_banner

logger = logging.getLogger(__name__)

DOCKER_HUB_REGISTRY = "index.docker.io"
# The very same registry shows up in manifests under several names. Without
# normalising, a request ends up at https://docker.io/v2/... — but there is no
# registry API there, and the result would be a silent false hit.
DOCKER_HUB_ALIASES = frozenset({"docker.io", "registry-1.docker.io", "index.docker.io"})
# The host that actually serves the registry API.
DOCKER_HUB_API_HOST = "registry-1.docker.io"
KEEL_POLICY_KEY = "keel.sh/policy"
# Keel reads the policy from the annotations first, then from the labels
# (internal/policy/policy.go, GetPolicyFromLabelsOrAnnotations) and treats
# "never" like a missing entry, i.e. as NilPolicy. Mirror both here, otherwise
# the selection silently diverges from the one Keel itself makes.
KEEL_INACTIVE_POLICIES = frozenset({"", "never"})
# initContainer tracking is opt-in in Keel and defaults to false. We check them
# regardless — a stale initContainer is a fact, and that Keel does not touch it
# is precisely the interesting information.
KEEL_INIT_CONTAINERS_KEY = "keel.sh/initContainers"
REQUEST_TIMEOUT_SECONDS = 20

# The three resource kinds Keel can update by polling. They share metadata,
# spec.selector and spec.template — the comparison needs no more than that, and a
# union instead of object keeps the type checking sharp.
type Workload = client.V1Deployment | client.V1StatefulSet | client.V1DaemonSet

# Without these Accept headers the registry serves the old v1 manifest type and
# therefore a different digest than the one containerd carries in the imageID.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# In a pipe rich would otherwise fall back to 80 columns and mangle both the
# table and the progress lines — for a tool one likes to push into less or into a
# log, a fixed, generous width is more usable.
_WIDE = 200

console = Console(width=None if sys.stdout.isatty() else _WIDE)
err_console = Console(stderr=True, width=None if sys.stderr.isatty() else _WIDE)


class DriftStatus(StrEnum):
    """Outcome of the comparison for a single container."""

    CURRENT = "current"
    STALE = "STALE"
    PINNED = "pinned"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ImageRef:
    """A parsed image reference.

    Attributes:
        registry: Registry host, e.g. ``index.docker.io`` or ``ghcr.io``.
        repository: Repository path including the namespace, e.g. ``library/redis``.
        tag: Tag name, e.g. ``latest``.
        digest: Hard-referenced digest, if the image is pinned via ``@sha256:``.
            In that case Keel has no effect on it anyway.
    """

    registry: str
    repository: str
    tag: str
    digest: str | None = None

    @property
    def display(self) -> str:
        """Return the reference in the form it appears in the manifest."""
        if self.digest:
            return f"{self.repository}@{self.digest[:19]}"
        return f"{self.repository}:{self.tag}"


@dataclass
class Finding:
    """Comparison result for one container of a workload.

    Attributes:
        namespace: Namespace of the workload.
        kind: Resource kind, e.g. ``Deployment``.
        name: Name of the workload.
        container: Name of the container inside the pod template.
        image: Parsed image reference from the pod template.
        running: Digest the running pod actually uses.
        registry: Digest the tag currently points at in the registry.
        status: Outcome of the comparison.
        note: Additional explanation, mostly for ``UNKNOWN``.
        pull_policy: ``imagePullPolicy`` of the container. Anything but
            ``Always`` renders Keel's force policy on an unchanged tag
            ineffective — the kubelet then takes the locally present layer.
        is_init: Whether this container is an initContainer.
        keel_tracks: Whether Keel would act on this container at all.
    """

    namespace: str
    kind: str
    name: str
    container: str
    image: ImageRef
    running: str | None
    registry: str | None
    status: DriftStatus
    note: str = ""
    pull_policy: str = "Always"
    is_init: bool = False
    keel_tracks: bool = True

    @property
    def label(self) -> str:
        """Container name, with initContainers made recognisable."""
        return f"init:{self.container}" if self.is_init else self.container

    @property
    def restart_helps(self) -> bool:
        """Tell whether a ``rollout restart`` can renew the image at all.

        Returns:
            ``True`` only for ``imagePullPolicy: Always``. Otherwise the kubelet
            does not re-pull the unchanged tag and a restart just spins in
            circles.
        """
        return self.pull_policy == "Always"


@dataclass
class RegistryAuth:
    """Credentials per registry.

    Attributes:
        by_registry: Mapping of registry key to ``(user, password)``.
        fallback: Optional second source, consulted when this one yields
            nothing. That lets the local Docker login step in where the cluster
            brings no ``imagePullSecret``.
    """

    by_registry: dict[str, tuple[str, str]] = field(default_factory=dict)
    fallback: "RegistryAuth | None" = None

    def for_registry(self, registry: str) -> tuple[str, str] | None:
        """Look up credentials for a registry.

        Args:
            registry: Registry host, e.g. ``index.docker.io``.

        Returns:
            A ``(username, password)`` pair, or ``None`` when neither this
            instance nor its ``fallback`` matches.
        """
        if registry in self.by_registry:
            return self.by_registry[registry]
        # In dockerconfigjson, Docker Hub is traditionally keyed by the fully
        # qualified v1 URL rather than a bare host.
        if registry == DOCKER_HUB_REGISTRY:
            for key in ("https://index.docker.io/v1/", "docker.io", "https://docker.io"):
                if key in self.by_registry:
                    return self.by_registry[key]
        # Other registries may be noted with a scheme as well.
        for prefix in ("https://", "http://"):
            if f"{prefix}{registry}" in self.by_registry:
                return self.by_registry[f"{prefix}{registry}"]
        if self.fallback is not None:
            return self.fallback.for_registry(registry)
        return None


def parse_image(image: str) -> ImageRef:
    """Split an image reference into registry, repository and tag.

    Args:
        image: Reference such as ``ghcr.io/foo/bar:1.2`` or ``redis:8.6``.

    Returns:
        The parsed reference. A missing tag defaults to ``latest``, a missing
        registry to Docker Hub.
    """
    remainder = image
    digest: str | None = None
    if "@" in remainder:
        remainder, digest = remainder.split("@", 1)

    head, _, rest = remainder.partition("/")
    # A registry host has a dot, a port, or is called localhost.
    if rest and (("." in head) or (":" in head) or head == "localhost"):
        registry, path = head, rest
    else:
        registry, path = DOCKER_HUB_REGISTRY, remainder

    if registry in DOCKER_HUB_ALIASES:
        registry = DOCKER_HUB_REGISTRY

    tag = "latest"
    if ":" in path.rsplit("/", 1)[-1]:
        path, _, tag = path.rpartition(":")

    # Official Docker Hub images live under library/.
    if registry == DOCKER_HUB_REGISTRY and "/" not in path:
        path = f"library/{path}"

    return ImageRef(registry=registry, repository=path, tag=tag, digest=digest)


def _authenticated_get(url: str, credentials: tuple[str, str] | None, *, method: str = "GET") -> requests.Response:
    """Perform a registry request including the bearer-token handshake.

    Registries answer the first attempt with a 401 and a ``WWW-Authenticate``
    header naming the token endpoint and the scope. Only with the token fetched
    from there does the actual request succeed.

    Args:
        url: Full registry URL.
        credentials: Optional ``(username, password)`` pair for private repos.
        method: HTTP method, ``GET`` or ``HEAD``.

    Returns:
        The response of the second, authorised request — or that of the first,
        if it already succeeded.
    """
    headers = {"Accept": MANIFEST_ACCEPT}
    response = requests.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code != 401:
        return response

    challenge = response.headers.get("WWW-Authenticate", "")
    realm_match = re.search(r'realm="([^"]+)"', challenge)
    if not realm_match:
        return response

    params: dict[str, str] = {}
    for key in ("service", "scope"):
        match = re.search(rf'{key}="([^"]+)"', challenge)
        if match:
            params[key] = match.group(1)

    token_response = requests.get(
        realm_match.group(1), params=params, auth=credentials, timeout=REQUEST_TIMEOUT_SECONDS
    )
    if not token_response.ok:
        return response

    token = token_response.json().get("token") or token_response.json().get("access_token")
    if not token:
        return response

    headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)


def registry_digests(image: ImageRef, credentials: tuple[str, str] | None) -> tuple[str | None, set[str], str]:
    """Determine the digests a tag currently points at.

    A multi-arch tag points at an index which in turn references one manifest
    per platform. Which of the two digests the container runtime carries in the
    ``imageID`` is not uniform — hence this function returns both levels, and the
    comparison counts as satisfied as soon as the running digest matches either.

    Args:
        image: The image reference to check.
        credentials: Optional ``(username, password)`` pair.

    Returns:
        A tuple of the index digest (the tag's canonical value), the set of all
        acceptable digests and an error message. On success the error message is
        empty, on failure the index digest is ``None``.
    """
    host = DOCKER_HUB_API_HOST if image.registry == DOCKER_HUB_REGISTRY else image.registry
    base = f"https://{host}/v2/{image.repository}/manifests"
    try:
        response = _authenticated_get(f"{base}/{image.tag}", credentials)
    except requests.exceptions.RequestException as exc:
        # A hanging or unreachable registry must not tear down the whole run —
        # the affected image becomes "unknown", the rest keeps going.
        return None, set(), f"unreachable: {type(exc).__name__}"
    if not response.ok:
        detail = response.text.strip()[:120].replace("\n", " ")
        return None, set(), f"HTTP {response.status_code} {detail}"

    index_digest = response.headers.get("Docker-Content-Digest")
    acceptable: set[str] = set()
    if index_digest:
        acceptable.add(index_digest)

    try:
        manifest = response.json()
    except ValueError:
        return index_digest, acceptable, ""

    for child in manifest.get("manifests", []):
        child_digest = child.get("digest")
        if child_digest:
            acceptable.add(child_digest)

    return index_digest, acceptable, ""


def child_digests(image: ImageRef, digest: str, credentials: tuple[str, str] | None) -> set[str]:
    """Resolve a concrete digest and return its platform manifests.

    Needed when the running digest does not match the current tag. Registries
    occasionally re-push a tag's index — because attestation entries change, for
    instance — without the platform manifests underneath moving at all. The index
    digest is then new while the actually running bits are identical. Without
    this resolution the tool would wrongly report such a case as stale.

    Args:
        image: The image reference (for registry and repository).
        digest: The digest to resolve, typically taken from the ``imageID``.
        credentials: Optional ``(username, password)`` pair.

    Returns:
        The set of referenced platform manifests. Empty when the digest is not
        an index or cannot be fetched.
    """
    host = DOCKER_HUB_API_HOST if image.registry == DOCKER_HUB_REGISTRY else image.registry
    try:
        response = _authenticated_get(f"https://{host}/v2/{image.repository}/manifests/{digest}", credentials)
    except requests.exceptions.RequestException:
        return set()
    if not response.ok:
        return set()

    try:
        manifest = response.json()
    except ValueError:
        return set()

    return {child["digest"] for child in manifest.get("manifests", []) if child.get("digest")}


def load_pull_credentials(core: client.CoreV1Api, namespace: str, secret_names: list[str]) -> RegistryAuth:
    """Read ``imagePullSecrets`` and build a registry mapping from them.

    Args:
        core: Client for the Core API.
        namespace: Namespace the secrets live in.
        secret_names: Names of the referenced secrets.

    Returns:
        The credentials found. Unreadable or unsuitably formatted secrets are
        skipped silently — a missing login surfaces as a visible HTTP 401 later
        on anyway.
    """
    auth = RegistryAuth()
    for name in secret_names:
        try:
            secret = core.read_namespaced_secret(name=name, namespace=namespace)
        except ApiException:
            continue

        raw = (secret.data or {}).get(".dockerconfigjson")
        if not raw:
            continue

        try:
            parsed = json.loads(base64.b64decode(raw))
        except (ValueError, json.JSONDecodeError):
            continue

        auth.by_registry.update(parse_docker_auths(parsed))
    return auth


def parse_docker_auths(config_json: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Pull the registry logins out of a Docker config structure.

    Args:
        config_json: Parsed content of a ``config.json`` respectively of a
            ``.dockerconfigjson``.

    Returns:
        Mapping of registry key to ``(user, password)``. Entries without usable
        credentials are left out.
    """
    found: dict[str, tuple[str, str]] = {}
    for registry, entry in (config_json.get("auths") or {}).items():
        username = entry.get("username")
        password = entry.get("password")
        if not username and entry.get("auth"):
            try:
                decoded = base64.b64decode(entry["auth"]).decode()
                username, _, password = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                continue
        if username and password:
            found[registry] = (username, password)
    return found


def load_local_docker_config() -> tuple[RegistryAuth, str]:
    """Load the local Docker login as a fallback for public images.

    Without a login Docker Hub counts anonymously and per IP — 100 manifest
    requests per hour, which a single run across all tracked images can already
    blow through. With a login it counts per account and far more generously.

    Honours ``DOCKER_CONFIG`` like the docker CLI does and otherwise falls back
    to ``~/.docker/config.json``.

    Returns:
        The credentials found plus a short provenance description for the
        output.
    """
    base = os.environ.get("DOCKER_CONFIG")
    path = Path(base) / "config.json" if base else Path.home() / ".docker/config.json"

    if not path.is_file():
        return RegistryAuth(), f"{path} does not exist"

    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return RegistryAuth(), f"{path} not readable: {exc}"

    # With credsStore/credHelpers the password is not in the file but has to be
    # fetched via docker-credential-<helper>. We do not reimplement that here —
    # but silently doing the wrong thing would be worse than saying so.
    helpers = []
    if parsed.get("credsStore"):
        helpers.append(f"credsStore={parsed['credsStore']}")
    if parsed.get("credHelpers"):
        helpers.append(f"credHelpers={sorted(parsed['credHelpers'])}")

    auth = RegistryAuth(by_registry=parse_docker_auths(parsed))
    if not auth.by_registry and helpers:
        return auth, f"{path}: only {', '.join(helpers)}, no plaintext logins"
    return auth, f"{path} ({len(auth.by_registry)} registry logins)"


def running_digests(core: client.CoreV1Api, namespace: str, selector: str) -> dict[str, str]:
    """Collect the image digests that are actually running, per container.

    Args:
        core: Client for the Core API.
        namespace: Namespace of the pods.
        selector: Label selector of the workload.

    Returns:
        A mapping of container name to the digest part of the ``imageID``. Only
        running pods are taken into account — a pod in CrashLoop says nothing
        about what is being operated regularly.
    """
    digests: dict[str, str] = {}
    pods = core.list_namespaced_pod(namespace=namespace, label_selector=selector)
    for pod in pods.items:
        if pod.status.phase != "Running":
            continue
        # initContainers have long finished by the time we query, but their
        # status still holds on to the digest that was used.
        statuses = list(pod.status.container_statuses or [])
        statuses += list(pod.status.init_container_statuses or [])
        for status in statuses:
            image_id = status.image_id or ""
            if "@" in image_id:
                digests[status.name] = image_id.split("@", 1)[1]
    return digests


def collect_workloads(apps: client.AppsV1Api, namespace: str | None) -> list[tuple[str, Workload]]:
    """Collect all workloads carrying a Keel policy.

    Args:
        apps: Client for the Apps API.
        namespace: Namespace to restrict to, or ``None`` for all.

    Returns:
        Pairs of resource kind and object.
    """
    if namespace:
        sources = (
            ("Deployment", apps.list_namespaced_deployment(namespace).items),
            ("StatefulSet", apps.list_namespaced_stateful_set(namespace).items),
            ("DaemonSet", apps.list_namespaced_daemon_set(namespace).items),
        )
    else:
        sources = (
            ("Deployment", apps.list_deployment_for_all_namespaces().items),
            ("StatefulSet", apps.list_stateful_set_for_all_namespaces().items),
            ("DaemonSet", apps.list_daemon_set_for_all_namespaces().items),
        )

    found: list[tuple[str, Workload]] = []
    for kind, items in sources:
        for item in items:
            if keel_policy(item.metadata) is not None:
                found.append((kind, item))
    return found


def keel_tracks_init_containers(meta: client.V1ObjectMeta) -> bool:
    """Tell whether Keel also tracks this workload's initContainers.

    Mirrors ``getInitContainerTrackingFromMeta``: opt-in, default ``false``, key
    matched case-insensitively, value must be exactly ``"true"``. Unlike for the
    policy, the **labels are checked first** here, then the annotations.

    Args:
        meta: Metadata of the workload.

    Returns:
        ``True`` only for an explicit ``keel.sh/initContainers: "true"``.
    """
    needle = KEEL_INIT_CONTAINERS_KEY.lower()
    for source in (meta.labels, meta.annotations):
        for key, value in (source or {}).items():
            if key.lower() == needle:
                return bool(value == "true")
    return False


def keel_policy(meta: client.V1ObjectMeta) -> str | None:
    """Determine the effective Keel policy of a workload.

    Mirrors the order from ``GetPolicyFromLabelsOrAnnotations``: annotations beat
    labels, and an entry is only found when it is set at all.

    Args:
        meta: Metadata of the workload.

    Returns:
        The policy name, or ``None`` when Keel does not touch the workload — that
        is, when neither annotation nor label is set or the policy reads
        ``never`` respectively empty.
    """
    for source in (meta.annotations, meta.labels):
        policy = (source or {}).get(KEEL_POLICY_KEY)
        if policy is not None:
            return None if policy in KEEL_INACTIVE_POLICIES else str(policy)
    return None


def analyse(namespace: str | None, verbose: bool, use_local_credentials: bool = True) -> list[Finding]:
    """Compare all tracked workloads against their registries.

    Args:
        namespace: Namespace to restrict to, or ``None`` for all.
        verbose: When set, log every single step.
        use_local_credentials: When set, use the local Docker login as a
            fallback wherever the cluster provides no ``imagePullSecret``.

    Returns:
        One result per container, sorted by namespace and name.
    """
    apps = client.AppsV1Api()
    core = client.CoreV1Api()

    local_auth: RegistryAuth | None = None
    if use_local_credentials:
        candidate, origin = load_local_docker_config()
        if candidate.by_registry:
            local_auth = candidate
            logger.info(f"local Docker login used as fallback: {origin}")
        else:
            logger.warning(
                f"no local Docker login usable: {origin} — public images are queried anonymously (100/h per IP)"
            )

    findings: list[Finding] = []
    digest_cache: dict[tuple[str, str, str], tuple[str | None, set[str], str]] = {}

    with err_console.status("[cyan]looking for workloads with keel.sh/policy…[/]"):
        workloads = collect_workloads(apps, namespace)

    scope = f"namespace {namespace}" if namespace else "all namespaces"
    logger.info(f"{len(workloads)} tracked workloads found in {scope}")
    if not workloads:
        return findings

    seen_namespaces: set[str] = set()

    # The progress display deliberately goes to stderr so stdout stays clean for
    # a pipe. transient: the bar disappears again once the run is over. Anything
    # logged WHILE it is live must go through progress.console, otherwise the
    # loguru sink (stderr as well) would tear the live display apart.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=err_console,
        transient=not verbose,
    ) as progress:
        task = progress.add_task("checking…", total=len(workloads))

        for kind, workload in workloads:
            meta = workload.metadata
            spec = workload.spec.template.spec

            if meta.namespace not in seen_namespaces:
                seen_namespaces.add(meta.namespace)
                if verbose:
                    progress.console.print(f"[bold cyan]:: namespace {meta.namespace}[/]")

            progress.update(task, description=f"{meta.namespace}/[bold]{meta.name}[/]")
            if verbose:
                progress.console.print(f"   {kind} {meta.name} ({len(spec.containers)} containers)")

            selector_pairs = (workload.spec.selector.match_labels or {}).items()
            selector = ",".join(f"{key}={value}" for key, value in selector_pairs)
            actual = running_digests(core, meta.namespace, selector)

            secret_names = [ref.name for ref in (spec.image_pull_secrets or [])]
            auth = load_pull_credentials(core, meta.namespace, secret_names)
            auth.fallback = local_auth
            if verbose and secret_names:
                progress.console.print(f"      imagePullSecrets: {', '.join(secret_names)}")

            _examine_containers(
                kind=kind,
                meta=meta,
                spec=spec,
                actual=actual,
                auth=auth,
                digest_cache=digest_cache,
                findings=findings,
                progress=progress,
                task=task,
                verbose=verbose,
            )
            progress.advance(task)

    findings.sort(key=lambda item: (item.namespace, item.name, item.container))
    return findings


def _examine_containers(
    *,
    kind: str,
    meta: client.V1ObjectMeta,
    spec: client.V1PodSpec,
    actual: dict[str, str],
    auth: RegistryAuth,
    digest_cache: dict[tuple[str, str, str], tuple[str | None, set[str], str]],
    findings: list[Finding],
    progress: Progress,
    task: TaskID,
    verbose: bool,
) -> None:
    """Check the containers of one workload and append the results.

    Args:
        kind: Resource kind of the workload.
        meta: Metadata of the workload.
        spec: Pod spec of the workload.
        actual: Running digests per container name.
        auth: Credentials for private registries.
        digest_cache: Shared cache for registry lookups.
        findings: List the results are appended to.
        progress: Progress display whose console is used for logging (the live
            display owns stderr while it runs).
        task: ID of the progress task whose description is updated.
        verbose: When set, log every single step.
    """
    tracks_init = keel_tracks_init_containers(meta)
    todo = [(c, False) for c in spec.containers]
    todo += [(c, True) for c in (spec.init_containers or [])]

    for container, is_init in todo:
        image = parse_image(container.image)
        running = actual.get(container.name)

        if image.digest:
            if verbose:
                progress.console.print(f"      [dim]{container.name}: {image.display} pinned by digest, skipped[/]")
            findings.append(
                Finding(
                    namespace=meta.namespace,
                    kind=kind,
                    name=meta.name,
                    container=container.name,
                    image=image,
                    running=running,
                    registry=image.digest,
                    status=DriftStatus.PINNED,
                    note="pinned by digest, Keel has no effect",
                    pull_policy=container.image_pull_policy or "Always",
                    is_init=is_init,
                    keel_tracks=tracks_init if is_init else True,
                )
            )
            continue

        key = (image.registry, image.repository, image.tag)
        cached = key in digest_cache
        if verbose:
            source = "cache" if cached else f"asking {image.registry}"
            progress.console.print(f"      {container.name}: {image.display} [dim]({source})[/]")
        if not cached:
            progress.update(task, description=f"{meta.namespace}/[bold]{meta.name}[/] [dim]→ {image.registry}[/]")
            digest_cache[key] = registry_digests(image, auth.for_registry(image.registry))
        index_digest, acceptable, error = digest_cache[key]

        if error:
            status, note = DriftStatus.UNKNOWN, f"registry: {error}"
        elif not acceptable:
            # Without a value to compare against, "stale" is a claim rather than
            # a finding — better to report it as unknown.
            status, note = DriftStatus.UNKNOWN, "no digest from the registry"
        elif running is None:
            status, note = DriftStatus.UNKNOWN, "no running pod"
        elif running in acceptable:
            status, note = DriftStatus.CURRENT, ""
        elif child_digests(image, running, auth.for_registry(image.registry)) & acceptable:
            # The running digest is an older index pointing at the same platform
            # manifests as the current tag. Content-wise current, then — only the
            # index shell was pushed anew.
            status = DriftStatus.CURRENT
            note = "different index, same platform manifests"
        else:
            status, note = DriftStatus.STALE, "tag points elsewhere"

        keel_tracks = tracks_init if is_init else True
        if status is DriftStatus.STALE and not keel_tracks:
            # Keel only touches initContainers with keel.sh/initContainers:
            # "true". Without that annotation such a container stays stale for
            # arbitrarily long without anything showing up anywhere.
            note = f"{note}; initContainer without {KEEL_INIT_CONTAINERS_KEY}, Keel ignores it"

        pull_policy = container.image_pull_policy or "Always"
        if status is DriftStatus.STALE and pull_policy != "Always":
            # No restart helps here: on an unchanged tag the kubelet takes the
            # locally present layer. Keel's force policy runs into the void as
            # well and still reports success.
            note = f"{note}; imagePullPolicy={pull_policy} prevents a re-pull"

        if verbose:
            colour = {
                DriftStatus.CURRENT: "green",
                DriftStatus.STALE: "bold red",
                DriftStatus.PINNED: "dim",
                DriftStatus.UNKNOWN: "yellow",
            }[status]
            detail = f" {note}" if note else ""
            progress.console.print(
                f"         running {_short(running)} vs. registry {_short(index_digest)} "
                f"→ [{colour}]{status}[/]{detail}"
            )

        findings.append(
            Finding(
                namespace=meta.namespace,
                kind=kind,
                name=meta.name,
                container=container.name,
                image=image,
                running=running,
                registry=index_digest,
                status=status,
                note=note,
                pull_policy=pull_policy,
                is_init=is_init,
                keel_tracks=keel_tracks,
            )
        )


def _short(digest: str | None) -> str:
    """Shorten a digest to a length readable in a terminal.

    Args:
        digest: The digest to shorten, or ``None``.

    Returns:
        The first 12 characters of the hash, or ``"-"`` for a missing digest.
    """
    if not digest:
        return "-"
    return digest.removeprefix("sha256:")[:12]


def render(findings: list[Finding], drift_only: bool) -> None:
    """Print the results as a table.

    Args:
        findings: The comparison results.
        drift_only: When set, only show stale and unknown rows.
    """
    style = {
        DriftStatus.CURRENT: "green",
        DriftStatus.STALE: "bold red",
        DriftStatus.PINNED: "dim",
        DriftStatus.UNKNOWN: "yellow",
    }

    table = Table(title="Keel drift: running image vs. registry tag")
    table.add_column("Namespace")
    table.add_column("Workload")
    table.add_column("Container")
    table.add_column("Image")
    table.add_column("running", justify="right")
    table.add_column("registry", justify="right")
    table.add_column("Status")
    table.add_column("Note", overflow="fold")

    for finding in findings:
        if drift_only and finding.status in (DriftStatus.CURRENT, DriftStatus.PINNED):
            continue
        table.add_row(
            finding.namespace,
            finding.name,
            f"[dim]{finding.label}[/]" if finding.is_init else finding.label,
            finding.image.display,
            _short(finding.running),
            _short(finding.registry),
            f"[{style[finding.status]}]{finding.status}[/]",
            finding.note,
        )

    console.print(table)


def main(
    namespace: str | None = typer.Option(
        None, "--namespace", "-n", envvar="KEEL_NAMESPACE", help="only check this namespace"
    ),
    drift_only: bool = typer.Option(
        False, "--drift-only", envvar="KEEL_DRIFT_ONLY", help="only show stale and unknown workloads"
    ),
    fix_command: bool = typer.Option(
        False, "--fix-command", envvar="KEEL_FIX_COMMAND", help="emit kubectl rollout restart commands"
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", envvar="KEEL_QUIET", help="suppress the table, print the summary only"
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        envvar="KEEL_VERBOSE",
        help="log every namespace, workload and registry access individually (DEBUG logging)",
    ),
    no_local_credentials: bool = typer.Option(
        False,
        "--no-local-credentials",
        envvar="KEEL_NO_LOCAL_CREDENTIALS",
        help="ignore the local Docker login and query everything anonymously",
    ),
    context: str | None = typer.Option(
        None,
        "--context",
        envvar="KEEL_CONTEXT",
        help="kubeconfig context to use instead of the currently active one (e.g. ht@dgxarley)",
    ),
) -> None:
    """Check whether Keel-tracked workloads lag behind their tag.

    Keel memorises digests in memory only and seeds them from the registry at
    startup. A push during a Keel restart is therefore never recognised as a
    change. This tool finds the cases that are left behind — worth running
    before and after every Keel rollout.

    \f
    Everything below the ``\\f`` is hidden from ``--help`` (click convention).

    The exit code is 1 as soon as at least one workload is stale, so the tool
    can serve as a gate in a pipeline.

    Raises:
        typer.Exit: Always — with the module docstring's exit codes.
    """
    configure_logging(verbose=verbose)
    print_banner("keel_drift")

    try:
        config.load_kube_config(context=context)
    except config.ConfigException as first:
        if context:
            # An explicitly named context that does not exist is a typo — silently
            # falling back to the in-cluster context would then check the wrong
            # cluster.
            logger.error(f"context '{context}' not usable: {first}")
            raise typer.Exit(code=2) from first
        try:
            config.load_incluster_config()
        except config.ConfigException as exc:
            logger.error(f"neither a kubeconfig nor an in-cluster context: {exc}")
            raise typer.Exit(code=2) from exc

    logger.info(f"cluster: {context or 'active context'}")

    findings = analyse(namespace, verbose, not no_local_credentials)
    if not findings:
        logger.warning("no workloads with keel.sh/policy found")
        raise typer.Exit(code=0)

    if not quiet:
        render(findings, drift_only)

    stale = [item for item in findings if item.status == DriftStatus.STALE]
    unknown = [item for item in findings if item.status == DriftStatus.UNKNOWN]

    console.print(
        f"{len(findings)} containers checked, [bold red]{len(stale)} stale[/], [yellow]{len(unknown)} unknown[/]"
    )

    if fix_command and stale:
        restartable = [item for item in stale if item.restart_helps]
        blocked = [item for item in stale if not item.restart_helps]

        if restartable:
            console.print("\n[bold]To straighten out:[/]")
            seen: set[tuple[str, str, str]] = set()
            for item in restartable:
                key = (item.namespace, item.kind, item.name)
                if key in seen:
                    continue
                seen.add(key)
                console.print(f"  kubectl rollout restart -n {item.namespace} {item.kind.lower()}/{item.name}")

        if blocked:
            console.print(
                "\n[bold yellow]A restart does not help here[/] — with imagePullPolicy != Always the kubelet "
                "does not re-pull the unchanged tag. Fix the policy first:"
            )
            for item in blocked:
                console.print(
                    f"  kubectl set image ... [dim]# {item.namespace}/{item.name} container {item.label}: "
                    f"imagePullPolicy={item.pull_policy} → Always[/]"
                )

    raise typer.Exit(code=1 if stale else 0)


if __name__ == "__main__":
    typer.run(main)
