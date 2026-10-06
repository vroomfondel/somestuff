#!/usr/bin/env python3
"""Find version pins in a repo that have fallen behind their upstream release.

Counterpart to :mod:`k3shelperstuff.keel_drift`: that one compares the digest of
a rolling tag, this one compares a fixed pin (``v1.2.3``) against the release
list of the project it comes from.

The pins are declared in ``pin_drift.yml``, searched upwards from the current
directory. Each entry names the files carrying the pin, how to extract it
(``var``, ``image`` or a raw ``pattern`` with one capture group) and where the
releases live (``github: owner/repo`` or ``forgejo: host/owner/repo``). Nothing
is ever written: the tool reads the repo and the release APIs only.

A pin shorter than the upstream version (``5.1`` against ``5.1.3``) is a
floating tag and counts as current until a newer ``5.2`` appears.

With a usable kubeconfig the table gains a ``running`` column: the tags of the
matching container images in the cluster's pods (``image`` pins match by
default, ``running_image`` overrides, ``running_kubelet`` reads the nodes'
kubelet version). A running version that differs from the pin is flagged as
not (or only partially) rolled out; it does not change the exit code.

GitHub allows 60 anonymous API requests per hour, so a token is picked up from
``GITHUB_TOKEN`` / ``GH_TOKEN`` or, failing that, from ``gh auth token``.

Every option can also be given as a ``PIN_DRIFT_*`` environment variable (the
CLI option wins).

Exit codes:
    * ``0`` — every pin is current (or unclear).
    * ``1`` — at least one pin has an update available, so the tool doubles as
      a pipeline gate.
    * ``2`` — no usable ``pin_drift.yml``, an unknown ``--only`` name or an
      unusable explicit ``--context``.

Examples:
    Typical invocations::

        python3 -m k3shelperstuff.pin_drift                        # every declared pin
        python3 -m k3shelperstuff.pin_drift --updates-only         # hide the pins that are current
        python3 -m k3shelperstuff.pin_drift --only mosquitto       # a single pin (repeatable)
        python3 -m k3shelperstuff.pin_drift --config ../pin_drift.yml
        python3 -m k3shelperstuff.pin_drift --context ht@heidk8    # running versions from this context
        python3 -m k3shelperstuff.pin_drift --no-running           # repo against upstream only

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/k3shelperstuff/pin_drift.py
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Self, TypedDict

import requests
import typer
import urllib3
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator, model_validator
from rich.console import Console
from rich.table import Table

from k3shelperstuff import configure_logging, print_banner
from k3shelperstuff.keel_drift import parse_image

type VersionKey = tuple[int, ...]
type UpstreamKind = Literal["github", "forgejo"]
type TagSource = Literal["releases", "tags"]
type ImageRepo = tuple[str, str]

CONFIG_NAME: Final[str] = "pin_drift.yml"
GITHUB_API_HOST: Final[str] = "api.github.com"
GITCRYPT_MAGIC: Final[bytes] = b"\x00GITCRYPT"
DEFAULT_TAG_PATTERN: Final[str] = r"^v?\d+(?:\.\d+)+$"
REQUEST_TIMEOUT_SECONDS: Final[int] = 20
FINISHED_POD_PHASES: Final[frozenset[str]] = frozenset({"Succeeded", "Failed"})
MAX_WORKERS: Final[int] = 8
# GitHub caps a page at 100 entries, Forgejo/Gitea at 50 by default.
PAGE_SIZE: Final[Mapping[UpstreamKind, tuple[str, int]]] = {"github": ("per_page", 100), "forgejo": ("limit", 50)}

_WIDE: Final[int] = 200

console: Final[Console] = Console(width=None if sys.stdout.isatty() else _WIDE)
err_console: Final[Console] = Console(stderr=True, width=None if sys.stderr.isatty() else _WIDE)

CLI_HELP: Final[str] = """Check whether version pins in the repo lag behind their upstream release.

Reads the pins declared in pin_drift.yml from the repo files and compares each
against the release list of its upstream project. Read-only.

