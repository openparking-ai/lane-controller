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
sequence runs and the tests pass on any machine, with no Vehicle ID service
running: the simulated identifier stands in for it.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
python scripts/offline_fail_control.py   # breaks the outbox, requires the tests to fail
```

## Vehicle ID

Identification is a **separate system**: [openparking-ai/vehicle-id](https://github.com/openparking-ai/vehicle-id).
This lane is an ordinary client of its contract — the same interface a third
party integrating their own system uses. There is no in-process path reserved
for us, and `tests/test_vehicle_id_boundary.py` fails if this package ever
imports anything from that one except its public contract.

```sh
# in the vehicle-id repository, on the same device or the same LAN
vehicle-id serve
```

```python
from lane_controller.vehicle_id_client import VehicleIdClient

identifier = VehicleIdClient("http://127.0.0.1:8088")
```

The engine applies its own **measured** operating threshold and returns
`answer` or `fallback`. A fallback arrives here as no identity at all, so a read
the engine would not stand behind can never match a rule — and the lane's own
threshold cannot second-guess a decision made against measured data. The engine
being unreachable is a fallback too, not a crash: there is a car at the barrier
and the lane needs an outcome it can act on.

Accuracy numbers, the licensing audit behind the recogniser and the statement
that real-plate accuracy is NOT MEASURED all live with the engine, where they
travel with the thing they describe.

**The lane does not trust the engine to be right.** The engine has no rejection
stage — it reads text out of sensor noise — so a plate that matches no rule is
refused here whatever confidence came with it. Two independent things have to
fail before a barrier opens, and that is deliberate: measured against a lane
holding a permit list, confident noise reads opened zero barriers.

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

Vehicle ID runs in its own process on the Jetson, beside this one — see
[openparking-ai/vehicle-id](https://github.com/openparking-ai/vehicle-id). The
platform server is Node/Express/Postgres — see
[openparking-ai/platform](https://github.com/openparking-ai/platform).

## Licence and contributing

AGPL-3.0-or-later — see [LICENSE](LICENSE). Contributions require a signed CLA;
see [CONTRIBUTING.md](CONTRIBUTING.md).

---

Built by 72 Knots Method by 72Knots.ai
