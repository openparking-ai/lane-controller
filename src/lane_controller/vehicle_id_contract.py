"""The contract. This module IS the product's public surface.

Everything else in this package is an implementation detail that may be
rewritten. A READ record is not: it is what a consumer integrates against,
ours and a third party's alike, and it is versioned from the first release so
that a change to it is always visible as a change.

Three properties are the whole point, and each is enforced below rather than
documented and hoped for:

  * **A field that was not MEASURED is null.** The engine never emits a value it
    did not measure. `make` is null today because no slice measures it -- not
    because the car had no make. A plausible guess in that field would be
    indistinguishable from a measurement to everything downstream.

  * **`fallback` is an answer, not an error.** A read the engine is not
    confident enough to stand behind is a successful read with
    `outcome = "fallback"`. It is returned, transported and stored exactly like
    any other. An integrator who treats it as a failure will retry it forever;
    one who treats it as an answer will do what the lane does -- ask a human.

  * **The operating threshold travels with the read.** `threshold_applied` is
    part of the record, so a consumer can see the operating point that produced
    the outcome instead of having to know it out of band. With fast-alpr, the
    plate reader, it is the per-character threshold the operator states; with
    our own recogniser, the operating point measured for its weights -- see
    `vehicle_id.engine`.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime

#: Bumped whenever the record's shape changes in a way a consumer could notice.
#: A consumer that does not recognise the version it is handed should say so
#: and stop, not guess which fields it still understands.
SCHEMA_VERSION = 1

#: The two outcomes, and there is deliberately no third. "error" is not one:
#: an engine that cannot read is an engine that returns `fallback`.
ANSWER = "answer"
FALLBACK = "fallback"
OUTCOMES = (ANSWER, FALLBACK)

#: What the plate characters say about the plate, and there is deliberately no
#: "could not identify". Every car gets an ID; the plate's part of it is one of
#: these three, and each is a fact rather than a failure:
#:
#:   * `read`: at least one character is SURE, in its position.
#:   * `unreadable`: a picture of the car was read and no character is sure --
#:     no plate found, a plate nothing could read, or frames and readers that
#:     disagreed everywhere. A fact about the car, compared like any other.
#:   * `not_seen`: no picture was read at all -- a dead camera feed, captures
#:     that would not decode. A fact about the camera, not about the car, and
#:     never to be matched against another car's `unreadable`.
PLATE_READ = "read"
PLATE_UNREADABLE = "unreadable"
PLATE_NOT_SEEN = "not_seen"
PLATE_STATES = (PLATE_READ, PLATE_UNREADABLE, PLATE_NOT_SEEN)

#: Whether the plate READERS agreed, stated rather than settled by a silent pick:
#: `agree` (every reader that read text read the same folded text), `differ`
#: (two or more readers read text and it is not the same), `single` (only one
#: reader read any text), `none` (nobody read any text).
AGREE = "agree"
DIFFER = "differ"
SINGLE = "single"
NO_READS = "none"
AGREEMENTS = (AGREE, DIFFER, SINGLE, NO_READS)


def _text(value, field_name: str) -> str:
    """A string, or a refusal that names the field.

    Every one of these used to be copied verbatim out of the request into the
    record: `captured_at: "banana"`, `camera_id: {"a": 1}`, a naive timestamp
    with no offset. All produced a 200 with `outcome: "answer"`, and a consumer
    pricing a stay from two such timestamps has no way to know.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _iso_utc(value, field_name: str) -> str:
    """An ISO 8601 timestamp that carries an offset.

    A naive timestamp is refused rather than assumed to be UTC. Assuming is how
    a lane in one timezone and a consumer in another end up disagreeing about
    when a car arrived, months later, with the money already collected.
    """
    text = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not ISO 8601: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} has no UTC offset: {text!r}")
    return text


