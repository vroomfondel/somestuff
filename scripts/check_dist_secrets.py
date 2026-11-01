#!/usr/bin/env python3
"""Fail the build if a distribution artifact contains files that must never ship.

Background: hatchling's sdist target has no default file selection. An undeclared
sdist ships *the whole project root minus .gitignore*, while the wheel target
auto-detects the package directory and stays clean. That divergence has already
leaked git-crypt protected vault files in plaintext elsewhere (git-crypt decrypts
into the working tree, and no build backend reads .gitattributes). This script is
the backstop that verifies the *built artifacts*, so a future edit to the build
config cannot silently reintroduce such a leak.

The allowlist is derived from pyproject.toml, not hardcoded. Distributions in this
repo are built from a staging directory (build/<pkg>/, see `make stage-<pkg>`), so
pyproject.toml and dist/ are read from --root while .gitattributes is read from the
repository root (the parent of scripts/).

Four checks per artifact:

  1. Authority check: the sdist allowlist may only come from a key that actually
     governs the sdist, i.e. [tool.hatch.build.targets.sdist] include/only-include
     or the build-level [tool.hatch.build] packages/include/only-include. A
     `packages` key under [tool.hatch.build.targets.wheel] scopes the WHEEL ONLY
     and is deliberately NOT accepted here: trusting it is precisely the bug.
     No usable key means hard failure, not a permissive default.
  2. git-crypt check: no member may match a path pattern that .gitattributes
     marks with `filter=git-crypt`. Patterns are read at runtime, so a newly
     protected path is covered without touching this script.
  3. Allowlist check: every member must sit under a known-good top-level entry.
     This is the check that holds the line, since it also catches secret-bearing
     files that were never git-crypt protected in the first place.
  4. Namespace check (--namespace NAME): no member may be NAME/__init__.py. A PEP 420
     namespace package must stay free of that file in every distribution; one wheel
     shipping it hides the NAME.* packages of all other distributions.

Usage:
    check_dist_secrets.py [--root DIR] [--namespace NAME] [ARTIFACT ...]

With no artifact arguments it checks every .tar.gz and .whl in <root>/dist. Exit
code 0 means the artifacts are clean, 1 means at least one offending member was
found.
"""

import argparse
import fnmatch
import re
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Metadata a build backend adds on its own. Always permitted in an sdist.
SDIST_METADATA: tuple[str, ...] = (
    ".gitignore",
    "PKG-INFO",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
)
# Same, matched case-sensitively as a glob (README.md, LICENSE.md, LICENSEGPL.md, ...).
SDIST_METADATA_GLOBS: tuple[str, ...] = ("README*", "LICENSE*", "COPYING*", "NOTICE*")

# Content patterns that must never appear in a shipped file.
SECRET_CONTENT_RES: tuple[tuple[str, re.Pattern[bytes]], ...] = ()
# Members larger than this are not content-scanned (binary blobs, models, ...).
MAX_SCAN_BYTES = 8 * 1024 * 1024

# Stripped from sdist member paths before matching ("foo-0.0.7/bar" -> "bar").
SDIST_PREFIX_RE = re.compile(r"^[^/]+-[0-9][^/]*/")


class ConfigError(RuntimeError):
    """The build config does not state, in a binding way, what may be shipped."""


