"""US plate layout templates, Florida first.

These are APPROXIMATIONS of real layouts, not reproductions. They exist to give
the generator a realistic distribution of character counts, groupings and
furniture (state name, county strip, slogan) so a recogniser trained on them is
not learning a single rigid shape.

Two honest limitations, recorded here rather than discovered later:

  * The FONTS are wrong. Real US plates use specific embossing typefaces which
    we neither have nor could redistribute. The generator uses OpenCV's built-in
    Hershey fonts. This is the single largest domain gap between synthetic and
    real plates, and it is why real-plate accuracy stays unmeasured until the
    physical bench exists.
  * The COLOURS and furniture are stylised. Enough to vary the background, not
    enough to be a facsimile of any state's plate.

No real plate, real vehicle or real registration appears anywhere here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 'L' = letter, 'N' = digit, ' ' = a visible gap
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # I and O omitted: most states skip them
DIGITS = "0123456789"


@dataclass(frozen=True, slots=True)
class PlateTemplate:
    state: str
    patterns: tuple[str, ...]
    top_text: str = ""
    bottom_text: str = ""
    #: BGR, because OpenCV
    background: tuple[int, int, int] = (245, 245, 245)
    ink: tuple[int, int, int] = (35, 35, 45)
    #: Relative frequency when sampling. Florida is weighted up deliberately.
    weight: float = 1.0


TEMPLATES: tuple[PlateTemplate, ...] = (
    PlateTemplate(
        state="FL",
        patterns=("LLL LNN", "LNN NLL", "NNL LNN"),
        top_text="FLORIDA",
        bottom_text="MYFLORIDA.COM",
        background=(250, 250, 250),
        ink=(60, 45, 40),
        weight=6.0,
    ),
    PlateTemplate(
        state="GA",
        patterns=("LLL NNNN", "LLLL NNN"),
        top_text="GEORGIA",
        background=(248, 246, 240),
        weight=1.0,
    ),
    PlateTemplate(
        state="NY",
        patterns=("LLL NNNN",),
        top_text="NEW YORK",
        background=(252, 248, 232),
        ink=(40, 55, 95),
        weight=1.0,
    ),
    PlateTemplate(
        state="TX",
        patterns=("LLL NNNN", "NNN LLLL"),
        top_text="TEXAS",
        background=(250, 250, 250),
        weight=1.0,
    ),
    PlateTemplate(
        state="CA",
        patterns=("NLLL NNN",),
        top_text="CALIFORNIA",
        background=(252, 252, 248),
        ink=(30, 45, 90),
        weight=1.0,
    ),
)


def charset() -> str:
    """Every character a plate can contain, plus CTC blank handled separately."""
    return LETTERS + DIGITS


@dataclass(frozen=True, slots=True)
class GeneratedPlate:
    text: str
    state: str
    template: PlateTemplate = field(repr=False)
