#!/usr/bin/env python3
"""Find Keel-tracked workloads whose running image has fallen behind its tag.

On every poll, Keel compares the registry digest of right now against the digest
it memorised during the previous poll (``trigger/poll/single_tag_watcher.go``).
That memo lives in memory only and is seeded from the registry at startup
(``trigger/poll/watcher.go``, ``addJob``).

It follows that what actually runs in the cluster never enters Keel's decision.
If a tag is moved while Keel restarts, Keel sets its baseline to the new digest
without ever touching the corresponding Deployment, so the change stays
invisible forever, until the next push.

This script performs exactly the comparison Keel does not: the digest of the
running pod against the digest the tag currently points at. A mismatch alone is
not proof of staleness, so index drift is separated from image drift -- see
:func:`child_digests`.

Run as ``python3 -m k3shelperstuff.keel_drift``.

Examples:
    python3 -m k3shelperstuff.keel_drift                          # every tracked workload
    python3 -m k3shelperstuff.keel_drift --context ht@dgxarley    # a specific kubeconfig context
    python3 -m k3shelperstuff.keel_drift --namespace somestuff    # a single namespace
    python3 -m k3shelperstuff.keel_drift --drift-only --quiet     # drift only, terse
    python3 -m k3shelperstuff.keel_drift --fix-command            # print rollout-restart commands

Every option can also be given as a ``KEEL_*`` environment variable (the CLI
option wins); ``--context`` additionally reads ``KUBE_CONTEXT``.

Exit codes:
    * ``0`` -- nothing stale (or no tracked workloads at all).
    * ``1`` -- at least one container is stale, so the tool doubles as a gate.
    * ``2`` -- no usable kubeconfig / in-cluster context.

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/k3shelperstuff/keel_drift.py
"""

import base64
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypedDict, cast

import requests
import typer
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
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

DOCKER_HUB_REGISTRY = "index.docker.io"
# The same registry shows up in manifests under several names. Without
# normalisation a request ends up at https://docker.io/v2/... -- which serves no
# registry API, so the result would be a silent false hit.
DOCKER_HUB_ALIASES = frozenset({"docker.io", "registry-1.docker.io", "index.docker.io"})
# The host that actually serves the registry API.
DOCKER_HUB_API_HOST = "registry-1.docker.io"
KEEL_POLICY_KEY = "keel.sh/policy"
# Keel reads the policy from the annotations first, then from the labels
# (internal/policy/policy.go, GetPolicyFromLabelsOrAnnotations) and treats
# "never" like a missing entry, as a NilPolicy. Mirror both here, otherwise the
# selection silently diverges from the one Keel itself makes.
KEEL_INACTIVE_POLICIES = frozenset({"", "never"})
# initContainer tracking is opt-in in Keel and defaults to false. We check them
# regardless -- a stale initContainer is a fact, and the very information worth
# having is that Keel will not touch it.
KEEL_INIT_CONTAINERS_KEY = "keel.sh/initContainers"
REQUEST_TIMEOUT_SECONDS = 20

# The three resource kinds Keel can update via poll. They share metadata,
# spec.selector and spec.template -- the comparison needs no more than that, and
# a union instead of object keeps type checking sharp.
type Workload = client.V1Deployment | client.V1StatefulSet | client.V1DaemonSet

# Identity of one registry lookup: registry host, repository path, tag.
type DigestCacheKey = tuple[str, str, str]

# Identity of one index resolution: registry host, repository path, digest.
type ChildCacheKey = tuple[str, str, str]

# Without these Accept headers the registry returns the old v1 manifest type,
# and with it a different digest than the one containerd carries in the imageID.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

# In a pipe rich would otherwise fall back to 80 columns and mangle both the
# table and the progress lines -- for a tool one likes to push into less or into
# a log file, a fixed generous width is more usable.
_WIDE = 200

console = Console(width=None if sys.stdout.isatty() else _WIDE)
err_console = Console(stderr=True, width=None if sys.stderr.isatty() else _WIDE)

