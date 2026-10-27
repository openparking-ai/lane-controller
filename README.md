# Open Parking AI — lane controller

The software that runs one lane: the arming loops report a vehicle, cameras are
read, the vehicle is identified, a decision is made from rules already on the
box, the vend relay asks the barrier to open, and the loops after the barrier
say whether a vehicle actually went through.

```
arming loops ─▶ grab frames ─▶ identify ─▶ decide ─▶ vend ─▶ closing loop(s)
                                             │                    │
                                             │                    ├─▶ occupied (1 loop) confirmed  → session
                                             │                    ├─▶ A→B (2 loops)     confirmed  → session
                                             │                    ├─▶ B→A (2 loops)     backed out → nothing
                                             │                    └─▶ none              held       → flagged
                                             └─▶ fallback (named, logged, never a guess)

          (idle) ─────────────────────────────────────────────▶ closing loop(s)
                                                                    └─▶ occupied / A→B with nothing pending → entry_unadmitted
                                                                                                             + session (tailgater)
```

**The entry, as the owner set it (2026-10-26).** The activation loop, the
closing loop and the pictures make the entry; the barrier plays no part in the
record. So **every car that crosses the arming loop and then the closing loop
is a stay**, with or without the lane's open command, and the car is
identified by what was seen: the plate when it was read at the lane's
confidence, or `plate_unreadable` -- a value of the car's identity, never a
reason to leave it out of the record. **A garage that takes transients opens
for every car** (`transient_available: true`): a plate read low, no plate, an
unknown car, an engine that did not answer and rules past their age all open,
and the stay carries what was seen. Only the garage full (`closed: full`, which
still lets in a car a register covers), the owner's closing to everyone, a car
on the banned list, an owner's rule against a plate, and nothing there keep the
gate shut. **A monthly/pass-only garage** (`transient_available: false`) opens
for a car its registers cover today and **denies every other car**, the gate
shut -- not a person's case -- whatever its default. A garage that has not said
which it is decides as it always did. `tests/test_entry_always_a_stay.py` holds
each, and `scripts/entry_stay_fail_control.py` breaks each.

**The ticket is not the entry.** A driver can pull up, take a ticket and drive
away; a vend with nothing behind it is not an arrival. So the vend creates a
PENDING entry, and the loop after the barrier promotes it to a session when it
reads a vehicle inside the confirmation window.
An entry that is never confirmed is HELD and
flagged — never voided silently, and never turned into a session.

**One closing loop is the standard installation**, and it is the loop every
barrier lane already has: the barrier's own closing loop, under the arm, which
the installer drawings show. It stays wired to the barrier, which closes on it by
itself. Its loop detector's second output goes to an input on the lane's control
panel, and the lane only reads that input (`closing_loops = 1`). Occupied after
the vend, inside the window, is `confirmed`, at an entry and at an exit alike.
**What one loop does not tell:** which way the car then went. A car that reaches
the loop and backs out is not seen; its stay simply has no exit, as any open
stay. A site that wants that seen installs two confirmation loops after the gate
(`closing_loops = 2`): crossed A then B they confirm, B then A is a car backing
out. Every event carries the geometry it relied on, `closing_loops` included.

**It narrows the fraud rather than closing it**: a ticket with no car becomes a
ticket with *a* car, because nothing here binds the crossing to the vehicle that
took the ticket, and that binding is unbuilt.