def load_pyproject(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    if not path.is_file():
        raise ConfigError(f"no pyproject.toml at {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _entries(table: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for key in keys:
        value = table.get(key)
        if isinstance(value, list):
            out.extend(str(v) for v in value)
    return out


def selection(pyproject: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return (sdist_allowed, wheel_allowed) as top-level entry names.

    Raises ConfigError when no key with authority over the sdist exists. A
    wheel-target `packages` key is not authority over the sdist, see the module
    docstring.
    """
    build = pyproject.get("tool", {}).get("hatch", {}).get("build", {})
    if not build:
        raise ConfigError("no [tool.hatch.build] section")

    targets = build.get("targets", {})
    common = _entries(build, ("packages", "only-include", "include"))
    sdist = _entries(targets.get("sdist", {}), ("packages", "only-include", "include")) or common
    wheel = _entries(targets.get("wheel", {}), ("packages", "only-include", "include")) or common

    if not sdist:
        raise ConfigError(
            "no file selection with authority over the sdist.\n"
            "  Found only a wheel-target key, or nothing at all. Add a build-level\n"
            "  [tool.hatch.build] packages = [...] (it applies to every target), or an\n"
            "  explicit [tool.hatch.build.targets.sdist] include = [...]. Without one,\n"
            "  hatchling ships the whole project root minus .gitignore."
        )
    if not wheel:
        raise ConfigError("no file selection with authority over the wheel")

    return sdist, wheel


def top_level(entries: list[str]) -> list[str]:
    """Reduce include/packages entries to top-level names ('/pkg/sub' -> 'pkg')."""
    out: list[str] = []
    for entry in entries:
        head = entry.strip("/").split("/", 1)[0]
        if head and head not in out:
            out.append(head)
    return out


def gitcrypt_patterns() -> list[str]:
    """Return the path patterns .gitattributes hands to the git-crypt filter."""
    attributes = REPO_ROOT / ".gitattributes"
    if not attributes.is_file():
        return []

    patterns: list[str] = []
    for raw in attributes.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "filter=git-crypt" not in line:
            continue
        patterns.append(line.split()[0])
    return patterns


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitattributes path pattern into an anchored regex.

    Handles the three wildcards that matter here: `**` spans directory
    separators, `*` and `?` do not. A pattern without a slash matches at any
    depth, which is the gitattributes rule that makes `*.local` cover
    `host_vars/x/y.local` as well.
    """
    if "/" not in pattern.strip("/"):
        pattern = "**/" + pattern.lstrip("/")

    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def is_allowed(member: str, allowed: list[str], *, is_wheel: bool) -> bool:
    head = member.split("/", 1)[0]
    if head in allowed:
        return True
    if is_wheel:
        return head.endswith(".dist-info") or head.endswith(".data")
    if "/" in member:
        return False
    if member in SDIST_METADATA:
        return True
    return any(fnmatch.fnmatchcase(member, g) for g in SDIST_METADATA_GLOBS)


def artifact_members(path: Path) -> list[str]:
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            return [n for n in zf.namelist() if not n.endswith("/")]

    with tarfile.open(path, "r:*") as tf:
        return [SDIST_PREFIX_RE.sub("", m.name) for m in tf.getmembers() if m.isfile()]


def artifact_member_bytes(path: Path) -> Iterator[tuple[str, bytes]]:
    """Yield (member name, content) for every regular member up to MAX_SCAN_BYTES."""
    if path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.filename.endswith("/") and info.file_size <= MAX_SCAN_BYTES:
                    yield info.filename, zf.read(info)
        return

    with tarfile.open(path, "r:*") as tf:
        for m in tf.getmembers():
            if m.isfile() and m.size <= MAX_SCAN_BYTES:
                fh = tf.extractfile(m)
                if fh is not None:
                    yield SDIST_PREFIX_RE.sub("", m.name), fh.read()


def content_leaks(path: Path) -> list[str]:
    """Return members whose content matches a known secret pattern."""
    if not SECRET_CONTENT_RES:
        return []
    hits: list[str] = []
    for member, data in artifact_member_bytes(path):
        for label, rx in SECRET_CONTENT_RES:
            if rx.search(data):
                hits.append(f"{member}  [contains {label}]")
    return hits


def check(path: Path, allowed: list[str], protected: list[re.Pattern[str]], namespace: str | None) -> list[str]:
    """Return the offending members of one artifact (empty list means clean)."""
    is_wheel = path.name.endswith(".whl")
    namespace_init = f"{namespace}/__init__.py" if namespace else None

    secrets: list[str] = []
    namespace_hits: list[str] = []
    strays: list[str] = []
    for member in artifact_members(path):
        if any(rx.match(member) for rx in protected):
            secrets.append(f"{member}  [git-crypt protected]")
        elif member == namespace_init:
            namespace_hits.append(f"{member}  [breaks PEP 420 namespace '{namespace}']")
        elif not is_allowed(member, allowed, is_wheel=is_wheel):
            strays.append(f"{member}  [outside allowlist]")

    # git-crypt hits first: they are the actual credential leak, and the caller truncates the list.
    return secrets + content_leaks(path) + namespace_hits + strays


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify built sdist/wheel artifacts against the pyproject allowlist.")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="directory holding pyproject.toml and dist/ (default: repository root)",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="PEP 420 namespace name; an artifact containing <namespace>/__init__.py fails",
    )
    parser.add_argument("artifacts", nargs="*", type=Path, help="artifacts to check (default: <root>/dist/*)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root: Path = args.root.resolve()
    namespace: str | None = args.namespace

    artifacts: list[Path] = list(args.artifacts)
    if not artifacts:
        dist = root / "dist"
        artifacts = sorted(dist.glob("*.tar.gz")) + sorted(dist.glob("*.whl"))

    if not artifacts:
        print(f"check_dist_secrets: no artifacts to check in {root / 'dist'}", file=sys.stderr)
        return 1

    try:
        sdist_sel, wheel_sel = selection(load_pyproject(root))
    except ConfigError as exc:
        print(f"check_dist_secrets: {exc}", file=sys.stderr)
        return 1

    sdist_allowed = top_level(sdist_sel)
    wheel_allowed = top_level(wheel_sel)
    protected = [pattern_to_regex(p) for p in gitcrypt_patterns()]
    failed = False

    for path in artifacts:
        if not path.is_file():
            print(f"check_dist_secrets: missing artifact {path}", file=sys.stderr)
            failed = True
            continue

        allowed = wheel_allowed if path.name.endswith(".whl") else sdist_allowed
        offenders = check(path, allowed, protected, namespace)
        if offenders:
            failed = True
            print(f"\nFAIL {path.name}: {len(offenders)} file(s) must not be published", file=sys.stderr)
            for member in offenders[:40]:
                print(f"    {member}", file=sys.stderr)
            if len(offenders) > 40:
                print(f"    ... and {len(offenders) - 40} more", file=sys.stderr)
        else:
            suffix = f", namespace: {namespace}" if namespace else ""
            print(f"ok   {path.name}  (allowed: {', '.join(allowed)}{suffix})")

    if failed:
        print(
            "\nRefusing to publish. Fix the file selection in pyproject.toml, rebuild,\n"
            "and re-run. Never publish an artifact that fails this check: a file that\n"
            "is only protected by .gitignore or .gitattributes is NOT protected from a\n"
            "build backend.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
