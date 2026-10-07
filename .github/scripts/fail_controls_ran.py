"""Every fail-control ran on this interpreter, exactly once, and passed.

The fail-controls used to be steps of `test`, one after another, and a step that
did not run was visible as a step that did not run. They are their own jobs now,
so this is what keeps that true: it reads the one-line report each job leaves
(`<control> exit=<status>`, in `<control>-<job index>.txt`) and fails, naming
each, on

  * a script in `scripts/*_fail_control.py` with no report -- left out of the
    matrix, killed, cancelled or never scheduled;
  * a script with more than one report -- listed twice;
  * a report for a script that does not exist;
  * a report whose exit status is not 0.

The list of controls is the scripts on disk and not the workflow's matrix, so the
two cannot agree by being edited together.

    python .github/scripts/fail_controls_ran.py <directory>
    python .github/scripts/fail_controls_ran.py --self-test
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_LINE = re.compile(r"^([a-z0-9_]+) exit=(\d+)$")


def controls(root: Path = ROOT) -> list[str]:
    return sorted(p.name.removesuffix("_fail_control.py")
                  for p in (root / "scripts").glob("*_fail_control.py"))


def problems(directory: Path, expected: list[str]) -> list[str]:
    found: dict[str, list[int]] = {}
    for where in sorted(directory.glob("*.txt")) if directory.is_dir() else []:
        for line in where.read_text(encoding="utf-8").splitlines():
            match = _LINE.fullmatch(line.strip())
            if match:
                found.setdefault(match[1], []).append(int(match[2]))
    out = []
    for name in expected:
        exits = found.get(name, [])
        if not exits:
            out.append(f"{name}: no report — it never ran, or never finished")
        elif len(exits) > 1:
            out.append(f"{name}: ran {len(exits)} times")
        elif exits != [0]:
            out.append(f"{name}: exit {exits[0]}")
    for name in sorted(set(found) - set(expected)):
        out.append(f"{name}: reported, and there is no scripts/{name}_fail_control.py")
    return out


def self_test() -> None:
    """Each refusal above, planted, must be refused and named."""
    expected = ["alpha", "beta", "gamma"]
    cases = {
        "whole": ({"alpha-0": "alpha exit=0", "beta-1": "beta exit=0", "gamma-2": "gamma exit=0"},
                  None),
        "missing": ({"alpha-0": "alpha exit=0", "gamma-2": "gamma exit=0"}, "beta"),
        "twice": ({"alpha-0": "alpha exit=0", "beta-1": "beta exit=0", "beta-3": "beta exit=0",
                   "gamma-2": "gamma exit=0"}, "beta"),
        "red": ({"alpha-0": "alpha exit=0", "beta-1": "beta exit=1", "gamma-2": "gamma exit=0"},
                "beta"),
        "unknown": ({"alpha-0": "alpha exit=0", "beta-1": "beta exit=0", "gamma-2": "gamma exit=0",
                     "delta-3": "delta exit=0"}, "delta"),
    }
    for case, (files, named) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            for stem, text in files.items():
                (Path(tmp) / f"{stem}.txt").write_text(text + "\n", encoding="utf-8")
            got = problems(Path(tmp), expected)
        if named is None:
            assert got == [], f"self-test {case}: {got}"
        else:
            assert len(got) == 1 and got[0].startswith(f"{named}:"), f"self-test {case}: {got}"
    assert controls(), "no scripts/*_fail_control.py found: the list this checks against is empty"
    print(f"self-test OK — {len(cases)} cases; {len(controls())} controls on disk")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    if len(sys.argv) != 2:
        sys.exit("usage: fail_controls_ran.py <directory> | --self-test")
    expected = controls()
    found = problems(Path(sys.argv[1]), expected)
    for one in found:
        print(f"*** {one} ***", file=sys.stderr)
    if found:
        sys.exit(1)
    print(f"{len(expected)} fail-controls, each ran exactly once and passed")