# Typer would otherwise render the whole docstring of main(), Args and Raises
# sections included, into --help. The prose belongs there, the sections do not.
CLI_HELP = """Check whether Keel-tracked workloads lag behind their tag.

Keel memorises digests in memory only and seeds them from the registry at
startup. A push during a Keel restart is therefore never recognised as a change.
This script finds the cases left behind that way -- useful before and after
every Keel rollout.

The exit code is 1 as soon as at least one workload is stale, so the script
works as a gate in a pipeline.
"""

app = typer.Typer(add_completion=False)


class DockerAuthEntry(TypedDict, total=False):
    """One ``auths`` entry of a Docker config.

    Attributes:
        username: Plaintext user name, if present.
        password: Plaintext password, if present.
        auth: Base64 of ``user:password``, the usual alternative to the two
            fields above.
    """

    username: str
    password: str
    auth: str


class DockerConfig(TypedDict, total=False):
    """The subset of ``config.json`` / ``.dockerconfigjson`` used here.

    Attributes:
        auths: Registry key mapped to its credentials entry.
        credsStore: Name of a credential helper holding all credentials.
        credHelpers: Per-registry credential helpers.
    """

    auths: dict[str, DockerAuthEntry]
    credsStore: str
    credHelpers: dict[str, str]


class ManifestChild(TypedDict, total=False):
    """One per-platform manifest referenced by a multi-arch index.

    Attributes:
        digest: Digest of the platform-specific manifest.
    """

    digest: str


class ManifestIndex(TypedDict, total=False):
    """The manifest body returned for a tag.

    Attributes:
        manifests: Child manifests, present only for a multi-arch index.
    """

    manifests: list[ManifestChild]


class TokenResponse(TypedDict, total=False):
    """Body of a registry token endpoint response.

    Attributes:
        token: The bearer token, as named by the Docker token spec.
        access_token: The same value, as named by some OAuth2 implementations.
    """

    token: str
    access_token: str


class DriftStatus(StrEnum):
    """Outcome of the comparison for a single container."""

    CURRENT = "current"
    STALE = "STALE"
    PINNED = "pinned"
    UNKNOWN = "unclear"


@dataclass(frozen=True)
class ImageRef:
    """A parsed image reference.

    Attributes:
        registry: Registry host, e.g. ``index.docker.io`` or ``ghcr.io``.
        repository: Repository path including namespace, e.g. ``library/redis``.
        tag: Tag name, e.g. ``latest``.
        digest: Hard-referenced digest, if the image is pinned via ``@sha256:``.
            In that case Keel is without effect anyway.
    """

    registry: str
    repository: str
    tag: str
    digest: str | None = None

    @property
    def display(self) -> str:
        """Return the reference in the form it takes in the manifest.

        Returns:
            ``repository@sha256:...`` for a pinned image, ``repository:tag``
            otherwise.
        """
        if self.digest:
            return f"{self.repository}@{self.digest[:19]}"
        return f"{self.repository}:{self.tag}"


@dataclass(frozen=True)
class RegistryLookup:
    """Result of asking a registry where a tag currently points.

    Attributes:
        index_digest: The canonical digest of the tag, or ``None`` on failure.
        acceptable: Every digest that counts as a match, i.e. the index digest
            plus, for a multi-arch tag, each per-platform manifest digest.
        error: Human-readable failure reason, empty on success.
    """

    index_digest: str | None
    acceptable: frozenset[str]
    error: str


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
            ``Always`` renders Keel's force policy useless on an unchanged tag,
            because the kubelet then takes the layer it already has locally.
        is_init: Whether this is an initContainer rather than a regular one.
        keel_tracks: Whether Keel would act on this container at all. Always
            ``True`` for regular containers, and for initContainers only with
            an explicit ``keel.sh/initContainers`` opt-in.
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
        """Return the container name with initContainers made recognisable.

        Returns:
            The name, prefixed with ``init:`` for an initContainer.
        """
        return f"init:{self.container}" if self.is_init else self.container

    @property
    def restart_helps(self) -> bool:
        """Tell whether a ``rollout restart`` can renew the image at all.

        Returns:
            ``True`` only for ``imagePullPolicy: Always``. Otherwise the kubelet
            does not re-fetch the unchanged tag and a restart goes in circles.
        """
        return self.pull_policy == "Always"


