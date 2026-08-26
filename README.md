# Open Parking AI — lane controller

The software that runs one lane: an arming loop reports a vehicle, cameras are
read, the vehicle is identified, a decision is made from rules already on the
box, and the vend relay asks the barrier to open.

```
arming loop ─▶ grab frames ─▶ identify ─▶ decide ─▶ vend
                                            │
                                            └─▶ fallback (named, logged, never a guess)
```

Then nothing. **The barrier closes itself on its own closing loop.** That loop is
wired to the barrier and never to this controller, and there is no `close()`
anywhere in this package. A controller that could close a barrier is a
controller that could close one on a vehicle; the safety case rests on that
being impossible rather than on us being careful.

## Two properties worth stating plainly

**It works with the internet down.** Every decision is made from a local cache
of allow and pricing rules. The server refreshes that cache and receives events
afterwards; the barrier never waits on a network call. If the rules go stale
past their configured age, the lane falls back rather than acting on pricing it
no longer trusts.

**It is never wrong silently.** When identification is not confident enough, the
lane takes an explicit fallback path — `LOW_CONFIDENCE`, `NO_PLATE_READ`,
`UNKNOWN_VEHICLE` or `STALE_RULES` — each of which is a named outcome with an
event behind it. It does not pick the most likely plate and open the gate.
Confidence is checked *before* the plate is used to look anything up, so a
low-confidence read cannot match a rule by accident.

## Runs with no hardware

Three interfaces — `LoopInput`, `CameraFeed`, `VendOutput` — plus a
`VehicleIdentifier`, each with a fully simulated implementation. The complete
sequence runs and the tests pass on any machine.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

## Reference hardware

None of it is required to run this package, and none of it has been purchased yet.

| | |
|---|---|
| Controller | Seeed reComputer Industrial J3011 (Jetson Orin NX), in the gate housing |
| Barrier | Q-SAQ, driven by a dry-contact vend relay |
| Detection | Inductive arming loop; the barrier's own closing loop |
| Cameras — default | Reolink RLC-810A |
| Cameras — upper tiers | Axis P1465-LE, Hanwha XNO-9082R |

RTSP and PoE are required for any camera.

Vision runs in Python on the Jetson. The platform server is Node/Express/Postgres —
see [openparking-ai/platform](https://github.com/openparking-ai/platform).

## Licence and contributing

AGPL-3.0-or-later — see [LICENSE](LICENSE). Contributions require a signed CLA;
see [CONTRIBUTING.md](CONTRIBUTING.md).

---

Built by 72 Knots. Method by [72Knots.ai](https://72knots.ai)