The exit code is 1 as soon as at least one pin has an update available, so the
tool works as a gate in a pipeline.
"""

app: Final[typer.Typer] = typer.Typer(add_completion=False)


class ReleaseEntry(TypedDict, total=False):
    """One element of a release or tag listing from the GitHub/Forgejo API.

    Only the fields the tool reads are declared; everything else in the
    payload is dropped during validation.

    Attributes:
        tag_name: Git tag of a release (``/releases`` endpoint only).
        name: Tag name on the ``/tags`` endpoint; free-form title (possibly
            ``null``) on the ``/releases`` endpoint.
        prerelease: Whether the release is flagged as a pre-release.
        draft: Whether the release is an unpublished draft.
    """

    tag_name: str
    name: str | None
    prerelease: bool
    draft: bool


_RELEASE_LIST: Final[TypeAdapter[list[ReleaseEntry]]] = TypeAdapter(list[ReleaseEntry])


class PinStatus(StrEnum):
    """Outcome of comparing one pin against its upstream.

    Attributes:
        CURRENT: The pin covers the newest upstream version.
        PATCH: A newer patch release exists.
        MINOR: A newer minor release exists.
        MAJOR: A newer major release exists.
        UNCLEAR: The pin or the upstream could not be determined.
    """

    CURRENT = "current"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "MAJOR"
    UNCLEAR = "unclear"


UPDATE_STATUSES: Final[tuple[PinStatus, ...]] = (PinStatus.MAJOR, PinStatus.MINOR, PinStatus.PATCH)
STATUS_ORDER: Final[tuple[PinStatus, ...]] = (*UPDATE_STATUSES, PinStatus.UNCLEAR, PinStatus.CURRENT)
STATUS_STYLE: Final[Mapping[PinStatus, str]] = {
    PinStatus.CURRENT: "green",
    PinStatus.PATCH: "yellow",
    PinStatus.MINOR: "bold yellow",
    PinStatus.MAJOR: "bold red",
    PinStatus.UNCLEAR: "magenta",
}


@dataclass(frozen=True, order=True)
class Upstream:
    """The project whose releases a pin is compared against.

    Attributes:
        kind: Forge flavour, decides the API layout.
        repo: ``owner/repo`` for GitHub, ``host/owner/repo`` for Forgejo.
        source: Whether to list ``releases`` or plain ``tags``.
    """

    kind: UpstreamKind
    repo: str
    source: TagSource

    @property
    def url(self) -> str:
        """API endpoint that lists the releases or tags of the project.

        Returns:
            Absolute HTTPS URL of the listing endpoint.
        """
        if self.kind == "github":
            return f"https://{GITHUB_API_HOST}/repos/{self.repo}/{self.source}"
        host, _, path = self.repo.partition("/")
        return f"https://{host}/api/v1/repos/{path}/{self.source}"

    @property
    def display(self) -> str:
        """Human-readable label for the result table.

        Returns:
            The repo path, suffixed with ``(forgejo)`` for non-GitHub upstreams.
        """
        return self.repo if self.kind == "github" else f"{self.repo} (forgejo)"


class PinSpec(BaseModel):
    """One pin declaration from ``pin_drift.yml``.

    Attributes:
        name: Unique name of the pin, used for ``--only`` and in the output.
        file: Single file carrying the pin, relative to the config directory.
        files: Further files carrying the same pin; all must agree.
        var: YAML-style key whose value is the pin (``key: value``).
        image: Image name whose tag is the pin (``image:tag``).
        pattern: Raw regular expression with exactly one capture group.
        github: Upstream as ``owner/repo`` on GitHub.
        forgejo: Upstream as ``host/owner/repo`` on a Forgejo/Gitea instance.
        source: Whether to compare against ``releases`` or ``tags``.
        tag_pattern: Regular expression an upstream tag must match to count.
        running_image: Image names whose tags in the cluster's pods are the
            running version; defaults to ``image``.
        running_kubelet: Take the running version from the nodes' kubelet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    file: str | None = None
    files: tuple[str, ...] = ()
    var: str | None = None
    image: str | None = None
    pattern: str | None = None
    github: str | None = None
    forgejo: str | None = None
    source: TagSource = "releases"
    tag_pattern: str = DEFAULT_TAG_PATTERN
    running_image: tuple[str, ...] = ()
    running_kubelet: bool = False

    @field_validator("running_image", mode="before")
    @classmethod
    def _single_image(cls, value: object) -> object:
        """Accept a single image name in place of a list.

        Args:
            value: The configured value.

        Returns:
            ``value`` wrapped in a tuple if it is a string, else unchanged.
        """
        return (value,) if isinstance(value, str) else value

    @field_validator("pattern", "tag_pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        """Reject regular expressions that do not compile.

        Args:
            value: The configured expression, or ``None`` if unset.

        Returns:
            The unchanged value.

        Raises:
            ValueError: If ``value`` is not a valid regular expression.
        """
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _exactly_one_of_each(self) -> Self:
        """Enforce the mutually exclusive option groups.

        Returns:
            The validated model.

        Raises:
            ValueError: If no file is given, not exactly one extractor or
                upstream is set, or ``pattern`` lacks exactly one capture group.
        """
        if not self.paths:
            raise ValueError("needs 'file' or 'files'")
        if sum(item is not None for item in (self.var, self.image, self.pattern)) != 1:
            raise ValueError("needs exactly one of 'var', 'image', 'pattern'")
        if sum(item is not None for item in (self.github, self.forgejo)) != 1:
            raise ValueError("needs exactly one of 'github', 'forgejo'")
        if self.pattern is not None and re.compile(self.pattern).groups != 1:
            raise ValueError("'pattern' needs exactly one capture group")
        if self.running_image and self.running_kubelet:
            raise ValueError("'running_image' and 'running_kubelet' exclude each other")
        return self

    @property
    def running_repos(self) -> frozenset[ImageRepo]:
        """Normalised image repositories that carry the running version.

        Returns:
            ``(registry, repository)`` pairs from ``running_image``, else from
            ``image``; empty for kubelet pins and pins without either.
        """
        if self.running_kubelet:
            return frozenset()
        names = self.running_image or ((self.image,) if self.image else ())
        return frozenset(image_repo(name) for name in names)

    @property
    def paths(self) -> tuple[str, ...]:
        """All files carrying the pin.

        Returns:
            ``file`` (if set) followed by ``files``.
        """
        return (*((self.file,) if self.file else ()), *self.files)

    @property
    def extractor(self) -> re.Pattern[str]:
        """Compiled expression that captures the pinned version.

        Returns:
            A pattern with exactly one capture group, built from ``var``,
            ``image`` or ``pattern``, whichever is set.
        """
        if self.var is not None:
            return re.compile(rf"^\s*{re.escape(self.var)}:\s*[\"']?([^\"'\s#]+)", re.MULTILINE)
        if self.image is not None:
            return re.compile(rf"(?<![\w./-]){re.escape(self.image)}:(\w[\w.+-]*)")
        return re.compile(self.pattern or "", re.MULTILINE)

    @property
    def upstream(self) -> Upstream:
        """The upstream project the pin is compared against.

        Returns:
            An :class:`Upstream` built from ``github`` or ``forgejo``.
        """
        if self.github is not None:
            return Upstream("github", self.github, self.source)
        return Upstream("forgejo", self.forgejo or "", self.source)


class PinConfig(BaseModel):
    """Root of ``pin_drift.yml``.

    Attributes:
        pins: All declared pins.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pins: tuple[PinSpec, ...]

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        """Reject duplicate pin names.

        Returns:
            The validated model.

        Raises:
            ValueError: If two pins share a name.
        """
        names = [spec.name for spec in self.pins]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate pin names: {', '.join(duplicates)}")
        return self


@dataclass(frozen=True)
class ClusterSnapshot:
    """Versions currently running in the cluster.

    Attributes:
        context: kubeconfig context the snapshot was taken from, ``None`` for
            the active one.
        images: Tags of every image repository in a live pod.
        kubelet: Kubelet versions of all nodes.
    """

    context: str | None
    images: Mapping[ImageRepo, frozenset[str]]
    kubelet: frozenset[str]

    def running(self, spec: PinSpec) -> tuple[str, ...]:
        """Running versions of one pin.

        Args:
            spec: The pin declaration.

        Returns:
            The distinct versions, oldest first; empty if nothing matches.
        """
        if spec.running_kubelet:
            found: set[str] = set(self.kubelet)
        else:
            found = {tag for repo in spec.running_repos for tag in self.images.get(repo, ())}
        return tuple(sorted(found, key=lambda tag: (version_key(tag), tag)))


@dataclass(frozen=True)
class TagLookup:
    """Result of querying one upstream.

    Attributes:
        tags: Tag names of the usable releases (or tags), newest first as
            delivered by the API.
        error: Reason the lookup failed; empty on success.
    """

    tags: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class Finding:
    """Comparison result for one pin.

    Attributes:
        spec: The pin declaration.
        pinned: Version read from the repo, ``None`` if it could not be read.
        latest: Newest matching upstream tag, ``None`` if unknown.
        status: Outcome of the comparison.
        note: Additional explanation, e.g. why the status is ``UNCLEAR``.
        running: Versions running in the cluster, oldest first.
    """

    spec: PinSpec
    pinned: str | None
    latest: str | None
    status: PinStatus
    note: str = ""
    running: tuple[str, ...] = ()

    @property
    def rolled_out(self) -> bool:
        """Whether the cluster runs exactly the pinned version.

        Returns:
            ``True`` if nothing runs, the pin is unknown, or every running
            version equals the pin (a leading ``v`` aside).
        """
        pinned = self.pinned
        return pinned is None or all(same_version(tag, pinned) for tag in self.running)


def image_repo(image: str) -> ImageRepo:
    """Normalise an image name to registry and repository, dropping tag and digest.

    Args:
        image: Reference such as ``redis``, ``prom/prometheus:v3`` or ``quay.io/ceph/ceph``.

    Returns:
        ``(registry, repository)`` with Docker Hub aliases folded together.
    """
    ref = parse_image(image)
    return ref.registry, ref.repository


def same_version(left: str, right: str) -> bool:
    """Compare two tags, ignoring a leading ``v``.

    Args:
        left: A tag such as ``0.40.0``.
        right: A tag such as ``v0.40.0``.

    Returns:
        Whether both name the same version.
    """
    return left.removeprefix("v") == right.removeprefix("v")


def version_key(text: str) -> VersionKey:
    """Turn a version string into a sortable tuple of numbers.

    Args:
        text: A tag or pin such as ``v1.2.3`` or ``v1.31.4+k3s1``.

    Returns:
        The numeric components, e.g. ``(1, 2, 3)``; empty if there are none.
    """
    # A number running into a letter belongs to a word ("k3s"), not to the version.
    return tuple(int(part) for part in re.findall(r"\d+(?![A-Za-z\d])", text))


def classify(pinned: VersionKey, candidate: VersionKey) -> PinStatus:
    """Classify how far a candidate version is ahead of the pin.

    Args:
        pinned: Version key of the pin.
        candidate: Version key of an upstream release.

    Returns:
        ``CURRENT`` if the candidate is not newer at the pin's precision,
        otherwise ``MAJOR``, ``MINOR`` or ``PATCH`` by the first differing part.
    """
    # Truncating to the pin's precision makes a floating "5.1" cover every 5.1.x.
    head = candidate[: len(pinned)]
    if head <= pinned:
        return PinStatus.CURRENT
    index = next(i for i, (old, new) in enumerate(zip(pinned, head)) if old != new)
    return (PinStatus.MAJOR, PinStatus.MINOR)[index] if index < 2 else PinStatus.PATCH


def find_config(start: Path) -> Path | None:
    """Search for ``pin_drift.yml`` in a directory and its parents.

    Args:
        start: Directory to start the search in.

    Returns:
        Path of the first config file found, or ``None``.
    """
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path) -> PinConfig:
    """Read and validate the pin declarations.

    Args:
        path: Location of ``pin_drift.yml``.

    Returns:
        The validated configuration.

    Raises:
        typer.Exit: With code 2 if the file is unreadable, not YAML, or fails
            validation.
    """
    try:
        raw: object = yaml.safe_load(path.read_text())
        return PinConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        err_console.print(f"[red]{path} is not usable:[/] {exc}")
        raise typer.Exit(code=2) from exc


