# Open Parking AI — lane controller

The software that runs one lane: the arming loops report a vehicle, cameras are
read, the vehicle is identified, a decision is made from rules already on the
box, the vend relay asks the barrier to open, and the loops after the barrier
say whether a vehicle actually went through.

```
arming loops ─▶ grab frames ─▶ identify ─▶ decide ─▶ vend ─▶ closing loops
                                             │                    │
                                             │                    ├─▶ A→B  confirmed  → session
                                             │                    ├─▶ B→A  backed out → nothing
                                             │                    └─▶ none held       → flagged
                                             └─▶ fallback (named, logged, never a guess)
```

**The ticket is not the entry.** A driver can pull up, take a ticket and drive
away; a vend with nothing behind it is not an arrival. So the vend creates a
PENDING entry, and two loops after the barrier, crossed in order inside the
confirmation window, promote it to a session.
An entry that is never confirmed is HELD and
flagged — never voided silently, and never turned into a session.

**It narrows the fraud rather than closing it**: a ticket with no car becomes a
ticket with *a* car, because nothing here binds the crossing to the vehicle that
took the ticket, and that binding is unbuilt.

**An EXIT is the other way round.** The vend there is the payment moment and the
barrier opened, so an exit the loops did not confirm still closes the session and
bills the stay — marked `held`, with an `exit_held` event for a human to look at.
Holding it open instead would leave the stay unbilled and the vehicle counted as
inside for ever, which would make installing the loops worse than not.

**The barrier still closes itself on its own closing loop.** That loop is wired
to the barrier and never to this controller, the confirmation loops above are a
different pair that this package reads and never drives, and there is no
`close()` anywhere in this package. A controller that could close a barrier is a
controller that could close one on a vehicle; the safety case rests on that
being impossible rather than on us being careful.

**Every loop count, spacing and window is a per-site setting** in `[loops]`, and
every one of them is an ASSUMPTION until a site measures it — nothing here
measures a spacing. A site with one arming loop and no
closing loops runs exactly as it always did; what it does not get is named in
the record on every vehicle rather than described in a document.

## Two properties worth stating plainly

**It works with the internet down.** Every decision is made from a local cache
of allow and pricing rules. The server refreshes that cache and receives events
afterwards; the barrier never waits on a network call. If the rules go stale
past their configured age, the lane falls back rather than acting on pricing it
no longer trusts.

When identification is not confident enough, the
lane takes an explicit fallback path — the `Fallback` members in
`decision.py` — each of which is a named outcome with an
event behind it. It does not pick the most likely plate and open the gate.
Confidence is checked *before* the plate is used to look anything up, so a
low-confidence read cannot match a rule by accident.

## The lane's contract

Everything outside this process reads the lane through one versioned contract —
[docs/CONTRACT.md](docs/CONTRACT.md). Our own intercom agent will be an ordinary
client of it, and a lane that is not ours can take the same seat:
`tests/third_party_lane/` is a minimal one, and the same consumer code reads
both.

```sh
lane-controller serve --config lane.toml
```

Loopback by default; off loopback it refuses to start without **both** tokens.

**Four reads and one act.** Contract version 2 adds `POST /v1/lane/vend` — the
assisted vend, and the only route here that changes anything. It completes a
decision this lane already made and could not finish on its own: a driver whose
plate was not read, at a barrier that did not open, with a human on the intercom
or a confirmed display code saying to let them in.

What makes that safe is enforced, not promised:

- **The lane applies its own refusals** — presence read off the arming loop at
  the moment of the call, its own malfunction table, its own arming geometry,
  and the case must be one this lane is holding and is still recent. All of them
  come from the code an ordinary arrival goes through.
- **The lane writes the completed identity before the relay moves**, in that
  order, asserted by a test that records every call.
- **A read token never authorises an act.** The vend takes a second token from
  `--act-token-file`; each is `403` on the other's routes, and either in a query
  string is `401`.
- **The answer is `vend_commanded`, never `opened`.** Nothing in this system
  measures the boom, and this lane's own health surface says so.

`capabilities.can_vend` says whether a given lane serves it, derived from the
route table and the lane's direction — an exit lane answers `false`, because
completing an identity there would close a stay and freeze a fee.

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

Four interfaces — `LoopInput`, `ClosingLoops`, `CameraFeed`, `VendOutput` — plus
a `VehicleIdentifier`, each with a fully simulated implementation. The complete
sequence runs and the tests pass on any machine, with no Vehicle ID service
running: the simulated identifier stands in for it.

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
python scripts/offline_fail_control.py       # breaks the outbox, requires the tests to fail
python scripts/confirmation_fail_control.py  # breaks the confirmation, same requirement
python scripts/contract_fail_control.py      # breaks the read contract, same requirement
```

## Vehicle ID

Identification is a **separate system**: [openparking-ai/vehicle-id](https://github.com/openparking-ai/vehicle-id).
This lane is an ordinary client of its contract — the same interface a third
party integrating their own system uses. There is no in-process path reserved
for us, and `tests/test_vehicle_id_boundary.py` fails if this package ever
imports anything from that one except its public contract.

```sh
# in the vehicle-id repository, on the same device
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

**The second gate is CONFIGURATION, not a property of this code.** The platform
serves `plate_rules: []` and the garage's `default_action`, whose column default
is `allow`, so a lane is refusing nothing here until an operator has asked it
to.

## Reference hardware

None of it is required to run this package, and none of it has been purchased yet.

| | |
|---|---|
| Controller | Seeed reComputer Industrial J3011 (Jetson Orin NX), in the gate housing |
| Barrier | Q-SAQ, driven by a dry-contact vend relay |
| Detection | Inductive arming loops before the barrier, and two confirmation loops after it; plus the barrier's own closing loop, which is wired to the barrier |
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