@dataclass
class RegistryAuth:
    """Credentials per registry.

    Attributes:
        by_registry: Registry key mapped to ``(user, password)``.
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
            A ``(username, password)`` pair, or ``None`` if neither this
            instance nor its ``fallback`` has a match.
        """
        if registry in self.by_registry:
            return self.by_registry[registry]
        # In dockerconfigjson, Docker Hub is traditionally written as a fully
        # qualified v1 URL rather than a bare host.
        if registry == DOCKER_HUB_REGISTRY:
            for key in ("https://index.docker.io/v1/", "docker.io", "https://docker.io"):
                if key in self.by_registry:
                    return self.by_registry[key]
        # Other registries may carry a scheme as well.
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
        The parsed reference. A missing tag is assumed to be ``latest``, a
        missing registry to be Docker Hub.
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


def _authenticated_get(
    url: str,
    credentials: tuple[str, str] | None,
    *,
    method: str = "GET",
) -> requests.Response:
    """Run a registry request including the bearer token handshake.

    Registries answer the first attempt with a 401 and a ``WWW-Authenticate``
    header naming the token endpoint and the scope. Only with the token fetched
    from there does the actual request succeed.

    Args:
        url: Full registry URL.
        credentials: Optional ``(username, password)`` pair for private repos.
        method: HTTP method, ``GET`` or ``HEAD``.

    Returns:
        The response of the second, authorised request -- or that of the first,
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
        realm_match.group(1),
        params=params,
        auth=credentials,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not token_response.ok:
        return response

    try:
        body = cast(TokenResponse, token_response.json())
    except ValueError:
        return response

    token = body.get("token") or body.get("access_token")
    if not token:
        return response

    headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)


def registry_digests(image: ImageRef, credentials: tuple[str, str] | None) -> RegistryLookup:
    """Determine the digests a tag currently points at.

    A multi-arch tag points at an index, which in turn references one manifest
    per platform. Which of the two digests the container runtime carries in the
    ``imageID`` is not consistent -- hence this function returns both levels,
    and the comparison counts as satisfied as soon as the running digest matches
    any of them.

    Args:
        image: The image reference to check.
        credentials: Optional ``(username, password)`` pair.

    Returns:
        The lookup result. On failure its ``index_digest`` is ``None`` and its
        ``error`` explains why.
    """
    host = DOCKER_HUB_API_HOST if image.registry == DOCKER_HUB_REGISTRY else image.registry
    base = f"https://{host}/v2/{image.repository}/manifests"
    try:
        response = _authenticated_get(f"{base}/{image.tag}", credentials)
    except requests.exceptions.RequestException as exc:
        # A hanging or unreachable registry must not tear down the whole run --
        # the affected image becomes "unclear", the rest keeps going.
        return RegistryLookup(None, frozenset(), f"unreachable: {type(exc).__name__}")
    if not response.ok:
        detail = response.text.strip()[:120].replace("\n", " ")
        return RegistryLookup(None, frozenset(), f"HTTP {response.status_code} {detail}")

    index_digest = response.headers.get("Docker-Content-Digest")
    acceptable: set[str] = set()
    if index_digest:
        acceptable.add(index_digest)

    try:
        manifest = cast(ManifestIndex, response.json())
    except ValueError:
        return RegistryLookup(index_digest, frozenset(acceptable), "")

    for child in manifest.get("manifests", []):
        child_digest = child.get("digest")
        if child_digest:
            acceptable.add(child_digest)

    return RegistryLookup(index_digest, frozenset(acceptable), "")


def child_digests(image: ImageRef, digest: str, credentials: tuple[str, str] | None) -> frozenset[str]:
    """Resolve a concrete digest and return the platform manifests below it.

    Needed when the running digest does not match the current tag. Registries
    occasionally re-push the index of a tag -- because attestation entries
    changed, for instance -- without the platform manifests underneath moving at
    all. The index digest is new then, but the bits actually running are
    identical. Without this resolution the tool would report such a case as
    stale, which is index drift misread as image drift.

    Args:
        image: The image reference, for registry and repository.
        digest: The digest to resolve, typically taken from an ``imageID``.
        credentials: Optional ``(username, password)`` pair.

    Returns:
        The platform manifests the digest references. Empty if the digest is not
        an index or cannot be fetched -- both mean "no evidence of equivalence",
        which the caller treats as drift.
    """
    host = DOCKER_HUB_API_HOST if image.registry == DOCKER_HUB_REGISTRY else image.registry
    try:
        response = _authenticated_get(f"https://{host}/v2/{image.repository}/manifests/{digest}", credentials)
    except requests.exceptions.RequestException:
        return frozenset()
    if not response.ok:
        return frozenset()

    try:
        manifest = cast(ManifestIndex, response.json())
    except ValueError:
        return frozenset()

    return frozenset(child["digest"] for child in manifest.get("manifests", []) if child.get("digest"))


def load_pull_credentials(core: client.CoreV1Api, namespace: str, secret_names: Sequence[str]) -> RegistryAuth:
    """Read ``imagePullSecrets`` and build a registry mapping from them.

    Args:
        core: Client for the Core API.
        namespace: Namespace the secrets live in.
        secret_names: Names of the referenced secrets.

    Returns:
        The credentials found. Unreadable or badly formatted secrets are skipped
        silently -- a missing login shows up as a visible HTTP 401 later anyway.
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
            parsed = cast(DockerConfig, json.loads(base64.b64decode(raw)))
        except (ValueError, json.JSONDecodeError):
            continue

        auth.by_registry.update(parse_docker_auths(parsed))
    return auth


