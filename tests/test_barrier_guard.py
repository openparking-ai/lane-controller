"""The barrier guard: nothing in this package can command a barrier DOWN.

The safety case for this lane is that the boom lowers on the barrier's own
closing loop, wired to the barrier and never to us. A controller that could
close a barrier is a controller that can close one on a vehicle, and the READMEs
have said for weeks that the case rests on that being IMPOSSIBLE rather than on
anybody being careful.

Until now nothing tested it. It was true by inspection, which is the form §6
calls out: an ordering constraint that is remembered rather than encoded holds
until the day somebody adds a method in good faith. A single `close()` on a
relay implementation, or a second action on the one interface that reaches the
barrier, would pass every other test in this suite.

EVERY LIST HERE IS DERIVED, none is typed. The implementations are found by
walking the package; the protocol's method set is read off the protocol; the
calls made on the relay are read out of the source with an AST. A hard-coded
list of classes to check could not notice a class somebody added, which is the
only case that matters.

Nothing about behaviour changes. This test asserts a shape.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import lane_controller
from lane_controller.interfaces import VendOutput

#: Verbs that name a barrier going DOWN. Not a list of things to check -- the
#: checks below derive what they cover -- but the vocabulary a new method would
#: use if somebody added one to the controller.
LOWERING_VERBS = frozenset({"close", "lower", "drop", "shut", "descend", "retract"})


def _package_modules() -> dict[str, object]:
    """Every module in the installed package, found by walking it."""
    modules = {}
    for info in pkgutil.iter_modules(lane_controller.__path__):
        modules[info.name] = importlib.import_module(f"lane_controller.{info.name}")
    assert modules, "walked the package and found no modules -- this test is measuring nothing"
    return modules


def package_sources() -> dict[str, str]:
    """The source of every module in the package, keyed by name.

    A function rather than a constant because the fail-control replaces it: the
    plant has to reach the thing being read, or the control proves nothing about
    the check that reads it.
    """
    return {name: inspect.getsource(module) for name, module in _package_modules().items()}


def _vend_output_implementations() -> dict[str, type]:
    """Every class in the package that implements VendOutput, found by query."""
    found = {}
    for module_name, module in _package_modules().items():
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj is VendOutput:
                continue
            # Defined here, not imported from somewhere already walked.
            if obj.__module__ != module.__name__:
                continue
            if callable(getattr(obj, "vend", None)):
                found[f"{module_name}.{name}"] = obj
    return found


def _public_methods(cls: type) -> set[str]:
    return {
        name
        for name, member in vars(cls).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def test_the_relay_interface_declares_exactly_one_action():
    """The protocol is the only route from this package to the barrier."""
    declared = _public_methods(VendOutput)
    assert declared == {"vend"}, (
        f"VendOutput declares {sorted(declared)}. One action reaches the barrier and it opens it; "
        "a second one is a way to command the boom, whatever it is called."
    )


def test_no_relay_implementation_has_a_second_action():
    """And no implementation may have more than the protocol does."""
    implementations = _vend_output_implementations()
    assert implementations, "found no VendOutput implementation -- the query is not measuring"
    for name, cls in implementations.items():
        assert _public_methods(cls) == {"vend"}, (
            f"{name} exposes {sorted(_public_methods(cls))}. A relay this package holds must be "
            "able to do exactly one thing."
        )


def test_nothing_in_the_package_calls_anything_but_vend_on_the_relay():
    """Read out of the source, so a method that exists elsewhere is not enough.

    `self.vend` is the controller's VendOutput. Every attribute taken off it
    anywhere in the package is collected here, and there is one.
    """
    called: dict[str, str] = {}
    for module_name, source in package_sources().items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Attribute):
                continue
            inner = node.value
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "vend"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "self"
            ):
                called[node.attr] = f"{module_name}:{node.lineno}"
    assert called, "found no call on the relay at all -- the AST query is not measuring"
    assert set(called) == {"vend"}, (
        f"the relay is asked to do {sorted(called)} ({called}). It may be asked to vend, and "
        "nothing else: the barrier lowers on its own loop."
    )


def test_the_controller_has_no_method_that_names_a_barrier_going_down():
    """The controller itself, by name, because that is where one would be added."""
    from lane_controller.controller import LaneController

    offenders = {
        name: sorted(LOWERING_VERBS.intersection(name.split("_")))
        for name in vars(LaneController)
        if LOWERING_VERBS.intersection(name.split("_"))
    }
    assert not offenders, (
        f"LaneController has {offenders}. Whatever it does, a method on the controller named for "
        "a barrier going down is the thing the safety case says cannot exist here."
    )
