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

GitHub allows 60 anonymous API requests per hour, so a token is picked up from
``GITHUB_TOKEN`` / ``GH_TOKEN`` or, failing that, from ``gh auth token``.

Usage::

    python3 -m k3shelperstuff.pin_drift                        # every declared pin
    python3 -m k3shelperstuff.pin_drift --updates-only         # hide the pins that are current
    python3 -m k3shelperstuff.pin_drift --only mosquitto       # a single pin (repeatable)
    python3 -m k3shelperstuff.pin_drift --config ../pin_drift.yml

Every option can also be given as a ``PIN_DRIFT_*`` environment variable (the
CLI option wins).

Exit codes:

* ``0`` — every pin is current (or unclear).
* ``1`` — at least one pin has an update available, so the tool doubles as a pipeline gate.
* ``2`` — no usable ``pin_drift.yml`` or an unknown ``--only`` name.

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/k3shelperstuff/pin_drift.py
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, TypedDict, cast

import requests
import typer
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from rich.console import Console
from rich.table import Table

from k3shelperstuff import configure_logging, print_banner

CONFIG_NAME = "pin_drift.yml"
GITHUB_API_HOST = "api.github.com"
GITCRYPT_MAGIC = b"\x00GITCRYPT"
DEFAULT_TAG_PATTERN = r"^v?\d+(?:\.\d+)+$"
REQUEST_TIMEOUT_SECONDS = 20
MAX_WORKERS = 8
# GitHub caps a page at 100 entries, Forgejo/Gitea at 50 by default.
PAGE_SIZE = {"github": ("per_page", 100), "forgejo": ("limit", 50)}

_WIDE = 200

console = Console(width=None if sys.stdout.isatty() else _WIDE)
err_console = Console(stderr=True, width=None if sys.stderr.isatty() else _WIDE)

CLI_HELP = """Check whether version pins in the repo lag behind their upstream release.

Reads the pins declared in pin_drift.yml from the repo files and compares each
against the release list of its upstream project. Read-only.

The exit code is 1 as soon as at least one pin has an update available, so the
tool works as a gate in a pipeline.
"""

app = typer.Typer(add_completion=False)

type VersionKey = tuple[int, ...]


class ReleaseEntry(TypedDict, total=False):
    tag_name: str
    name: str
    prerelease: bool
    draft: bool


class PinStatus(StrEnum):
    CURRENT = "current"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "MAJOR"
    UNCLEAR = "unclear"


UPDATE_STATUSES = (PinStatus.MAJOR, PinStatus.MINOR, PinStatus.PATCH)
STATUS_ORDER = (*UPDATE_STATUSES, PinStatus.UNCLEAR, PinStatus.CURRENT)
STATUS_STYLE = {
    PinStatus.CURRENT: "green",
    PinStatus.PATCH: "yellow",
    PinStatus.MINOR: "bold yellow",
    PinStatus.MAJOR: "bold red",
    PinStatus.UNCLEAR: "magenta",
}


@dataclass(frozen=True)
class Upstream:
    kind: Literal["github", "forgejo"]
    repo: str
    source: Literal["releases", "tags"]

    @property
    def url(self) -> str:
        if self.kind == "github":
            return f"https://{GITHUB_API_HOST}/repos/{self.repo}/{self.source}"
        host, _, path = self.repo.partition("/")
        return f"https://{host}/api/v1/repos/{path}/{self.source}"

    @property
    def display(self) -> str:
        return self.repo if self.kind == "github" else f"{self.repo} (forgejo)"


class PinSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    file: str | None = None
    files: tuple[str, ...] = ()
    var: str | None = None
    image: str | None = None
    pattern: str | None = None
    github: str | None = None
    forgejo: str | None = None
    source: Literal["releases", "tags"] = "releases"
    tag_pattern: str = DEFAULT_TAG_PATTERN

    @field_validator("pattern", "tag_pattern")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"not a valid regular expression: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _exactly_one_of_each(self) -> Self:
        if not self.paths:
            raise ValueError("needs 'file' or 'files'")
        if sum(item is not None for item in (self.var, self.image, self.pattern)) != 1:
            raise ValueError("needs exactly one of 'var', 'image', 'pattern'")
        if sum(item is not None for item in (self.github, self.forgejo)) != 1:
            raise ValueError("needs exactly one of 'github', 'forgejo'")
        if self.pattern is not None and re.compile(self.pattern).groups != 1:
            raise ValueError("'pattern' needs exactly one capture group")
        return self

    @property
    def paths(self) -> tuple[str, ...]:
        return (*((self.file,) if self.file else ()), *self.files)

    @property
    def extractor(self) -> re.Pattern[str]:
        if self.var is not None:
            return re.compile(rf"^\s*{re.escape(self.var)}:\s*[\"']?([^\"'\s#]+)", re.MULTILINE)
        if self.image is not None:
            return re.compile(rf"(?<![\w./-]){re.escape(self.image)}:(\w[\w.+-]*)")
        return re.compile(self.pattern or "", re.MULTILINE)

    @property
    def upstream(self) -> Upstream:
        if self.github is not None:
            return Upstream("github", self.github, self.source)
        return Upstream("forgejo", self.forgejo or "", self.source)


class PinConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pins: tuple[PinSpec, ...]

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [spec.name for spec in self.pins]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate pin names: {', '.join(duplicates)}")
        return self


@dataclass(frozen=True)
class TagLookup:
    tags: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class Finding:
    spec: PinSpec
    pinned: str | None
    latest: str | None
    status: PinStatus
    note: str = ""


def version_key(text: str) -> VersionKey:
    # A number running into a letter belongs to a word ("k3s"), not to the version.
    return tuple(int(part) for part in re.findall(r"\d+(?![A-Za-z\d])", text))


def classify(pinned: VersionKey, candidate: VersionKey) -> PinStatus:
    # Truncating to the pin's precision makes a floating "5.1" cover every 5.1.x.
    head = candidate[: len(pinned)]
    if head <= pinned:
        return PinStatus.CURRENT
    index = next(i for i, (old, new) in enumerate(zip(pinned, head)) if old != new)
    return (PinStatus.MAJOR, PinStatus.MINOR)[index] if index < 2 else PinStatus.PATCH


def find_config(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path) -> PinConfig:
    try:
        raw = yaml.safe_load(path.read_text())
        return PinConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        err_console.print(f"[red]{path} is not usable:[/] {exc}")
        raise typer.Exit(code=2) from exc


def github_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    gh = shutil.which("gh")
    if gh is None:
        return None
    try:
        result = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=5, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    return result.stdout.strip() or None


def fetch_tags(upstream: Upstream, token: str | None) -> TagLookup:
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
        payload = response.json()
    except ValueError:
        return TagLookup(error="no JSON in the response")
    if not isinstance(payload, list):
        return TagLookup(error="unexpected response shape")

    entries = cast(list[ReleaseEntry], payload)
    if upstream.source == "tags":
        return TagLookup(tags=tuple(entry["name"] for entry in entries if entry.get("name")))
    return TagLookup(
        tags=tuple(
            entry["tag_name"]
            for entry in entries
            if entry.get("tag_name") and not entry.get("prerelease") and not entry.get("draft")
        )
    )


def read_pin(spec: PinSpec, root: Path) -> tuple[str | None, str]:
    found: set[str] = set()
    for relative in spec.paths:
        try:
            data = (root / relative).read_bytes()
        except OSError as exc:
            return None, f"{relative}: {exc.strerror or type(exc).__name__}"
        if data.startswith(GITCRYPT_MAGIC):
            return None, f"{relative}: git-crypt locked"
        matches = spec.extractor.findall(data.decode(errors="replace"))
        if not matches:
            return None, f"{relative}: pin not found"
        found.update(matches)
    if len(found) > 1:
        return None, f"inconsistent pins: {', '.join(sorted(found))}"
    return found.pop(), ""