def parse_docker_auths(config_json: DockerConfig) -> dict[str, tuple[str, str]]:
    """Extract the registry logins from a Docker config structure.

    Args:
        config_json: Parsed contents of a ``config.json`` or a
            ``.dockerconfigjson``.

    Returns:
        Registry key mapped to ``(user, password)``. Entries without usable
        credentials are left out.
    """
    found: dict[str, tuple[str, str]] = {}
    for registry, entry in (config_json.get("auths") or {}).items():
        username = entry.get("username")
        password = entry.get("password")
        encoded = entry.get("auth")
        if not username and encoded:
            try:
                decoded = base64.b64decode(encoded).decode()
                username, _, password = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                continue
        if username and password:
            found[registry] = (username, password)
    return found


def load_local_docker_config() -> tuple[RegistryAuth, str]:
    """Load the local Docker login as a fallback for public images.

    Without a login Docker Hub counts anonymously and per IP -- 100 manifest
    queries an hour, which a run across all tracked images can already blow
    through. With a login it counts per account and far more generously.

    Honours ``DOCKER_CONFIG`` like the docker CLI does and otherwise falls back
    to ``~/.docker/config.json``.

    Returns:
        The credentials found and a short description of where they came from,
        for display.
    """
    base = os.environ.get("DOCKER_CONFIG")
    path = Path(base) / "config.json" if base else Path.home() / ".docker/config.json"

    if not path.is_file():
        return RegistryAuth(), f"{path} does not exist"

    try:
        parsed = cast(DockerConfig, json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        return RegistryAuth(), f"{path} not readable: {exc}"

    # With credsStore/credHelpers the password is not in the file but has to be
    # fetched via docker-credential-<helper>. We do not reimplement that here --
    # but silently doing the wrong thing would be worse than saying so.
    helpers = []
    creds_store = parsed.get("credsStore")
    cred_helpers = parsed.get("credHelpers")
    if creds_store:
        helpers.append(f"credsStore={creds_store}")
    if cred_helpers:
        helpers.append(f"credHelpers={sorted(cred_helpers)}")

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
        A mapping of container name to the digest part of its ``imageID``,
        covering regular and init containers alike. Only running pods are
        considered -- a pod in CrashLoop says nothing about what is being served
        regularly.
    """
    digests: dict[str, str] = {}
    pods = core.list_namespaced_pod(namespace=namespace, label_selector=selector)
    for pod in pods.items:
        pod_status = pod.status
        if pod_status is None or pod_status.phase != "Running":
            continue
        # By the time we ask, initContainers have long finished, but their
        # status still carries the digest they ran with.
        statuses = list(pod_status.container_statuses or []) + list(pod_status.init_container_statuses or [])
        for status in statuses:
            image_id = status.image_id or ""
            if "@" in image_id:
                digests[status.name] = image_id.split("@", 1)[1]
    return digests


def collect_workloads(apps: client.AppsV1Api, namespace: str | None) -> list[tuple[str, Workload]]:
    """Collect every workload that carries a Keel policy.

    Args:
        apps: Client for the Apps API.
        namespace: Namespace to restrict to, or ``None`` for all of them.

    Returns:
        Pairs of resource kind and object.
    """
    sources: tuple[tuple[str, Sequence[Workload]], ...]
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
            if item.metadata is not None and keel_policy(item.metadata) is not None:
                found.append((kind, item))
    return found


def keel_tracks_init_containers(meta: client.V1ObjectMeta) -> bool:
    """Tell whether Keel follows this workload's initContainers.

    Mirrors ``getInitContainerTrackingFromMeta``: opt-in, default ``false``, key
    matched case-insensitively, value has to be exactly ``"true"``. Unlike the
    policy lookup, this one checks **labels first**, then annotations.

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

    Mirrors the order used by ``GetPolicyFromLabelsOrAnnotations``: annotations
    beat labels, and an entry is only found if it is set at all.

    Args:
        meta: Metadata of the workload.

    Returns:
        The policy name, or ``None`` if Keel does not touch the workload -- that
        is, if neither annotation nor label is set, or the policy reads
        ``never`` or is empty.
    """
    for source in (meta.annotations, meta.labels):
        policy = (source or {}).get(KEEL_POLICY_KEY)
        if policy is not None:
            return None if policy in KEEL_INACTIVE_POLICIES else policy
    return None


def analyse(namespace: str | None, verbose: bool, use_local_credentials: bool = True) -> list[Finding]:
    """Compare every tracked workload against its registry.

    Args:
        namespace: Namespace to restrict to, or ``None`` for all of them.
        verbose: If set, log every single step to stderr.
        use_local_credentials: If set, use the local Docker login as a fallback
            where the cluster provides no ``imagePullSecret``.

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
            err_console.print(f"[cyan]Local Docker login used as fallback: {origin}[/]")
        else:
            err_console.print(
                f"[yellow]No local Docker login usable: {origin} -- "
                f"public images are queried anonymously (100/h per IP).[/]"
            )

    findings: list[Finding] = []
    digest_cache: dict[DigestCacheKey, RegistryLookup] = {}
    child_cache: dict[ChildCacheKey, frozenset[str]] = {}

    with err_console.status("[cyan]Looking for workloads with keel.sh/policy…[/]"):
        workloads = collect_workloads(apps, namespace)

    scope = f"namespace {namespace}" if namespace else "all namespaces"
    err_console.print(f"[cyan]{len(workloads)} tracked workloads found in {scope}.[/]")
    if not workloads:
        return findings

    seen_namespaces: set[str] = set()

    # The progress display deliberately goes to stderr so that stdout stays
    # clean for a pipe. transient: the bar disappears again after the run.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=err_console,
        transient=not verbose,
    ) as progress:
        task = progress.add_task("Checking…", total=len(workloads))

        for kind, workload in workloads:
            meta = workload.metadata
            spec = workload.spec.template.spec
            assert meta is not None and meta.namespace is not None and meta.name is not None and spec is not None

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

            secret_names = [ref.name for ref in (spec.image_pull_secrets or []) if ref.name]
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
                child_cache=child_cache,
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
    digest_cache: dict[DigestCacheKey, RegistryLookup],
    child_cache: dict[ChildCacheKey, frozenset[str]],
    findings: list[Finding],
    progress: Progress,
    task: TaskID,
    verbose: bool,
) -> None:
    """Check the containers of one workload and append the results.

    Regular containers and initContainers are examined alike; whether Keel would
    act on the latter is recorded per finding rather than used to skip them.

    Args:
        kind: Resource kind of the workload.
        meta: Metadata of the workload.
        spec: Pod spec of the workload.
        actual: Running digests per container name.
        auth: Credentials for private registries.
        digest_cache: Shared cache for tag lookups.
        child_cache: Shared cache for resolving a running digest to its platform
            manifests.
        findings: List the results are appended to.
        progress: Progress display whose console is logged through.
        task: ID of the progress task whose description is updated.
        verbose: If set, log every single step.
    """
    assert meta.namespace is not None and meta.name is not None
    tracks_init = keel_tracks_init_containers(meta)
    todo: list[tuple[client.V1Container, bool]] = [(container, False) for container in spec.containers]
    todo += [(container, True) for container in (spec.init_containers or [])]

    for container, is_init in todo:
        if not container.image:
            continue
        image = parse_image(container.image)
        running = actual.get(container.name)
        name = f"init:{container.name}" if is_init else container.name

        if image.digest:
            if verbose:
                progress.console.print(f"      [dim]{name}: {image.display} pinned by digest, skipped[/]")
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

        key: DigestCacheKey = (image.registry, image.repository, image.tag)
        cached = key in digest_cache
        if verbose:
            source = "cache" if cached else f"querying {image.registry}"
            progress.console.print(f"      {name}: {image.display} [dim]({source})[/]")
        if not cached:
            progress.update(
                task,
                description=f"{meta.namespace}/[bold]{meta.name}[/] [dim]→ {image.registry}[/]",
            )
            digest_cache[key] = registry_digests(image, auth.for_registry(image.registry))
        lookup = digest_cache[key]

        if lookup.error:
            status, note = DriftStatus.UNKNOWN, f"registry: {lookup.error}"
        elif not lookup.acceptable:
            # Without a value to compare against, "stale" is a claim rather than
            # a finding -- better to report it as unclear.
            status, note = DriftStatus.UNKNOWN, "no digest from the registry"
        elif running is None:
            status, note = DriftStatus.UNKNOWN, "no running pod"
        elif running in lookup.acceptable:
            status, note = DriftStatus.CURRENT, ""
        elif _resolve_children(image, running, auth, child_cache) & lookup.acceptable:
            # The running digest is an older index pointing at the same platform
            # manifests as the current tag. Content-wise it is up to date, only
            # the index shell was re-pushed -- index drift, not image drift.
            status, note = DriftStatus.CURRENT, "different index, same platform manifests"
        else:
            status, note = DriftStatus.STALE, "tag points elsewhere"

        keel_tracks = tracks_init if is_init else True
        if status is DriftStatus.STALE and not keel_tracks:
            # Keel only touches initContainers with keel.sh/initContainers set
            # to "true". Without it such a container can stay stale indefinitely
            # without anything anywhere drawing attention to it.
            note = f"{note}; initContainer without {KEEL_INIT_CONTAINERS_KEY}, Keel ignores it"

        pull_policy = container.image_pull_policy or "Always"
        if status is DriftStatus.STALE and pull_policy != "Always":
            # A restart does not help here: on an unchanged tag the kubelet
            # takes the layer it already has locally. Keel's force policy runs
            # into the same void and still reports success.
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
                f"         running {_short(running)} vs. registry "
                f"{_short(lookup.index_digest)} → [{colour}]{status}[/]{detail}"
            )

        findings.append(
            Finding(
                namespace=meta.namespace,
                kind=kind,
                name=meta.name,
                container=container.name,
                image=image,
                running=running,
                registry=lookup.index_digest,
                status=status,
                note=note,
                pull_policy=pull_policy,
                is_init=is_init,
                keel_tracks=keel_tracks,
            )
        )