**A crossing with nothing pending is recorded too.** The three answers above are
what becomes of a vend, and the loops after the barrier used to be read only
after one — so a car the lane refused that drove in anyway, a car following the
one admitted, and every car through a barrier that is simply up were in no
record at all. The lane now polls those loops on every idle turn, and the closing
loop occupied (or, with two loops, a forward crossing) found with nothing pending
is an `entry_unadmitted` event: an entry,
because a vehicle is inside; a different one, because nothing admitted it. It
moves no transit state and is never folded into `entry_confirmed`. A stream of
them on `GET /v1/lane/events` is what a broken boom looks like from here; what
broke it is not measured in this package. **And the car is a stay**: a
`session_open` follows, `confirmed` (the loops saw it) and `tailgater: true` (no
open command let it in), with its closing-loop pictures and the identity read
from them, a plate or `plate_unreadable`; the event's `stay_opened` says so. The
one crossing that opens nothing is one the camera says no car made
(`presence: false` against the lane's reference view). A held vend -- an open
command with nothing crossing in time -- is still no stay.

**Pictures, the tailgater, and banned cars (U7f).**
- **Pictures.** The arming read's own frames -- the ones the car was identified
  from, where its colour, shape and damage were seen -- and a few between the
  vend and the closing loop, and a few at it, from the lane's camera. They are
  kept with the entry record: the stay's open (at a lane with no closing loop,
  the arming read's are its pictures), or the `entry_unadmitted` record of a
  crossing no car made. They go to the platform on their own route, `POST
  /api/v1/lane/pictures`, one at a time, each retried until it is taken, and
  never onto `GET /v1/lane/events`, because a picture shows the plate. **With an
  outbox file they are kept on disk** beside the stays they belong to (0600, in
  the cache's 0700 directory), not bounded and not dropped, so a restart in an
  outage loses none; a memory-only lane keeps the newest 200. The platform keeps
  them one week after the car leaves. A held entry keeps none.
- **The tailgater.** A crossing found by the idle poll is pictured and its car
  identified, and it is a stay with `tailgater: true`, which pays at the exit --
  read or not: a car nobody could read is a stay with `plate_unreadable`. The
  `entry_unadmitted` event is recorded beside it, `identified` saying whether
  the plate was read. The car this lane admitted and held, reaching the loop
  late -- with the same plate, or with a plate nobody could compare -- is a stay
  but not a tailgater, once per held vend.
- **Banned cars.** The platform sends the bans that reach this garage on
  `GET /lane/rules` as `banned: [{id, descriptor}]`. A car the lane would admit
  whose descriptor matches one is refused instead: no vend, reason `banned`,
  pictures kept with the `decision` event. The comparison is the identity
  service's `POST /v1/searches`, as the platform's exit search asks it, with the
  thresholds the site states in `[lane] banned_match_structure` and
  `banned_match_colour`. Without those thresholds, or with a search that fails,
  no car is refused as banned. The driver who presses help hears the line every
  refusal gets.

**An EXIT is the other way round.** The vend there is the payment moment and the
barrier opened, so an exit the loops did not confirm still closes the session and
bills the stay — marked `held`, with an `exit_held` event for a human to look at.
Holding it open instead would leave the stay unbilled and the vehicle counted as
inside for ever, which would make installing the loops worse than not.

**The barrier still closes itself on its own closing loop.** That loop is wired
to the barrier; this controller reads its detector's second output and drives
nothing on it, as it reads and never drives a pair of confirmation loops, and
there is no `close()` anywhere in this package. A controller that could close a barrier is a
controller that could close one on a vehicle; the safety case rests on that
being impossible rather than on us being careful.

**Every loop count, spacing and window is a per-site setting** in `[loops]`, and
every one of them is an ASSUMPTION until a site measures it — nothing here
measures a spacing. A site with one arming loop and no
closing loops runs exactly as it always did; what it does not get is named in
the record on every vehicle rather than described in a document.

**A third loop, before the arming loops, HOLDS arming**: the deactivate loop.
While it reads occupied the lane does not arm — a second vehicle that close
behind the one at the barrier would follow it through an open boom, so the
boom stays down until it backs off, and the assisted vend refuses for the same
reason (`vehicle_too_close`). The hold is a level re-read on every turn, never
an edge, and it is recorded at both ends (`arming_suppressed`,
`arming_suppression_ended`), because a car that was held and then followed the
one ahead anyway left neither picture nor event of its own. Zero or one per
lane, declared like every other loop, with a spacing bounded below so the loop
cannot sit under the tail of the very car it is meant to protect.

## Two properties worth stating plainly

**It works with the internet down.** Every decision is made from a local cache
of allow and pricing rules. The server refreshes that cache and receives events
afterwards; the barrier never waits on a network call. If the rules go stale
past their configured age, the lane falls back rather than acting on pricing it
no longer trusts.

**And the exit decides at the barrier, from the cache, before the barrier
moves.** At an exit lane a car the registers cover (a garage pass or a monthly
agreement that stands on the garage's local day) leaves **covered**, no fee; a
transient whose entry the cache holds is **priced on the box** by the rate
engine in-process — `rate_engine.contract.run_quote`, the one function behind
the platform's `/v1/quote`, so the lane's number and the platform's are one
computation; and a transient whose entry the cache does not hold yet — it
entered inside one stays interval, or during an outage — is said to be
**`no_cached_entry`**: the barrier opens as it always has, the platform settles
the stay at the close — covered if a module it asks there covers it, priced
through the engine if none does — nothing is collected at the reader, and the
record says so (`exit_pricing.py`, on the `decision` event). Nothing on that
path opens a socket to the platform: `tests/test_exit_decision.py` runs the
real Vehicle ID service on loopback, counts every connection the turn makes up
to the vend, and times it — a Mac number, a floor for a Jetson. **And the close carries that
decision** (`local_decision` on the session close, the whole of the `decision`
event's `exit_pricing`, unedited, handed down from the car's own decision and
never read back from "the last one"): a platform at migration 0017 or later
**consumes it** — writes the lane's fee, runs no engine — so the screen, the
card and the row are one computation, and its reconciler re-derives the number
out of band from what the decision says it was computed from. A platform that
does not consume it says why on the row; an older platform ignores the field
and prices as before. `tests/test_close_decision.py` holds the seam, and
`scripts/close_decision_fail_control.py` breaks it eight ways.

**And it works after a restart with the internet down**, when `[lane]
cache_path` is set: the cache is written whole to a SQLite file on every
refresh that changes it (`durable.py`) and read back at start, **as old as its
last refresh, not as old as the restart**. A lane that comes back after a power
cut with the platform still unreachable decides from what it last held, and a
lane that comes back after two days holds a cache it does not trust and says
so. Without a path the cache is memory only, as it always was. **The
outbox's stay opens and closes are kept beside it** (`decision-cache.outbox.sqlite`
beside `decision-cache.sqlite`, same directory rule, 0600), with the pictures
kept with the entries, written as each is recorded and removed once the platform
has taken or refused it, so a restart during an outage loses none of them; the
lane sends them first thing, in the order they happened. **The writes are made
by the outbox's own disk thread, never by the lane thread** (`events.DiskWriter`):
a stay is recorded on the thread that decides the next car, and a disk that is
slow or hung used to hold that car on SQLite's write; now it holds nothing on the
barrier's path. What that costs, stated: an item is on disk milliseconds after it
is recorded, and a process killed inside those milliseconds loses it -- the same
class of window as the one between the vend and the stay's record. They are not cache: each is held until the platform has
it, not for `rules_max_age_seconds`. Log events are not kept there; they are
bounded and dropped first, as before. `serve` stopped by Ctrl-C or SIGTERM
makes one last send on the way out.

**What the cache holds at rest is personal data, and its bound is one number.**
Every pass holder's and monthly vehicle's identity at the garage, and every
open stay's plate or ticket, are on the box. The platform's retention purge
does not reach that file; the lane's own rule does, and it is
`rules_max_age_seconds` — the age past which the lane stops trusting the cache
is the age past which it stops holding it, wiped from memory and disk on start
and on every refresh tick, and the file is replaced whole on every refresh so a
plate that left the register is gone at the next read. **The garage's entry
settings outlive it** -- whether it takes transients, its default, and this
lane's closing -- because they name no car and no person and the entry is
decided on them: a garage that takes transients still opens for every car two
days into an outage, and a lane closed `full` stays closed. **The directory must be
the process's own** — a row in that file is `plate → allow`, and a line
written by hand opens the barrier — so a path in a directory anyone else can
write refuses to start, before the port opens, by the same rule Vehicle ID
applies to its queue. `scripts/durable_fail_control.py` breaks each property in turn.

When identification is not confident enough, the
lane never picks the most likely plate: confidence is checked *before* the
plate is used to look anything up, so a low-confidence read cannot match a
rule or a register by accident, and it never becomes a stay's plate --
"unreadable" does. What the lane then DOES depends on the garage: at one that
takes transients the car is let in and the stay records what was seen; at a
pass-only garage it is denied; at a garage that has not said, it takes an
explicit fallback path — the `Fallback` members in `decision.py` — each of
which is a named outcome with an event behind it.

## The lane's contract

Everything outside this process reads the lane through one versioned contract —
[docs/CONTRACT.md](docs/CONTRACT.md). Our own intercom agent will be an ordinary
client of it, and a lane that is not ours can take the same seat:
`tests/third_party_lane/` is a minimal one, and the same consumer code reads
both.

```sh
lane-controller serve --config lane.toml
lane-controller serve --config lane.toml --platform-token-file /etc/lane/device.token
```

Loopback by default; off loopback it refuses to start without **both** tokens.
The second form is a lane with a platform behind it: `[lane] server_url` names
it and the device token it issued is in the file. `server_url` is `https://`
(plain `http://` only to a platform on this box: 127.0.0.1, ::1, localhost),
the platform client follows no redirect, and a token file readable by more
than its owner (anything but 0600 or 0400) is refused before the port opens:
each would hand the token to someone else. `serve` refuses the two
half-configured states by name — a `server_url` with no token, a token with no
`server_url`. **The production loop runs beside the contract** (`runner.py`):
one thread serves cars (`run_once`, for ever), one keeps the cache fresh on two
cadences, and the seam table printed at start says, per seam, what is REAL and
what is SIMULATED — this package ships no loop, camera or relay driver, and the
line saying so is per seam rather than one word for all of them.

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
| `sync_rules()` | the SLOW cadence: `GET /lane/rules` — the garage's rate plans whole, its space class, each entitlement module's register (verbatim; a module the platform could not read is NOT replaced with nothing) and the full set of open stays with a cursor. On failure it keeps the cache it had — a failed request is not a reason to forget good rules. |
| `sync_stays()` | the FAST cadence: `GET /lane/stays?since=<cursor>` — every stay opened or closed since, closed rows included so the exit lane drops a car that left, paged and followed at once. A refusal drops the cursor so the next read takes the full set. It also carries this lane's closing and the owner's board, so a closing acts within one fast interval. |
| `decide()` at a closed lane | closed to everyone: no car opens by itself, `fallback` / `lane_closed`. Closed `full` at an entry: a car a register covers today (the exit's own lookup, `register_cover`) gets in, every other identified car is `lane_closed`. A person's word through `POST /v1/lane/vend` opens it; a display code does not. |
| `decide()` and the garage's settings | **At an entry, what the garage takes decides** (`garage_admits`). `transient_available: true`: every car opens -- unread, unknown, engine down, rules stale -- except full (a register-covered car still opens), closed to everyone, banned, an owner's rule against the plate, or nothing there. `transient_available: false` (**pass holders only**), at ANY default: a car a register covers today opens, every other car is `deny` (`REASON_NOT_REGISTERED`, where a scanned registration QR will be presented, not built). `true` with a default of `deny` is read as pass-only, the stricter reading. A payload without the field is the lane as it was: the default (`allow`, `deny`) and the fallback path. The default is an ENTRY rule: at an exit a car with no rule is let out, and `price_exit()` says covered, priced or not priceable here. Only a plate read at the lane's confidence is ever looked up, in a rule or a register (`plate_read`). |
| `board_items()` | the board on `GET /v1/lane/state`: the owner's messages in force on this lane's clock, then — when switched on — the price for 1, 2, 3 and 24 hours entering now, from `charge`, the same quote-and-tax call the exit charges with. |
| `price_exit()` | the exit's local decision, from the cache alone: `covered`, `priced` (the engine in-process), `no_cached_entry`, or the engine's own refusal verbatim. Reads the registers with the garage's local day, and a monthly agreement's own days when its register carries them (start, pauses, the owner's block, the first day unpaid past grace); folds the plate and the registrar's identity to letters and digits, with O read as 0 and I as 1 because the plate reader outputs neither letter (an assumption, stated in `normalise_identity`). A stay whose close the lane records leaves its list, so a car that pulls forward again is not priced twice. |
| `LaneRunner` | the production loop. The lane thread calls `run_once` for ever; the refresh thread runs the two cadences (`[lane] rules_refresh_seconds`, `stays_refresh_seconds`), the slow one first at start. A refresh that could not run is counted and said on `RunnerState`, never hidden; a turn that raises is logged and the loop goes round again. **The fast interval is the size of the class of cars that entered too recently to be priced at the barrier** — `config/lane.example.toml` says so where the number is set. **The outbox drains on a third thread**: the controller's `deliver()` is a signal under the runner, so the lane thread never waits on the network after the vend either — against a platform that black-holes, the client's timeout per queued action is the drain thread's cost, not the next car's; an outbox the platform refused is retried every `DRAIN_RETRY_S` without waiting for a car. A bare controller (the tests, the demo) drains inline, and `run_once` returns delivered. |
| `EventQueue` | one outbox holding both the activity log and the session actions, so a lane that was offline replays what happened in the order it happened. Safe to record into from the lane thread while a drain is mid-send on another: a flush removes what it sent, never what was recorded meanwhile. **Session actions are never dropped**; only log events are, and a drop is counted. With `[lane] cache_path` the session actions and the pictures are also kept on disk until the platform has them (`durable.OutboxStore`), written by the outbox's own disk thread so the lane thread never waits on the disk. A flush goes part by part -- each session action, then the log in chunks of 200 -- and each part leaves as soon as it is delivered or refused; the first part the platform cannot be reached for stops the flush, and the next one starts there. |
| `PlatformTransport` | delivers one part at a time. A part is all or nothing, because every platform endpoint it calls is idempotent. A log post over 256 KB, or one the platform answers 413, goes again in halves; only one event too large to post alone is refused for its size. |

**Session times come from the lane, never the server.** A car arrives when it
arrives, whatever time the platform eventually hears about it; pricing a stay by
when the network came back would be wrong.

**At an exit, the lane names the session it is closing** when the platform is
reachable: the transport asks for it as it delivers the close, on the drain's
thread, so a platform that does not answer never holds the next car at the
barrier. A close delivered late is matched as one sent on the plate alone
would be: the platform refuses an exit that precedes the open stay's entry.
Offline it closes on the plate alone, which works and is merely less
precise.

**Every queued item carries an `event_id` generated on the lane.** That is what
makes reconnecting safe: the queue re-sends everything it could not confirm, and
the platform deduplicates. Without it, "flush the queue again" means "bill the
customer again".

An item the platform *refuses* — a 4xx, meaning it understood and said no — is
dropped rather than retried forever, because retrying poison blocks everything
behind it. Dropped items are counted and logged, never silent: a dropped session
is a gap in the money record.

**At the reader, the phone prompt and the validation** (`reader.py`,
`ValidatingScreen`). For a priced stay with a fee to pay, the reader's one
screen is the fee and an optional phone number with **Skip**, and its words
(`PROMPT_TEXT`) name both uses. A number entered is **claimed at that moment**,
before any amount to pay is shown: `PlatformClient.claim_validation` sends it
with the decision on screen to `POST /lane/sessions/<id>/validation`, and the
platform claims a live validation for the stay on that fee and holds it. The
amount the driver is shown next is the platform's discounted fee, with its
line appended to the engine's, **read, never worked out here**. Skip, a number
that matches nothing, and a platform that cannot answer all show the fee as
priced. The close carries the decision as priced **and what the reader showed**
(`reader_shown`, sealed as the close is recorded, amendment A2): the platform
records the claim it holds only when the reader showed the discounted fee, and
nothing discounted goes up for a stay after its close was recorded — so the
number on the reader and the number on the row agree in every order. **The number goes into that request body and nowhere else**:
not the record, not an event, not the outbox, not a log line, not a URL. The
reader's own input action (`PhonePrompt`) is handed in, as the cart's `post`
is; this package supplies no reader transport. `scripts/validation_prompt_fail_control.py`
breaks each property in turn.

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

Pass `--confidence 0.40` to watch a read the lane will not stand behind: at
the demo's garage, which takes transients, the car is let in and its stay's
plate is unreadable (the platform needs migration 0036) -- and the exit, which
matches nothing to an unread stay yet, hands the car to a person.

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
python scripts/drain_fail_control.py         # breaks the drain thread, requires the tests to fail
python scripts/confirmation_fail_control.py  # breaks the confirmation, same requirement
python scripts/contract_fail_control.py      # breaks the read contract, same requirement
python scripts/deactivate_fail_control.py    # breaks the deactivate loop's hold, same requirement
python scripts/entry_stay_fail_control.py    # breaks the owner's entry rule, same requirement
```

## Vehicle ID

Identification is a **separate system**, and its repository is not public.
This lane is an ordinary client of its contract — the same interface a third
party integrating their own system uses. The lane carries its own copy of that
contract (`src/lane_controller/vehicle_id_contract.py`); there is no
in-process path reserved for us, and `tests/test_vehicle_id_boundary.py` fails
if this package ever imports anything from the identity service's package.

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
| Detection | Inductive arming loops before the barrier, an optional deactivate loop before those, and the barrier's own closing loop, wired to the barrier and read through its detector's second output; or two confirmation loops after the gate |
| Cameras — default | Reolink RLC-810A |
| Cameras — upper tiers | Axis P1465-LE, Hanwha XNO-9082R |

RTSP and PoE are required for any camera.

Vehicle ID runs in its own process on the Jetson, beside this one; its
repository is not public. The platform server is Node/Express/Postgres — see
[openparking-ai/platform](https://github.com/openparking-ai/platform).

## Licence and contributing

AGPL-3.0-or-later — see [LICENSE](LICENSE).

Open Parking AI does not accept outside contributions. Pull requests, issues and comments are limited to the maintainers.

---

Built by 72 Knots Method by 72Knots.ai