def evaluate(spec: PinSpec, root: Path, lookup: TagLookup) -> Finding:
    pinned, problem = read_pin(spec, root)
    if pinned is None:
        return Finding(spec, None, None, PinStatus.UNCLEAR, problem)
    pinned_key = version_key(pinned)
    if not pinned_key:
        return Finding(spec, pinned, None, PinStatus.UNCLEAR, "pin carries no version number")
    if lookup.error:
        return Finding(spec, pinned, None, PinStatus.UNCLEAR, f"upstream: {lookup.error}")

    matcher = re.compile(spec.tag_pattern)
    candidates = sorted((version_key(tag), tag) for tag in lookup.tags if matcher.search(tag))
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


def analyse(specs: Sequence[PinSpec], root: Path, token: str | None) -> list[Finding]:
    upstreams = sorted({spec.upstream for spec in specs}, key=lambda item: (item.kind, item.repo, item.source))
    with err_console.status(f"[cyan]asking {len(upstreams)} upstream projects...[/]"):
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            lookups = dict(zip(upstreams, pool.map(lambda item: fetch_tags(item, token), upstreams)))
    findings = [evaluate(spec, root, lookups[spec.upstream]) for spec in specs]
    findings.sort(key=lambda item: (STATUS_ORDER.index(item.status), item.spec.name))
    return findings


def render(findings: Sequence[Finding], updates_only: bool) -> None:
    table = Table(title="Pin drift: pinned version against upstream release")
    table.add_column("Pin")
    table.add_column("Upstream")
    table.add_column("pinned", justify="right")
    table.add_column("latest", justify="right")
    table.add_column("Status")
    table.add_column("Note", overflow="fold")

    for finding in findings:
        if updates_only and finding.status is PinStatus.CURRENT:
            continue
        table.add_row(
            finding.spec.name,
            finding.spec.upstream.display,
            finding.pinned or "-",
            finding.latest or "-",
            f"[{STATUS_STYLE[finding.status]}]{finding.status}[/]",
            finding.note,
        )

    console.print(table)


@app.command(help=CLI_HELP)
def main(
    config_path: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        envvar="PIN_DRIFT_CONFIG",
        help=f"Pin declarations (default: {CONFIG_NAME}, searched upwards from the current directory).",
    ),
    only: list[str] | None = typer.Option(
        None, "--only", "-o", envvar="PIN_DRIFT_ONLY", help="Check only this pin (repeatable)."
    ),
    updates_only: bool = typer.Option(
        False, "--updates-only", envvar="PIN_DRIFT_UPDATES_ONLY", help="Hide the pins that are current."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", envvar="PIN_DRIFT_QUIET", help="Suppress the table, print only the summary."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", envvar="PIN_DRIFT_VERBOSE", help="DEBUG logging."),
) -> None:
    configure_logging(verbose=verbose)
    print_banner("pin_drift")

    path = config_path or find_config(Path.cwd())
    if path is None:
        err_console.print(f"[red]No {CONFIG_NAME} found in {Path.cwd()} or any parent directory.[/]")
        raise typer.Exit(code=2)
    specs = list(load_config(path).pins)

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

    findings = analyse(specs, path.resolve().parent, token)
    if not quiet:
        render(findings, updates_only)

    counts = {status: sum(item.status is status for item in findings) for status in PinStatus}
    console.print(
        f"{len(findings)} pins checked, "
        f"[bold red]{counts[PinStatus.MAJOR]} major[/], "
        f"[bold yellow]{counts[PinStatus.MINOR]} minor[/], "
        f"[yellow]{counts[PinStatus.PATCH]} patch[/], "
        f"[magenta]{counts[PinStatus.UNCLEAR]} unclear[/]"
    )

    raise typer.Exit(code=1 if any(counts[status] for status in UPDATE_STATUSES) else 0)


if __name__ == "__main__":
    app()