def _unit_interval(value, field_name: str) -> float:
    """A real number within [0, 1].

    `True` is excluded explicitly. Python says `0.0 <= True <= 1.0`, so a
    `confidence` of `true` -- a field carrying no measurement at all -- used to
    reach the lane as maximum confidence and open a barrier.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite, got {value!r}")
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{field_name} must be within [0, 1], got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class Capture:
    """One image handed to the engine, plus enough provenance to argue later.

    This is the engine's INPUT, and it is part of the contract for the same
    reason the record is: a consumer has to construct it. It deliberately does
    not describe a camera, a stream or a driver -- the engine reads images and
    knows nothing about where they came from.
    """

    image_bytes: bytes
    captured_at: str
    camera_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.image_bytes, (bytes, bytearray)):
            raise ValueError("image_bytes must be bytes")
        _iso_utc(self.captured_at, "captured_at")
        _text(self.camera_id, "camera_id")

    @staticmethod
    def now(image_bytes: bytes, camera_id: str) -> Capture:
        return Capture(image_bytes=image_bytes, captured_at=utc_now(), camera_id=camera_id)


@dataclass(frozen=True, slots=True)
class PlateCharacter:
    """One position of the plate, what was read there, and whether it is SURE.

    `position` counts from 0 at the left of the plate as read. `character` is
    what the reader that set the positions read there; `folded` is its
    look-alike class under the rule named on `PlateCharacters.rule`, and it is
    the one to compare -- an 8 read for a B at the entry and at the exit is the
    same ID. `confidence` is the LOWEST any read gave this position, never the
    best: a position is only as sure as its least sure reading.
    """

    position: int
    character: str
    folded: str
    confidence: float
    sure: bool

    def __post_init__(self) -> None:
        if isinstance(self.position, bool) or not isinstance(self.position, int):
            raise ValueError(f"plate character position must be an integer, got {self.position!r}")
        if self.position < 0:
            raise ValueError(f"plate character position must be >= 0, got {self.position}")
        for name in ("character", "folded"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 1 or not value.isascii():
                raise ValueError(f"plate character {name} must be one character, got {value!r}")
            if not (value.isdigit() or value.isupper()):
                raise ValueError(f"plate character {name} must be A-Z or 0-9, got {value!r}")
        _unit_interval(self.confidence, "plate character confidence")
        if not isinstance(self.sure, bool):
            raise ValueError(f"plate character sure must be true or false, got {self.sure!r}")


@dataclass(frozen=True, slots=True)
class PlateRead:
    """One reader's read of one capture, kept beside the others.

    Every read that produced text is in the record, so that agreement and
    disagreement between frames and between readers are on the record rather
    than settled by a pick nobody can see afterwards.
    """

    reader: str
    capture: int
    text: str
    confidence: float
    model: str | None = None

    def __post_init__(self) -> None:
        _text(self.reader, "plate read reader")
        if isinstance(self.capture, bool) or not isinstance(self.capture, int) or self.capture < 0:
            raise ValueError(f"plate read capture must be an integer >= 0, got {self.capture!r}")
        text = self.text
        if not isinstance(text, str) or not text or not (text.isascii() and text.isalnum()):
            raise ValueError(
                f"plate read text must be non-empty letters and digits, got {self.text!r}"
            )
        _unit_interval(self.confidence, "plate read confidence")
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError(f"plate read model must be a string or null, got {self.model!r}")


@dataclass(frozen=True, slots=True)
class PlateCharacters:
    """The plate as positioned characters: the part of the ID a plate gives.

    `identity.plate` is beside it -- the whole registration, answered under
    `threshold_applied` (with fast-alpr, only when every position here is
    sure). This is the other question:
    WHICH characters, in which positions, are sure. Two or three sure characters
    in their positions are what pick one car out of a garage; a whole plate is
    not needed and is not waited for.

    `pattern` is the folded characters with every position that is not sure
    written `?` -- `48?YP17?` -- and empty when nothing was read. It is derived
    from `characters` and the record refuses one that disagrees with them.

    `rule` names, with its version, how the reads were folded and lined up and
    when a position counts as sure. Two IDs under different rules do not
    compare, for the reason two descriptors of different versions do not.
    """

    state: str
    rule: str
    threshold: float
    agreement: str
    pattern: str = ""
    characters: tuple[PlateCharacter, ...] = field(default_factory=tuple)
    reads: tuple[PlateRead, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.state not in PLATE_STATES:
            raise ValueError(f"plate state must be one of {PLATE_STATES}, got {self.state!r}")
        _text(self.rule, "plate characters rule")
        _unit_interval(self.threshold, "plate characters threshold")
        if self.agreement not in AGREEMENTS:
            raise ValueError(
                f"plate agreement must be one of {AGREEMENTS}, got {self.agreement!r}"
            )
        if not all(isinstance(c, PlateCharacter) for c in self.characters):
            raise ValueError("plate characters must all be PlateCharacter")
        if not all(isinstance(r, PlateRead) for r in self.reads):
            raise ValueError("plate reads must all be PlateRead")
        if [c.position for c in self.characters] != list(range(len(self.characters))):
            raise ValueError("plate character positions must run 0, 1, 2 ... with none missing")
        for c in self.characters:
            if c.sure and c.confidence < self.threshold:
                raise ValueError(
                    f"position {c.position} is sure at confidence {c.confidence}, below the "
                    f"stated threshold {self.threshold}"
                )
        if self.pattern != plate_pattern(self.characters):
            raise ValueError(
                f"plate pattern {self.pattern!r} does not match its characters "
                f"({plate_pattern(self.characters)!r})"
            )
        any_sure = any(c.sure for c in self.characters)
        if (self.state == PLATE_READ) != any_sure:
            raise ValueError(
                f"plate state {self.state!r} with {'some' if any_sure else 'no'} sure "
                "characters; `read` means at least one is sure, and only then"
            )
        if self.state == PLATE_NOT_SEEN and (self.characters or self.reads):
            raise ValueError("plate state not_seen carries characters or reads; nothing was seen")

    @property
    def sure(self) -> tuple[PlateCharacter, ...]:
        """The sure positions, which are what an exit is matched on."""
        return tuple(c for c in self.characters if c.sure)

    @staticmethod
    def from_dict(d: dict) -> PlateCharacters:
        payload = dict(d)
        payload["characters"] = tuple(
            _only_known(PlateCharacter, c) for c in payload.get("characters") or ()
        )
        payload["reads"] = tuple(_only_known(PlateRead, r) for r in payload.get("reads") or ())
        return _only_known(PlateCharacters, payload)


#: How `pattern` writes a position that is not sure.
UNSURE = "?"


def plate_pattern(characters) -> str:
    """The folded characters, `?` where a position is not sure; `""` for none."""
    return "".join(c.folded if c.sure else UNSURE for c in characters)


@dataclass(frozen=True, slots=True)
class Identity:
    """What was measured about the vehicle. Unmeasured stays null.

    An identity is a set of COMPONENTS, not a single value. A plate is one of
    them, and it is neither mandatory nor permanent -- a garage may run on plate
    alone, on appearance alone, on a QR code alone, or on any combination. The
    fields below are components; a consumer reads whichever ones are non-null
    and is told nothing about the ones that are not.

    `marks` is the one field that is a list rather than a nullable scalar, and
    empty means "none were measured", not "the vehicle had none". The
    distinction matters the day something does measure them.
    """

    plate: str | None = None
    plate_region: str | None = None
    make: str | None = None
    model: str | None = None
    color: str | None = None
    marks: tuple[str, ...] = field(default_factory=tuple)
    #: The APPEARANCE descriptor: an opaque, versioned, compact record computed
    #: from one capture. It is not an image and it is not human-readable, and
    #: this module deliberately does not parse it -- the contract carries it and
    #: `vehicle_id.opa_id` is what produces and compares it.
    #:
    #: Its type is a string for reasons that were measured rather than
    #: preferred. `Identity` is frozen, slots and hashable; `Read.__post_init__`
    #: compares one with `!=`; and the wire format is JSON. A dict or a list
    #: satisfies none of those. A string satisfies all four and survives a
    #: round trip through JSON unchanged, which a tuple does not.
    #:
    #: Null means NOT MEASURED, exactly as it does for every other component. A
    #: consumer pinned to a commit older than this field sees no descriptor and
    #: behaves precisely as it did before -- the field is additive, so
    #: `schema_version` does NOT move for it. See docs/CONTRACT.md
    #: Compatibility, and `_only_known` below, which is what makes that true.
    descriptor: str | None = None
    #: The plate as positioned characters, the sure ones marked. Additive, like
    #: `descriptor`: `plate` above keeps its meaning, and a consumer that does
    #: not know this field reads `plate` exactly as before. Null means NOT
    #: MEASURED -- only on a record that also says nothing was there.
    plate_characters: PlateCharacters | None = None
    #: The COLOUR CODE: an opaque, versioned string, never a colour name.
    #: Nobody needs the car's "real" colour, only a code that comes out the same
    #: at the entry and the exit under the same light. `color` stays null: a
    #: colour NAME is still measured by nothing. Not parsed by this contract;
    #: `vehicle_id.colour` produces and compares it.
    color_code: str | None = None

    def __post_init__(self) -> None:
        # An int plate was accepted here and then crashed inside the lane's
        # decision path on `.upper()`. A field that carries an identity has to
        # be text or absent; there is no third thing it could sensibly be.
        for name in (
            "plate", "plate_region", "make", "model", "color", "descriptor", "color_code"
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"identity.{name} must be a string or null, got {value!r}")
        if not all(isinstance(mark, str) for mark in self.marks):
            raise ValueError(f"identity.marks must all be strings, got {self.marks!r}")
        if self.plate_characters is not None and not isinstance(
            self.plate_characters, PlateCharacters
        ):
            raise ValueError(
                f"identity.plate_characters must be PlateCharacters or null, "
                f"got {self.plate_characters!r}"
            )


@dataclass(frozen=True, slots=True)
class Engine:
    """Which engine produced this, precisely enough to reproduce it.

    `weights_id` is a digest of the weights actually loaded, not a label
    somebody typed. Two reads that disagree are worth investigating only if you
    can tell whether the same model produced them.
    """

    name: str
    version: str
    weights_id: str | None = None


@dataclass(frozen=True, slots=True)
class Read:
    """One identification. The product's unit of output."""

    read_id: str
    captured_at: str
    camera_id: str
    identity: Identity
    confidence: float
    engine: Engine
    threshold_applied: float
    outcome: str
    #: Whether a VEHICLE was there, measured separately from whether it could
    #: be identified and NEVER collapsed into one number. `None` means it was
    #: not measured -- the same rule every other field obeys. A consumer that
    #: reads `presence is False` as "no transaction" and `presence is None` as
    #: "behave as you did before this field existed" is reading it correctly.
    presence: bool | None = None
    presence_confidence: float | None = None
    #: How many captures the engine was handed for this read. Additive, and
    #: present so that "one confident record" and "the batch disagreed with
    #: itself" are distinguishable after the fact rather than only in a log.
    captures_seen: int = 1
    #: A named equipment state the presence gate reported for this read --
    #: `no_signal`, `reference_not_recognised`, `scene_changed` -- or null when
    #: it reported none or there is no gate. Additive. It says what the camera
    #: was doing when this car was identified, so a dead feed is a camera fault
    #: on the car's record rather than an absence of one. Only `no_signal` is a
    #: confirmed fault; see docs/CONTRACT.md for what the other two cover.
    camera_health: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            # `True == 1` in Python, so a bare equality check accepted
            # `schema_version: true` and let the record through.
            raise ValueError(f"schema_version must be an integer, got {self.schema_version!r}")
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")
        _text(self.read_id, "read_id")
        _iso_utc(self.captured_at, "captured_at")
        _text(self.camera_id, "camera_id")
        confidence = _unit_interval(self.confidence, "confidence")
        threshold = _unit_interval(self.threshold_applied, "threshold_applied")
        if self.presence is not None and not isinstance(self.presence, bool):
            raise ValueError(f"presence must be true, false or null, got {self.presence!r}")
        if self.presence_confidence is not None:
            _unit_interval(self.presence_confidence, "presence_confidence")
        if self.camera_health is not None:
            _text(self.camera_health, "camera_health")

        # The invariant D2 exists for, and the reason presence is a separate
        # field rather than another way of saying "fallback": if nothing was
        # there, there is nothing to have identified. A record carrying a plate
        # while claiming no vehicle was present is not a bad read, it is a
        # record that contradicts itself, and something downstream would
        # believe one half of it.
        if self.presence is False and self.identity != Identity():
            raise ValueError(
                "presence is false but the identity is not empty: "
                f"{self.identity!r}. Nothing was there to identify."
            )
        if self.presence is False and self.outcome == ANSWER:
            raise ValueError("presence is false; there is nothing to stand behind")

        # The one relationship this record exists to express, and the one thing
        # that used to be unchecked: a record cannot claim the engine stood
        # behind it while carrying a confidence the stated operating point would
        # have rejected. Every combination below reached the lane and opened a
        # barrier -- `answer` at 0.86 against a threshold of 0.99, a threshold
        # of None, of NaN, of -5.
        if self.outcome == ANSWER and confidence < threshold:
            raise ValueError(
                f"outcome is {ANSWER!r} but confidence {confidence} is below the "
                f"stated threshold_applied {threshold}. A record cannot claim an "
                "operating point it did not clear."
            )

    @property
    def is_answer(self) -> bool:
        """True when the engine stands behind this read.

        Provided so that a consumer never has to compare `outcome` to a string
        it typed itself, which is how the check gets silently inverted.
        """
        return self.outcome == ANSWER

    def to_dict(self) -> dict:
        d = asdict(self)
        d["identity"]["marks"] = list(self.identity.marks)
        plate = d["identity"]["plate_characters"]
        if plate is not None:
            plate["characters"] = list(plate["characters"])
            plate["reads"] = list(plate["reads"])
        return d

    @staticmethod
    def from_dict(d: dict) -> Read:
        version = d.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {version!r}; "
                f"this build understands {SCHEMA_VERSION}. Refusing to guess "
                "which fields still mean what they used to."
            )
        identity = dict(d["identity"])
        identity["marks"] = tuple(identity.get("marks") or ())
        if isinstance(identity.get("plate_characters"), dict):
            identity["plate_characters"] = PlateCharacters.from_dict(
                identity["plate_characters"]
            )
        return Read(
            read_id=d["read_id"],
            captured_at=d["captured_at"],
            camera_id=d["camera_id"],
            identity=_only_known(Identity, identity),
            confidence=d["confidence"],
            engine=_only_known(Engine, d["engine"]),
            threshold_applied=d["threshold_applied"],
            outcome=d["outcome"],
            captures_seen=d.get("captures_seen", 1),
            presence=d.get("presence"),
            presence_confidence=d.get("presence_confidence"),
            camera_health=d.get("camera_health"),
            schema_version=d["schema_version"],
        )

    @property
    def vehicle_present(self) -> bool | None:
        """Provided so a consumer never writes `if read.presence:` by hand.

        That spelling reads `None` -- not measured -- as `False`, which would
        turn every lane without a reference view into a lane that refuses
        everybody.
        """
        return self.presence

    def redacted(self) -> Read:
        """The same record with the identity blanked, for logs and metrics.

        Keeps the shape, the outcome and the confidence; drops what identifies a
        vehicle. Nothing in this package logs a plate, and this is what it uses
        instead of hand-rolling a dict at each site.
        """
        return replace(self, identity=Identity())


def _only_known(kind, payload: dict):
    """Build `kind` from `payload`, dropping fields this build does not know.

    The contract promises that additive changes do not bump `schema_version`
    and that a consumer should ignore fields it does not recognise. This is the
    reference parser, so it has to actually do that -- and nested objects are
    where it matters most, because `identity` is exactly where the next slice
    adds `body_type` and the rest.

    Refusing an unknown field here would mean every consumer on this parser
    breaks on the first additive change, which is the opposite of what
    versioning it from day one was for.
    """
    known = {f.name for f in fields(kind)}
    return kind(**{k: v for k, v in payload.items() if k in known})


def utc_now() -> str:
    """UTC, ISO 8601, with the offset present.

    A naive timestamp is the bug that surfaces months later, when a lane in one
    timezone and a consumer in another disagree about when a car arrived.
    """
    return datetime.now(UTC).isoformat()


def new_read_id() -> str:
    return uuid.uuid4().hex