def _resolve_children(
    image: ImageRef,
    running: str,
    auth: RegistryAuth,
    child_cache: dict[ChildCacheKey, frozenset[str]],
) -> frozenset[str]:
    """Resolve a running digest to its platform manifests, memoised.

    Args:
        image: The image reference, for registry and repository.
        running: The digest the pod actually runs.
        auth: Credentials for private registries.
        child_cache: Shared cache across the whole run, so that several
            workloads running the same stale digest cost one request, not one
            each.

    Returns:
        The platform manifests below *running*, empty if it is not an index.
    """
    key: ChildCacheKey = (image.registry, image.repository, running)
    if key not in child_cache:
        child_cache[key] = child_digests(image, running, auth.for_registry(image.registry))
    return child_cache[key]


def _short(digest: str | None) -> str:
    """Shorten a digest to a length that reads well in a terminal.

    Args:
        digest: The full digest, or ``None`` if there is none.

    Returns:
        The first twelve hex characters, or ``-`` for a missing digest.
    """
    if not digest:
        return "-"
    return digest.removeprefix("sha256:")[:12]


def render(findings: Sequence[Finding], drift_only: bool) -> None:
    """Print the results as a table.

    Args:
        findings: The comparison results.
        drift_only: If set, show only stale and unclear rows.
    """
    style = {
        DriftStatus.CURRENT: "green",
        DriftStatus.STALE: "bold red",
        DriftStatus.PINNED: "dim",
        DriftStatus.UNKNOWN: "yellow",
    }

    table = Table(title="Keel drift: running image against registry tag")
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


