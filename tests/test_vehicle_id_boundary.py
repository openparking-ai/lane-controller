"""The boundary guard: this lane is an ordinary client of Vehicle ID.

Vehicle ID is a separate system. The lane depends on its CONTRACT -- the record
shape, which is standard-library-only and is what a third party integrates
against too -- and on nothing else. This package carries its own copy of that
contract as `lane_controller.vehicle_id_contract`, so nothing here, in `src/`
or in `tests/`, imports the `vehicle_id` package at all: not its engine, not
its service, and not its contract either. The moment a module reaches for
`vehicle_id`, the lane needs that package installed to build or to test, and
the interface stops being tested by its most important user.

This file is the enforcement. A rule nobody can break is the only kind that
survives, and a guard that has never gone red is a decoration -- so the guard
below ships with planted positive controls that prove it fires.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "lane_controller"
TESTS = ROOT / "tests"

#: This package's own copy of the Vehicle ID contract. Importing it is the
#: lane's ONLY way to the record shape.
CONTRACT_COPY = "lane_controller.vehicle_id_contract"


def _imports(root: Path) -> list[tuple[str, int, str, int]]:
    """Every import under `root`, as (file, line, module, level)."""
    found: list[tuple[str, int, str, int]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.append((path.name, node.lineno, alias.name, 0))
            elif isinstance(node, ast.ImportFrom):
                found.append((path.name, node.lineno, node.module or "", node.level))
    return found


def vehicle_id_imports(root: Path) -> list[tuple[str, int, str]]:
    """Every `vehicle_id` import under `root`, as (file, line, module)."""
    # level > 0 is a relative import: `from .interfaces import ...` cannot name
    # another distribution, so it is never in scope.
    return [
        (name, line, module)
        for name, line, module, level in _imports(root)
        if level == 0 and _is_vehicle_id(module)
    ]


def contract_copy_imports(root: Path) -> list[tuple[str, int, str]]:
    """Every import of this package's contract copy under `root`."""
    return [
        (name, line, module)
        for name, line, module, level in _imports(root)
        if (level == 0 and module == CONTRACT_COPY)
        or (level == 1 and module == CONTRACT_COPY.rsplit(".", 1)[1])
    ]


def _is_vehicle_id(module: str) -> bool:
    return module == "vehicle_id" or module.startswith("vehicle_id.")


# --- the guard ------------------------------------------------------------

def test_the_lane_imports_nothing_from_vehicle_id_in_src_or_tests():
    offenders = vehicle_id_imports(PACKAGE) + vehicle_id_imports(TESTS)
    assert not offenders, (
        "the lane must reach Vehicle ID through its own copy of the contract, "
        f"over the same interface a third party uses. Imports found: {offenders}"
    )


def test_the_scan_actually_reaches_the_source():
    """The control for the test above.

    Without this, an empty result would be indistinguishable from a scanner
    that walked the wrong directory and found no files at all -- which is
    exactly how a guard passes forever while guarding nothing. The same scan
    must find the imports of the contract copy, in both trees it guards.
    """
    in_src = contract_copy_imports(PACKAGE)
    assert in_src, "no contract-copy import found in src; the scan is not reaching the package"
    assert any(name == "vehicle_id_client.py" for name, _, _ in in_src)
    in_tests = contract_copy_imports(TESTS)
    assert in_tests, "no contract-copy import found in tests; the scan is not reaching them"
    assert any(name == "test_vehicle_id_client.py" for name, _, _ in in_tests)


# --- planted positive controls: the guard must go red ---------------------

def _plant(tmp_path: Path, source: str) -> Path:
    package = tmp_path / "lane_controller"
    package.mkdir()
    (package / "planted.py").write_text(source, encoding="utf-8")
    return package


def test_the_guard_fires_on_an_engine_import(tmp_path):
    planted = _plant(tmp_path, "from vehicle_id.engine import PlateEngine\n")
    assert vehicle_id_imports(planted) == [("planted.py", 1, "vehicle_id.engine")]


def test_the_guard_fires_on_a_deep_internal_import(tmp_path):
    planted = _plant(tmp_path, "from vehicle_id.plates.recognizer import PlateRecognizer\n")
    assert vehicle_id_imports(planted) == [("planted.py", 1, "vehicle_id.plates.recognizer")]


def test_the_guard_fires_on_a_plain_import_of_the_package(tmp_path):
    # `import vehicle_id` reaches everything the package chooses to expose,
    # so it is an internal import even though it names no submodule.
    planted = _plant(tmp_path, "import vehicle_id\n")
    assert vehicle_id_imports(planted) == [("planted.py", 1, "vehicle_id")]


def test_the_guard_fires_on_the_other_packages_contract_too(tmp_path):
    # The lane carries its own copy; reaching for the package's one would make
    # the package an install-time dependency again.
    planted = _plant(tmp_path, "from vehicle_id.contract import Read\n")
    assert vehicle_id_imports(planted) == [("planted.py", 1, "vehicle_id.contract")]


def test_the_guard_does_not_fire_on_the_contract_copy(tmp_path):
    """The negative control. A guard that flags everything proves nothing about
    the thing it is supposed to permit."""
    planted = _plant(
        tmp_path,
        "from lane_controller.vehicle_id_contract import Read\n"
        "from .vehicle_id_contract import SCHEMA_VERSION\n",
    )
    assert len(contract_copy_imports(planted)) == 2, "the plant was not seen at all"
    assert vehicle_id_imports(planted) == []


def test_a_relative_import_is_never_mistaken_for_the_other_package(tmp_path):
    planted = _plant(tmp_path, "from .interfaces import Frame\n")
    assert vehicle_id_imports(planted) == []