def github_token() -> str | None:
    """Find a GitHub API token.

    Returns:
        The value of ``GITHUB_TOKEN`` or ``GH_TOKEN``, else the output of
        ``gh auth token``, else ``None``.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if token := os.environ.get(name):
            return token
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=5, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip() or None


def fetch_tags(upstream: Upstream, token: str | None) -> TagLookup:
    """Fetch the first page of releases or tags of an upstream.

    Pre-releases and drafts are skipped when listing releases.

    Args:
        upstream: Project to query.
        token: GitHub API token; ignored for Forgejo.

    Returns:
        The tag names, or a :class:`TagLookup` with ``error`` set if the
        request failed or the response was not a release/tag list.
    """
    headers = {"Accept": "application/json"}
    if upstream.kind == "github" and token:
        headers["Authorization"] = f"Bearer {token}"
    size_key, size = PAGE_SIZE[upstream.kind]
    try:
        response = requests.get(
            upstream.url, headers=headers, params={size_key: str(size)}, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.exceptions.RequestException as exc:
        return TagLookup(error=f"unreachable: {type(exc).__name__}")
    if not response.ok:
        detail = response.text.strip()[:80].replace("\n", " ")
        return TagLookup(error=f"HTTP {response.status_code} {detail}")
    try:
        entries = _RELEASE_LIST.validate_json(response.content)
    except ValidationError as exc:
        if any(error["type"] == "json_invalid" for error in exc.errors()):
            return TagLookup(error="no JSON in the response")
        return TagLookup(error="unexpected response shape")

    if upstream.source == "tags":
        return TagLookup(tags=tuple(name for entry in entries if (name := entry.get("name"))))
    return TagLookup(
        tags=tuple(
            tag
            for entry in entries
            if (tag := entry.get("tag_name")) and not entry.get("prerelease") and not entry.get("draft")
        )
    )


def read_pin(spec: PinSpec, root: Path) -> tuple[str | None, str]:
    """Extract the pinned version from every file of a pin.

    Args:
        spec: The pin declaration.
        root: Directory the pin's file paths are relative to.

    Returns:
        ``(version, "")`` on success, or ``(None, problem)`` if a file is
        unreadable, git-crypt locked, lacks the pin, or the files disagree.
    """
    found: set[str] = set()
    for relative in spec.paths:
        try:
            data = (root / relative).read_bytes()
        except OSError as exc:
            return None, f"{relative}: {exc.strerror or type(exc).__name__}"
        if data.startswith(GITCRYPT_MAGIC):
            return None, f"{relative}: git-crypt locked"
        matches: list[str] = spec.extractor.findall(data.decode(errors="replace"))
        if not matches:
            return None, f"{relative}: pin not found"
        found.update(matches)
    if len(found) > 1:
        return None, f"inconsistent pins: {', '.join(sorted(found))}"
    return found.pop(), ""


def evaluate(spec: PinSpec, root: Path, lookup: TagLookup) -> Finding:
    """Compare one pin against the tags of its upstream.

    Args:
        spec: The pin declaration.
        root: Directory the pin's file paths are relative to.
        lookup: Result of :func:`fetch_tags` for the pin's upstream.

    Returns:
        The finding for this pin, including the newest matching upstream tag
        and, for a major update, the newest tag within the pinned major.
    """
    pinned, problem = read_pin(spec, root)
    if pinned is None:
        return Finding(spec, None, None, PinStatus.UNCLEAR, problem)
    pinned_key = version_key(pinned)
    if not pinned_key:
        return Finding(spec, pinned, None, PinStatus.UNCLEAR, "pin carries no version number")
    if lookup.error:
        return Finding(spec, pinned, None, PinStatus.UNCLEAR, f"upstream: {lookup.error}")

    matcher = re.compile(spec.tag_pattern)
    candidates: list[tuple[VersionKey, str]] = sorted(
        (version_key(tag), tag) for tag in lookup.tags if matcher.search(tag)
    )
    if not candidates:
        return Finding(spec, pinned, None, PinStatus.UNCLEAR, f"no upstream {spec.source} match tag_pattern")

    latest_key, latest = candidates[-1]
    status = classify(pinned_key, latest_key)
    notes: list[str] = []
    if status is PinStatus.CURRENT:
        if latest_key[: len(pinned_key)] < pinned_key:
            notes.append(f"pin is ahead of the newest upstream {spec.source}")
        elif len(pinned_key) < len(latest_key):
            notes.append("floating tag")
    elif status is PinStatus.MAJOR:
        same_major = [(key, tag) for key, tag in candidates if key[0] == pinned_key[0]]
        if same_major and classify(pinned_key, same_major[-1][0]) is not PinStatus.CURRENT:
            notes.append(f"{same_major[-1][1]} within the pinned major")
    return Finding(spec, pinned, latest, status, "; ".join(notes))


def load_cluster(context: str | None) -> ClusterSnapshot | None:
    """Read the image tags of all live pods and the nodes' kubelet versions.

    Args:
        context: kubeconfig context to use, ``None`` for the active one.

    Returns:
        The snapshot, or ``None`` if no cluster is reachable and no context was
        named explicitly.

    Raises:
        typer.Exit: With code 2 if an explicitly named context is unusable.
    """
    try:
        config.load_kube_config(context=context)
    except (config.ConfigException, OSError) as exc:
        if context:
            err_console.print(f"[red]context '{context}' not usable:[/] {exc}")
            raise typer.Exit(code=2) from exc
        try:
            config.load_incluster_config()
        except config.ConfigException:
            err_console.print("[yellow]No kubeconfig and no in-cluster context, skipping the running column.[/]")
            return None

    core = client.CoreV1Api()
    try:
        pods = core.list_pod_for_all_namespaces(_request_timeout=REQUEST_TIMEOUT_SECONDS).items
        nodes = core.list_node(_request_timeout=REQUEST_TIMEOUT_SECONDS).items
    except (ApiException, urllib3.exceptions.HTTPError) as exc:
        if context:
            err_console.print(f"[red]cluster of context '{context}' not usable:[/] {exc}")
            raise typer.Exit(code=2) from exc
        err_console.print(f"[yellow]Cluster not reachable, skipping the running column:[/] {exc}")
        return None

    images: dict[ImageRepo, set[str]] = {}
    for pod in pods:
        if pod.status and pod.status.phase in FINISHED_POD_PHASES:
            continue
        for container in (*(pod.spec.containers or ()), *(pod.spec.init_containers or ())):
            if container.image:
                ref = parse_image(container.image)
                images.setdefault((ref.registry, ref.repository), set()).add(ref.tag)
    kubelet = frozenset(
        node.status.node_info.kubelet_version for node in nodes if node.status and node.status.node_info
    )
    return ClusterSnapshot(context, {repo: frozenset(tags) for repo, tags in images.items()}, kubelet)


def with_running(finding: Finding, cluster: ClusterSnapshot) -> Finding:
    """Attach the running versions to a finding and note an incomplete rollout.

    Args:
        finding: Result of :func:`evaluate`.
        cluster: Snapshot from :func:`load_cluster`.

    Returns:
        A copy of ``finding`` with ``running`` set.
    """
    running = cluster.running(finding.spec)
    result = replace(finding, running=running)
    if result.rolled_out:
        return result
    pinned = finding.pinned or ""
    rollout = "partially rolled out" if any(same_version(tag, pinned) for tag in running) else "not rolled out"
    return replace(result, note="; ".join(filter(None, (finding.note, rollout))))


def analyse(
    specs: Sequence[PinSpec], root: Path, token: str | None, cluster: ClusterSnapshot | None = None
) -> list[Finding]:
    """Check all pins, querying each distinct upstream once in parallel.

    Args:
        specs: Pin declarations to check.
        root: Directory the pins' file paths are relative to.
        token: GitHub API token, or ``None`` for anonymous access.
        cluster: Running versions to attach, or ``None`` to leave them out.

    Returns:
        One finding per pin, ordered by severity, then by name.
    """
    upstreams = sorted({spec.upstream for spec in specs})
    with err_console.status(f"[cyan]asking {len(upstreams)} upstream projects...[/]"):
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            lookups: dict[Upstream, TagLookup] = dict(
                zip(upstreams, pool.map(lambda item: fetch_tags(item, token), upstreams))
            )
    findings = [evaluate(spec, root, lookups[spec.upstream]) for spec in specs]
    if cluster is not None:
        findings = [with_running(finding, cluster) for finding in findings]
    findings.sort(key=lambda item: (STATUS_ORDER.index(item.status), item.spec.name))
    return findings


def running_cell(finding: Finding) -> str:
    """Format the running versions, highlighting those that differ from the pin.

    Args:
        finding: Result carrying the running versions.

    Returns:
        Rich markup for the ``running`` column.
    """
    if not finding.running:
        return "[dim]-[/]"
    style = "green" if finding.rolled_out else "yellow"
    return ", ".join(
        f"[{'green' if same_version(tag, finding.pinned or "") else style}]{tag}[/]" for tag in finding.running
    )


def render(findings: Sequence[Finding], updates_only: bool, show_running: bool = False) -> None:
    """Print the findings as a table to stdout.

    Args:
        findings: Results to show.
        updates_only: Hide pins whose status is ``CURRENT`` and that are
            rolled out.
        show_running: Add the ``running`` column.
    """
    title = "Pin drift: pinned version against upstream release"
    table = Table(title=f"{title} and cluster" if show_running else title)
    table.add_column("Pin")
    table.add_column("Upstream")
    table.add_column("pinned", justify="right")
    if show_running:
        table.add_column("running", justify="right")
    table.add_column("latest", justify="right")
    table.add_column("Status")
    table.add_column("Note", overflow="fold")

    for finding in findings:
        if updates_only and finding.status is PinStatus.CURRENT and finding.rolled_out:
            continue
        table.add_row(
            finding.spec.name,
            finding.spec.upstream.display,
            finding.pinned or "-",
            *((running_cell(finding),) if show_running else ()),
            finding.latest or "-",
            f"[{STATUS_STYLE[finding.status]}]{finding.status}[/]",
            finding.note,
        )

    console.print(table)


@app.command(help=CLI_HELP)
def main(
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            envvar="PIN_DRIFT_CONFIG",
            help=f"Pin declarations (default: {CONFIG_NAME}, searched upwards from the current directory).",
        ),
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option("--only", "-o", envvar="PIN_DRIFT_ONLY", help="Check only this pin (repeatable)."),
    ] = None,
    updates_only: Annotated[
        bool,
        typer.Option("--updates-only", envvar="PIN_DRIFT_UPDATES_ONLY", help="Hide the pins that are current."),
    ] = False,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            envvar="PIN_DRIFT_CONTEXT",
            help="kubeconfig context for the running column (default: the active one).",
        ),
    ] = None,
    no_running: Annotated[
        bool,
        typer.Option(
            "--no-running", envvar="PIN_DRIFT_NO_RUNNING", help="Do not ask the cluster for running versions."
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", envvar="PIN_DRIFT_QUIET", help="Suppress the table, print only the summary."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", envvar="PIN_DRIFT_VERBOSE", help="DEBUG logging.")
    ] = False,
) -> None:
    """CLI entry point: check the declared pins and report the drift.

    Args:
        config_path: Explicit config file; searched upwards from the current
            directory if ``None``.
        only: Restrict the check to these pin names.
        updates_only: Hide pins that are current.
        context: kubeconfig context for the running versions.
        no_running: Skip the cluster entirely.
        quiet: Print only the summary line, not the table.
        verbose: Enable DEBUG logging.

    Raises:
        typer.Exit: Always. Code 0 if no pin has an update, 1 if at least one
            has, 2 for a missing/invalid config, an unknown ``--only`` name or
            an unusable explicit ``--context``.
    """
    configure_logging(verbose=verbose)
    print_banner("pin_drift")

    path = config_path or find_config(Path.cwd())
    if path is None:
        err_console.print(f"[red]No {CONFIG_NAME} found in {Path.cwd()} or any parent directory.[/]")
        raise typer.Exit(code=2)
    specs: list[PinSpec] = list(load_config(path).pins)

    if only:
        unknown = sorted(set(only) - {spec.name for spec in specs})
        if unknown:
            err_console.print(f"[red]Unknown pin: {', '.join(unknown)}[/]")
            err_console.print(f"[yellow]Declared: {', '.join(sorted(spec.name for spec in specs))}[/]")
            raise typer.Exit(code=2)
        specs = [spec for spec in specs if spec.name in only]

    token = github_token()
    access = "authenticated" if token else "anonymous, 60 requests/h"
    err_console.print(f"[cyan]Checking {len(specs)} pins from {path} (GitHub API: {access}).[/]")

    cluster = None if no_running else load_cluster(context)
    if cluster is not None:
        err_console.print(f"[cyan]Running versions from cluster context {cluster.context or '(active)'}.[/]")

    findings = analyse(specs, path.resolve().parent, token, cluster)
    if not quiet:
        render(findings, updates_only, show_running=cluster is not None)

    counts: dict[PinStatus, int] = {status: sum(item.status is status for item in findings) for status in PinStatus}
    console.print(
        f"{len(findings)} pins checked, "
        f"[bold red]{counts[PinStatus.MAJOR]} major[/], "
        f"[bold yellow]{counts[PinStatus.MINOR]} minor[/], "
        f"[yellow]{counts[PinStatus.PATCH]} patch[/], "
        f"[magenta]{counts[PinStatus.UNCLEAR]} unclear[/]"
        + (f", [yellow]{sum(not item.rolled_out for item in findings)} not rolled out[/]" if cluster else "")
    )

    raise typer.Exit(code=1 if any(counts[status] for status in UPDATE_STATUSES) else 0)


if __name__ == "__main__":
    app()