def load_cluster_config(context: str | None) -> str:
    """Point the Kubernetes client at a cluster.

    With an explicit *context* there is deliberately no in-cluster fallback: a
    named context cannot be honoured from inside a pod, and silently checking a
    different cluster than the one asked for is the worst possible outcome for a
    tool whose whole job is comparing against the right cluster.

    Args:
        context: Name of the kubeconfig context to use, or ``None`` for the
            current context (and, failing that, the in-cluster service account).

    Returns:
        A short description of what was loaded, for display.

    Raises:
        typer.Exit: With code 2 if no usable configuration was found. For an
            unknown context name the available ones are listed first.
    """
    try:
        config.load_kube_config(context=context)  # type: ignore[no-untyped-call]
    except config.ConfigException as exc:
        if context is not None:
            err_console.print(f"[red]Context {context!r} not usable: {exc}[/]")
            err_console.print(f"[yellow]Available: {', '.join(available_contexts()) or 'none'}[/]")
            raise typer.Exit(code=2) from exc
        try:
            config.load_incluster_config()  # type: ignore[no-untyped-call]
        except config.ConfigException as incluster_exc:
            err_console.print(f"[red]Neither a kubeconfig nor an in-cluster context: {incluster_exc}[/]")
            raise typer.Exit(code=2) from incluster_exc
        return "in-cluster service account"

    if context is not None:
        return f"context {context}"
    _, active = config.list_kube_config_contexts()  # type: ignore[no-untyped-call]
    return f"current context {(active or {}).get('name', '?')}"


