import importlib
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_FILES = sorted((REPO_ROOT / "packaging").glob("*.toml"))


def test_no_namespace_init() -> None:
    assert not (REPO_ROOT / "somestuff" / "__init__.py").exists()


def test_somestuff_is_pep420_namespace() -> None:
    import somestuff

    assert somestuff.__file__ is None
    assert str(REPO_ROOT / "somestuff") in [str(Path(p).resolve()) for p in somestuff.__path__]


def _script_targets() -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    for toml_path in PACKAGING_FILES:
        with toml_path.open("rb") as fh:
            scripts = tomllib.load(fh)["project"].get("scripts", {})
        for name, target in scripts.items():
            targets.append((toml_path.stem, name, target))
    return targets


@pytest.mark.parametrize("dist,script,target", _script_targets(), ids=lambda v: v if ":" not in str(v) else str(v))
def test_console_script_targets_resolve(dist: str, script: str, target: str) -> None:
    module_name, attr = target.split(":")
    assert module_name.startswith(f"somestuff.{dist}.")
    obj = getattr(importlib.import_module(module_name), attr)
    assert callable(obj)


def test_packaging_files_present() -> None:
    assert len(PACKAGING_FILES) == 7
