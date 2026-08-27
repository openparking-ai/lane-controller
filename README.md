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

## Talking to the platform

The lane syncs its rules from the platform and reports back to it, and does
neither on the path between a car arriving and a barrier opening.

| | |
|---|---|
| `sync_rules()` | pulls the garage's rules into the local cache. On failure it keeps the cache it had — a failed request is not a reason to forget good rules. |
| `EventQueue` | one outbox holding both the activity log and the session actions, so a lane that was offline replays what happened in the order it happened. **Session actions are never dropped**; only log events are, and a drop is counted. |
| `PlatformTransport` | delivers the outbox. All-or-nothing, because every platform endpoint it calls is idempotent. |

**Session times come from the lane, never the server.** A car arrives when it
arrives, whatever time the platform eventually hears about it; pricing a stay by
when the network came back would be wrong.

**At an exit, the lane names the session it is closing** when the platform is
reachable, so a queued close cannot land on a later visit. Offline it closes on
the plate alone, which works and is merely less precise.

**Every queued item carries an `event_id` generated on the lane.** That is what
makes reconnecting safe: the queue re-sends everything it could not confirm, and
the platform deduplicates. Without it, "flush the queue again" means "bill the
customer again".

An item the platform *refuses* — a 4xx, meaning it understood and said no — is
dropped rather than retried forever, because retrying poison blocks everything
behind it. Dropped items are counted and logged, never silent: a dropped session
is a gap in the money record.

## Watch a car drive through

From the [platform](https://github.com/openparking-ai/platform) repository:

```sh
npm run demo
```

then here:

```sh
python -m lane_controller.demo --credentials ../platform/.demo-credentials.json
```

```
  [entry lane] a car arms the loop
    identified   SIM-4271  confidence 0.97
    decision     ALLOW
    gate         VENDED   — the barrier will close on its own loop
  [exit lane]  3.5 hours later, the same car arrives
    FEE          10.00 USD
```

Pass `--confidence 0.40` to watch the lane refuse to guess instead.

## Runs with no hardware

Three interfaces — `LoopInput`, `CameraFeed`, `VendOutput` — plus a
`VehicleIdentifier`, each with a fully simulated implementation. The complete
sequence runs and the tests pass on any machine.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
python scripts/offline_fail_control.py   # breaks the outbox, requires the tests to fail
```

## Vehicle ID

Plate reading is real; make/model, colour and appearance are the next slice and
their fields stay `None` rather than being invented.

The recogniser is **ours** — a small CRNN+CTC trained on generated plates. That
was a licensing decision before it was a technical one: no public US plate-reading
dataset permits commercial use (see [docs/EVAL_DATA.md](docs/EVAL_DATA.md)), and
every third-party model we checked ships permissive *code* with **no stated licence
for its weights**. Generating the training data removes both problems and makes the
weights our own asset.

```sh
pip install -e '.[dev,vision]'
python -m lane_controller.vision.plates.train      # weights are NOT committed
python scripts/eval_plates.py                      # every number we quote
```

Measured on synthetic plates, 200 per rung across a 10-rung degradation ladder:

| | rungs 0–7 exact | rung 9 exact | ms/plate |
|---|---|---|---|
| ours (CRNN+CTC) | 98.0–100% | 68.5% | **4.1** (CPU) |
| baseline (RapidOCR PP-OCRv3) | 10.5–98.5% | 50.0% | 208.7 |

**These are synthetic numbers and are not a prediction of real-world accuracy** —
the generator uses OpenCV fonts, not embossing typefaces. The harness prints
real-plate accuracy as NOT MEASURABLE, and it stays that way until the physical
bench exists.

**The recogniser is accurate and overconfident**, which matters more than the
accuracy: mean confidence barely moves across the ladder while accuracy falls.
The operating threshold is therefore measured, not chosen — **0.99**, giving
0.87% wrong-but-answered. A lane running this engine must use that value, not
the lane's own 0.85 default.

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

Built by 72 Knots Method by 72Knots.ai