def available_contexts() -> list[str]:
    """List the context names the kubeconfig offers.

    Returns:
        Every context name found, or an empty list if the kubeconfig cannot be
        read at all -- this only ever feeds an error message, so an unreadable
        file must not raise on top of the failure being reported.
    """
    try:
        contexts, _ = config.list_kube_config_contexts()  # type: ignore[no-untyped-call]
    except config.ConfigException:
        return []
    return [str(entry["name"]) for entry in contexts or []]


@app.command(help=CLI_HELP)
def main(
    namespace: str | None = typer.Option(
        None, "--namespace", "-n", envvar="KEEL_NAMESPACE", help="Check only this namespace."
    ),
    context: str | None = typer.Option(
        None,
        "--context",
        "-c",
        envvar=["KEEL_CONTEXT", "KUBE_CONTEXT"],
        help="kubeconfig context to use (default: the current context).",
    ),
    drift_only: bool = typer.Option(
        False, "--drift-only", envvar="KEEL_DRIFT_ONLY", help="Show only stale and unclear workloads."
    ),
    fix_command: bool = typer.Option(
        False, "--fix-command", envvar="KEEL_FIX_COMMAND", help="Print kubectl rollout restart commands."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", envvar="KEEL_QUIET", help="Suppress the table, print only the summary."
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        envvar="KEEL_VERBOSE",
        help="Log every namespace, workload and registry access individually (DEBUG logging).",
    ),
    no_local_credentials: bool = typer.Option(
        False,
        "--no-local-credentials",
        envvar="KEEL_NO_LOCAL_CREDENTIALS",
        help="Ignore the local Docker login and query everything anonymously.",
    ),
) -> None:
    """Check whether Keel-tracked workloads lag behind their tag.

    Keel memorises digests in memory only and seeds them from the registry at
    startup. A push during a Keel restart is therefore never recognised as a
    change. This script finds the cases left behind that way -- useful before
    and after every Keel rollout.

    The exit code is 1 as soon as at least one workload is stale, so the script
    works as a gate in a pipeline.

    Args:
        namespace: Restrict the check to this namespace.
        context: kubeconfig context to check against, ``None`` for the current
            one. Also readable from ``KEEL_CONTEXT`` or ``KUBE_CONTEXT``.
        drift_only: Show only stale and unclear workloads.
        fix_command: Print the kubectl commands that would straighten things out.
        quiet: Suppress the table and print only the summary line.
        verbose: Log every namespace, workload and registry access.
        no_local_credentials: Ignore the local Docker login.

    Raises:
        typer.Exit: Always -- with code 2 when no Kubernetes context could be
            loaded, 1 when at least one workload is stale, 0 otherwise.
    """
    configure_logging(verbose=verbose)
    print_banner(Path(__file__).stem)
    err_console.print(f"[cyan]Checking against {load_cluster_config(context)}.[/]")

    findings = analyse(namespace, verbose, not no_local_credentials)
    if not findings:
        console.print("[yellow]No workloads with keel.sh/policy found.[/]")
        raise typer.Exit(code=0)

    if not quiet:
        render(findings, drift_only)

    stale = [item for item in findings if item.status == DriftStatus.STALE]
    unknown = [item for item in findings if item.status == DriftStatus.UNKNOWN]

    console.print(
        f"{len(findings)} containers checked, "
        f"[bold red]{len(stale)} stale[/], "
        f"[yellow]{len(unknown)} unclear[/]"
    )

    if fix_command and stale:
        restartable = [item for item in stale if item.restart_helps]
        blocked = [item for item in stale if not item.restart_helps]
        # The printed commands must target the same cluster that was checked --
        # without this the user would paste them against their current context.
        context_flag = f" --context={context}" if context else ""

        if restartable:
            console.print("\n[bold]To straighten out:[/]")
            seen: set[tuple[str, str, str]] = set()
            for item in restartable:
                key = (item.namespace, item.kind, item.name)
                if key in seen:
                    continue
                seen.add(key)
                console.print(
                    f"  kubectl{context_flag} rollout restart -n {item.namespace} {item.kind.lower()}/{item.name}"
                )

        if blocked:
            console.print(
                "\n[bold yellow]A restart does not help here[/] -- the kubelet "
                "does not re-fetch the unchanged tag with imagePullPolicy != "
                "Always. Fix the policy first:"
            )
            for item in blocked:
                console.print(
                    f"  kubectl{context_flag} set image ... [dim]# {item.namespace}/{item.name} "
                    f"container {item.label}: imagePullPolicy="
                    f"{item.pull_policy} → Always[/]"
                )

    raise typer.Exit(code=1 if stale else 0)


if __name__ == "__main__":
    app()
