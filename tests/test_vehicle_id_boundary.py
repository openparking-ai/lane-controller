"""The boundary guard: this lane is an ordinary client of Vehicle ID.

Vehicle ID is a separate system. The lane may depend on its CONTRACT -- the
record shape, which is standard-library-only and is what a third party
integrates against too -- and on nothing else. The moment a module here reaches
for `vehicle_id.engine`, `vehicle_id.plates` or anything else inside that
package, the lane has a private path that no third party has, and the interface
stops being tested by its most important user.

This file is the enforcement. A rule nobody can break is the only kind that
survives, and a guard that has never gone red is a decoration -- so the guard
below ships with planted positive controls that prove it fires.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "lane_controller"

#: The one module of Vehicle ID this lane is allowed to import. It is the
#: public contract: no torch, no OpenCV, no engine, nothing a third party
#: integrating over HTTP does not also have.
ALLOWED = "vehicle_id.contract"


def vehicle_id_imports(root: Path) -> list[tuple[str, int, str]]:
    """Every `vehicle_id` import under `root`, as (file, line, module)."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_vehicle_id(alias.name):
                        found.append((path.name, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative import: `from .interfaces import ...`
                # cannot name another distribution, so it is never in scope.
                if node.level == 0 and node.module and _is_vehicle_id(node.module):
                    found.append((path.name, node.lineno, node.module))
    return found


def _is_vehicle_id(module: str) -> bool:
    return module == "vehicle_id" or module.startswith("vehicle_id.")


def internals(imports) -> list[tuple[str, int, str]]:
    return [entry for entry in imports if entry[2] != ALLOWED]


# --- the guard ------------------------------------------------------------

def test_the_lane_imports_the_contract_and_nothing_else_from_vehicle_id():
    offenders = internals(vehicle_id_imports(PACKAGE))
    assert not offenders, (
        "the lane must reach Vehicle ID through its contract, over the same "
        f"interface a third party uses. Internal imports found: {offenders}"
    )


def test_the_scan_actually_reaches_the_source():
    """The control for the test above.

    Without this, an empty result would be indistinguishable from a scanner
    that walked the wrong directory and found no files at all -- which is
    exactly how a guard passes forever while guarding nothing.
    """
    imports = vehicle_id_imports(PACKAGE)
    assert imports, "no vehicle_id import found anywhere; the scan is not reaching the package"
    assert any(name == "vehicle_id_client.py" for name, _, _ in imports)


# --- planted positive controls: the guard must go red ---------------------

def _plant(tmp_path: Path, source: str) -> Path:
    package = tmp_path / "lane_controller"
    package.mkdir()
    (package / "planted.py").write_text(source, encoding="utf-8")
    return package


def test_the_guard_fires_on_an_engine_import(tmp_path):
    planted = _plant(tmp_path, "from vehicle_id.engine import PlateEngine\n")
    offenders = internals(vehicle_id_imports(planted))
    assert offenders == [("planted.py", 1, "vehicle_id.engine")]


def test_the_guard_fires_on_a_deep_internal_import(tmp_path):
    planted = _plant(tmp_path, "from vehicle_id.plates.recognizer import PlateRecognizer\n")
    assert internals(vehicle_id_imports(planted)) == [
        ("planted.py", 1, "vehicle_id.plates.recognizer")
    ]


def test_the_guard_fires_on_a_plain_import_of_the_package(tmp_path):
    # `import vehicle_id` reaches everything the package chooses to expose,
    # so it is an internal import even though it names no submodule.
    planted = _plant(tmp_path, "import vehicle_id\n")
    assert internals(vehicle_id_imports(planted)) == [("planted.py", 1, "vehicle_id")]


def test_the_guard_does_not_fire_on_the_contract(tmp_path):
    """The negative control. A guard that flags everything proves nothing about
    the thing it is supposed to permit."""
    planted = _plant(tmp_path, "from vehicle_id.contract import Read\n")
    assert vehicle_id_imports(planted), "the plant was not seen at all"
    assert internals(vehicle_id_imports(planted)) == []


def test_a_relative_import_is_never_mistaken_for_the_other_package(tmp_path):
    planted = _plant(tmp_path, "from .interfaces import Frame\n")
    assert vehicle_id_imports(planted) == []
