# The lane contract

This is the lane's public surface. Everything else in the repository is an
implementation detail that may be rewritten; this is not.

Open Parking AI's own software integrates through exactly what is described
here. There is no private path, no second mode and no in-process shortcut
reserved for us — so if this contract is inadequate, we find out first.

**A lane that is not ours can take this seat.** `tests/third_party_lane/` is a
minimal lane that implements this contract and nothing else — no loops, no
confirmation, reasons outside our vocabulary — and the same consumer code reads
it and reads ours. If that test ever needs a special case for either lane, this
contract is wrong.

---

## Four reads and ONE act

Four `GET` routes that change nothing, and one `POST` that opens a barrier:

| | |
|---|---|
| `GET /v1/lane` | who this lane is, and what it can do |
| `GET /v1/lane/state` | the last decision, and the current transit |
| `GET /v1/lane/health` | every malfunction code, with its source |
| `GET /v1/lane/events?since=N` | the event cursor |
| `POST /v1/lane/vend` | **the assisted vend** — the only route here that changes anything |

Every other method, and a `POST` to any other path, is answered `405` with
`Allow: GET, POST`.

**The act surface arrives deliberately late**, after the display and the agent,
because it is the first thing that can open a barrier — and a new route to a
vend is the boundary every outside reviewer of this project has named. What
makes it safe is stated with the route below and is not a promise in prose:
**this lane applies its own refusals, and it writes the completed identity
before it pulses the relay.** `capabilities.can_vend` says whether a given lane
serves it, and it is derived rather than declared.

## Compatibility

`contract_version` is `2`, and every payload carries it.

**What version 2 changed, in one line:** `POST /v1/lane/vend` exists, and
`capabilities.can_vend` answers `true` at an entry lane where version 1 answered
`false` for every lane there has ever been. A consumer written against version 1
read that field to mean *nothing here opens a barrier*; it no longer does. A
value a consumer branches on changing meaning is exactly what this number is
for.

On the read routes, `GET /v1/lane/state` gained one field — `decision.completed`
— and nothing else changed shape. **It is required of a lane that serves the act
route and optional for one that does not**: a lane with no act surface has no
completions and nothing to report, so a consumer must not require it of every
lane, and must not read its absence as `false`.

- **Additive changes do not bump it.** New fields may appear. Ignore fields you
  do not recognise rather than rejecting the payload.
- **Anything a consumer could notice bumps it** — a field removed, renamed, or
  changed in meaning or type.
- **An unrecognised version is refused, not partially read.** Half-understanding
  a payload about a vehicle is worse than admitting you cannot read it.

This is the same policy the [Vehicle ID
contract](https://github.com/openparking-ai/vehicle-id/blob/main/docs/CONTRACT.md)
states, in the same words, so one consumer can hold one policy for both.

---

## `GET /v1/lane` — who this lane is, and what it can do

<!--payload:lane-->
```json
{
  "lane_id": "lane-1",
  "site_id": "site-1",
  "direction": "entry",
  "contract_version": 2,
  "geometry": {
    "arming_loops": 2,
    "arming_spacing_m": 1.5,
    "closing_loops": 2,
    "closing_spacing_m": 1.5,
    "confirmation_window_seconds": 10.0
  },
  "event_window_depth": 256,
  "capabilities": {
    "confirms_entry": true,
    "has_identity_service": true,
    "has_platform": true,
    "has_display": false,
    "can_vend": true
  }
}
```

`direction` is `entry` or `exit` — a physical lane runs in one direction, and a
controller does one or the other, never both.

`geometry` is **exactly** what the lane writes on every vehicle under
`geometry_assumed`: the same dict, from the same method
(`LoopConfig.as_published()`). It is not a second rendering of the loop
geometry, because a second rendering is a second thing to go stale. Every value
in it is a **per-site setting and an assumption** — nothing in this package
measures a spacing.

`event_window_depth` is how many events `GET /v1/lane/events` can still serve
behind the current cursor. Fall further behind than this and you are told
**`reset`**, not served a short page. It is published rather than described
here because it is a property of *this* lane's window, and a document can only
describe one lane's.

### The capability set

Each of these is derived from the lane it describes — its declared geometry,
its wiring, its route table — and none is a flag somebody set.

| | |
|---|---|
| `confirms_entry` | Two loops after the barrier can say whether a vehicle actually went through. **`false` is an ordinary lane, not a broken one**, and a consumer must have a case for a lane that cannot confirm anything, because a third-party lane usually cannot. |
| `has_identity_service` | This lane is wired to an identification service. `false` means identity comes from somewhere else, or from nowhere. |
| `has_platform` | This lane reports to a platform. **`false` is standalone, which is a supported mode and not a degraded one.** |
| `has_display` | A display this lane can put a per-arrival code on. `false` until a display seam exists; derived from the wiring, so the day one is wired this follows it. |
| `can_vend` | Whether this lane serves a route that opens a barrier. Derived from the service's own act-route table **and this lane's direction** — so a vend route cannot be added without this answer changing with it, and an **exit** lane answers `false` and serves no such route rather than announcing a capability every call to which it would refuse. Completing an identity at an exit would close a stay and freeze a fee, which is the exit process and is not this round. |

## `GET /v1/lane/state` — the last decision, and the current transit

<!--payload:state-->
```json
{
  "contract_version": 2,
  "decision": {
    "outcome": "fallback",
    "reason": "engine_unreachable",
    "fallback": "engine_unreachable",
    "cause": "unreachable",
    "presence": true,
    "at": "2026-08-30T14:03:11.482913+00:00",
    "read_ref": null,
    "completed": false
  },
  "transit": {
    "state": "pending",
    "since": "2026-08-30T14:03:11.482913+00:00"
  }
}
```

**`decision.completed` says whether this case is still open.** A decision is
one case and one case is one vend: once `POST /v1/lane/vend` has completed it,
this reads `true` and any further completion naming it is refused
`already_completed`, whatever `Idempotency-Key` it carries. It is here because
without it the decision, its outcome and its `at` are byte-identical before and
after a vend, so a second consumer could not see that the first one had acted.
It returns to `false` on the next arrival, because a new decision is a new case
— and a new arrival is the only route to a second vend.

**`decision` is `null` until this lane has decided something.** It lives in
memory for as long as the process does; this package has no state store and
this contract does not add one. After a restart `decision` is `null` and
`transit.state` is `none` — which is the honest answer, and is not the same
thing as "nothing has ever happened here".

### `outcome` is CLOSED. `reason` is OPEN.

`outcome` is one of the values of `decision.Outcome` — **listed in full under
The closed sets** — and that set will not grow without a version bump. A consumer has to be able to branch on what
happened to the vehicle — admitted, refused, handed to a human, or nothing was
there — and a lane that could invent a fifth would leave every consumer with a
case it has no behaviour for.

`reason` is where a lane's own vocabulary goes, and it is an **open string with
a required closed subset**. The subset is `contract.REQUIRED_REASONS`, derived
from `decision.Fallback` — this document does not list it, because a
hand-written copy of a set the code defines is the copy that goes wrong.

**A lane that is not ours may emit a reason outside that subset, and will.** A
consumer that does not recognise a reason **escalates** — it hands the vehicle
to a human. It does not map the reason onto the nearest thing it knows, and it
does not treat it as a fallback it understands. Guessing here is the standing
acceptance of this project broken at the seam it matters most.

`fallback` is **derived from `reason`**, not stored beside it: it carries the
value when the reason is a member of our closed subset and `null` when it is
not. So the two cannot come to disagree, and `"fallback": null` on an outcome of
`fallback` is precisely the signal to escalate.

`cause` says **which** failure stopped the lane getting a read at all, and is
`null` when it got one. Open in the same way, with `contract.REQUIRED_CAUSES`
— derived from `interfaces.Unavailable` — as the subset a consumer may assume.
It exists because a dead identification engine and a marginal plate read used
to arrive as the same code, and an agent reading that code would tell a driver
to wipe a plate while the service was switched off.

`presence` is `true`, `false`, or `null` for **not measured**. A consumer that
reads `null` as `false` turns every lane without a reference view into one that
refuses every customer.

`read_ref` is the identification this decision was made from, when the
identifier names one — Vehicle ID's `read_id`, which is unique per
identification and stable across re-delivery. `null` when no read was obtained,
and `null` from an identifier that does not name its reads.

### The transit

`transit.state` is one of `contract.TransitState` — **listed in full under The
closed sets** — and `since` is `null` exactly when the state is `none`.

`held` is neither a confirmation nor a refutation, and is never folded into
either: voiding it silently re-creates the abandoned-ticket fraud, and promoting
it to a session is the phantom occupant that fills a garage on paper before it
fills in concrete.

`backed_out` means the loops saw the crossing go the other way. **Read it with
`direction`** — at an `exit` lane that is a vehicle that reversed back inside,
which is a different fact about different money from a vehicle that backed out
of an entry.

## `GET /v1/lane/health` — every malfunction code, every time

<!--payload:health-->
```json
{
  "contract_version": 2,
  "codes": [
    {
      "code": "reference_not_recognised",
      "state": "unknown",
      "source": "not_measured",
      "never_alarm": true,
      "caveat": "NOT an alarm. This code originates in the identification engine, which publishes what the one reason covers in its own `camera_faults_caveat` -- read that, not this. One of the causes it covers is an ordinary car arriving, so paging a human on this code is the failure the caveat exists to prevent."
    }
  ]
}
```

One entry per member of `contract.MalfunctionCode`, and **every one of them
ships on every response**. A payload missing a code is refused when it is built,
because a code that is absent reads to a consumer exactly like a code that is
fine. The whole set is published below, under **The closed sets**.

`never_alarm` is a **required JSON boolean on every entry**, and so is `state`.
An implementer of this contract emits `true` or `false` for every code on every
response — not a string, not a number, and never absent. A consumer may not
guess what a missing one meant: absent, it could be a lane that has nothing to
say or a lane whose serialiser dropped it, and the two point in opposite
directions. One of them dispatches a technician because a car arrived
(`reference_not_recognised`), and the other silences a real fault for ever.

### `state`, and why `unknown` is not `ok`

| | |
|---|---|
| `ok` | Somebody measured, and found nothing wrong. |
| `active` | The malfunction is happening. |
| `unknown` | **Nobody measured.** |

**A consumer may never read `unknown` as `ok`.** At a lane, `false` is not a
safe default and neither is a clean bill of health: the value that means "I
cannot tell" is its own value, and it is this one.

That is enforced rather than asked for. `ok` and `active` are claims about a
measurement, and `contract.HealthEntry` **refuses** either from a code whose
source is not `measured`.

### `source`, and why a health surface needs one

| | |
|---|---|
| `measured` | This build derives a state for the code from something it observes. |
| `not_measured` | A signal exists — named below — but this build does not read it, so it has nothing to say. |
| `no_source` | Nothing in this system produces this signal at all. It stays `unknown` until something is built that can answer it. |

The distinction is the difference between the two kinds of work: closing a
`not_measured` code is **reading something that already exists**, and closing a
`no_source` code is **building it**. A surface that reported them the same way
would hide which of those an operator is waiting for.

Where the `not_measured` signals live today, so nobody has to go looking:

- `arming_loops_disagree` — the lane already writes an event per vehicle
  (`arming_incomplete`). What does not exist is the aggregation that turns a RUN
  of them into a fault, and nothing has measured how many in a row a run is. A
  threshold invented here would be a number nobody measured, applied at every
  site;
- `camera_feed_lost`, `lens_obstructed_or_dark` and `reference_not_recognised` —
  the identification engine's own `GET /v1/health`, under `camera_faults`. That
  field is a **count since that service started**, not a current state, and a
  count is not a state: a camera that failed at 3am and was fixed at 4am reports
  the same number for ever. Reading it as a state needs a rate over a window,
  and what rate is a fault has not been measured either. So what is missing here
  is a current-state SIGNAL, not a read of an existing one;
- `lane_gone_quiet` — the platform writes `lane_devices.last_seen_at` on every
  authenticated lane request and publishes it on `GET /garages/:id/devices`.
  This lane is not the thing that can read it: a lane that has gone quiet is
  quiet, so the fault is only visible from the other end, and it is derived by
  whatever watches both.

Every code this build calls `not_measured` is named above, and no code it
measures is — `tests/test_lane_contract.py` requires both directions against
`contract.SOURCES`, so the list cannot fall behind the code in either.

`identity_service_degraded` reads the identification engine's own `status`,
which that service sets to `degraded` when a read was **lost** or when its queue
held a line it could not read — the two cases where a record was answered and
then existed nowhere. This lane asks the service when it is asked, rather than
remembering the answer from the last vehicle: at a lane with no arrivals since
midnight, the memory is the whole night old. A service that cannot be read
answers `unknown` here, never `ok`.

`clock_skew_rejected` reads the platform's own name for the refusal. A `409` is
the platform's terminal refusal and seven different conditions produce one; six
are ordinary and the seventh means every session open and close this lane sends
is being **dead-lettered**, with the barrier still working and the money record
silently losing the stay. The platform names each refusal in a `code` field, so
this lane counts the named skew separately from every other conflict.

An unnamed conflict counts as **neither**. A platform that predates that field
refuses a skew exactly as it refuses everything else, so this code answers
`unknown` after one — never `ok`. It also answers `unknown` on a lane that has
attempted no platform call at all: nothing was sent, so nothing could have been
refused, and a clock nobody has had checked is not a clock found correct.

**It recovers.** The next write the platform ACCEPTS clears both, so the code
reads `ok` again once the clock is fixed and the lane is being taken. A code
that could only ever go one way would be a latch that reads like a state:
`active` for the life of the process however long ago the repair was made, with
no recovery for a monitor to report.

`identity_service_degraded` is read on the request, so this route waits on a
process that is usually on another machine — and it waits **at most
`[lane] identity_health_timeout_s`, which defaults to 1.0 second**. That bound
is a **setting and an assumption**: nothing here measures how long a loaded
identification service takes to answer its own health route. What it is drawn
against is the other side of the seam. This route is polled by a monitor, and a
monitor gives up on a target that does not answer — so a lane that blocked on a
THIRD machine for as long as its own consumers are willing to wait would be
published as a dead lane while it was up and serving, taking every real signal
it publishes with it. **A hung identification service costs this one field, not
this payload.** On timeout the entry is `unknown`: nobody measured, never `ok`
and never `active`. A consumer of this route sets its own timeout comfortably
above this one.

`arming_loop_stuck_occupied` reads THIS LANE'S OWN ARMING LOOP, and the
measurement is exactly this: the lane samples the loop on every poll of its
sequence — arrival or not — and on every call to this route, and holds the
instant of the first observation in the current unbroken run of occupied
readings. The code is `active` when that run is older than
`[lane] arming_loop_max_occupied_s`, which defaults to **600 seconds**.

That number is a **setting and an assumption, and nothing has measured a
dwell**: no site exists, no arrival has been timed, and 600 seconds is not a
measurement of how long a real vehicle waits at a real barrier. It is drawn
against what the code means — a car that has been on the arming loop for ten
minutes is a breakdown, a van somebody parked, or a loop reading occupied with
nothing on it. All three want a human, and the third is the one that matters
here: **a stuck arming loop defeats this route's first refusal rather than being
caught by its second.** `no_vehicle` asks the loop whether a car is there and a
stuck loop says yes; `geometry_incomplete` asks the second loop the same
question. Without this measure a lane with stuck arming loops would accept every
assisted vend with nothing in front of it.

The state is `active` or `unknown` and **never `ok`**: what is observed is one
way for a loop to be wrong, and a loop that reads clear at this instant has not
been found healthy. The claim is about this lane's own observations and not
about the gaps between them — it samples when it polls and when it is asked, and
it says so rather than claiming continuity it did not watch.

`closing_loops_never_firing` is derived from **one named occurrence**, and the
name suggests something else, so this says which: an assisted vend whose
`resolve_transit` had not returned by the confirmation window plus
`[lane] settle_grace_s`. That is a loop driver that did not return AT ALL — not
a run of crossings that were missed, which is the aggregation nobody has
measured and which is still not built. The transit is published `unconfirmable`
with reason `loop_driver_timeout` and no session is opened.

It is **latched** and it is never `ok`. A driver that stops answering is a fault
somebody goes and fixes, so it does not clear the next time a transit happens to
settle; it clears when the lane restarts, by which point a person has been
there. And it is never `ok` because nothing here observes the loops WORKING: an
ordinary arrival's crossing wait is not bounded by this deadline, so silence is
not evidence of health.

`outbox_depth_growing` reads the outbox's PENDING DEPTH — every undelivered
event, log entries and session actions alike — against a per-site threshold,
`[lane] outbox_depth_threshold`, which defaults to **1000**. That threshold is a
**setting and an assumption**, not a measurement: nothing here measures how deep
a healthy lane's outbox gets. It is drawn well below the point at which the
bounded log begins discarding entries, so the code reads `active` while a human
can still act on it rather than after events have already been lost.

### `never_alarm`

`never_alarm` and `caveat` come from one mapping (`contract.NEVER_ALARM`), so a
code cannot be published as safe in one payload and alarmed on in the next.

`reference_not_recognised` is on it. That code originates in the identification
engine, whose own `camera_faults_caveat` says what the one reason covers — and
one of the causes it covers is **an ordinary car arriving**. A gate that pages a
technician because a car arrived is the failure that caveat exists to prevent,
so a monitor built on this surface must not page a human on it.

## `GET /v1/lane/events?since=N` — the cursor

<!--payload:events-->
```json
{
  "contract_version": 2,
  "cursor": 7,
  "reset": false,
  "dropped": 0,
  "events": [
    {
      "cursor": 7,
      "event_id": "9f2c1a7d-4e8b-40c2-a1f6-d3b8e5c07a91",
      "kind": "decision",
      "lane_id": "lane-1",
      "occurred_at": "2026-08-30T14:03:11.482913+00:00",
      "detail": {}
    }
  ]
}
```

Deliberately the same shape and the same semantics as the Vehicle ID service's
`GET /v1/reads?since=N`, field for field, so one consumer can hold one cursor
policy for both surfaces.

- The cursor is **monotonic within one run** and is **not durable across a
  restart**. It is a catch-up window for a consumer that blinked, not a record of
  anything. The durable copy of what happened at a lane belongs to the platform
  it reports to.
- `since` ahead of the lane's own cursor sets **`reset`**. That means the
  process restarted and your saved position no longer refers to anything. An
  empty list without that flag would be indistinguishable from "nothing
  happened", which is how a consumer silently misses everything after a restart.
- `since` **behind the oldest event still held** also sets `reset`. The window
  is bounded — `event_window_depth` on `GET /v1/lane` says by how much — and a
  consumer that has fallen further behind than that would otherwise receive a
  page with the evicted events simply absent from it, which looks exactly like
  a complete one. The Vehicle ID service does not have to report this, because
  its contract tells a consumer that needs guaranteed delivery to use push and
  it has push. **This contract has no push path**, so the eviction is reported
  here or it is not reported at all.
- `dropped` is the lane's count of **log** events the OUTBOX discarded because
  it reached its limit. That is a different queue with a different bound from
  the read window above, and it is published because a gap nobody knows about
  is worse than one that is counted.

**This route serves LOG events.** Session actions — a session opened, a session
closed — are not on it. They are the ledger's: they become
`POST /lane/sessions/open` and `/close` on the platform this lane reports to,
they carry the plate, and the platform is the durable record of them. What
happened at the lane is answered here and by `GET /v1/lane/state`; who was in
the vehicle is not on this contract at all.

`detail` is opaque to this contract. It is whatever the lane recorded with the
event, and a consumer ignores keys it does not recognise. **No plate text goes
in it**, on any route of this contract.

**ADDED, and it was always what this lane served: `occurred_at` is ISO 8601 with
an explicit UTC offset.** It was shown that way in the example above and never
stated, so an implementer of this contract could read the example as one
formatting of a local time — and a consumer subtracting a naive timestamp from
its own clock gets an answer that is wrong by whatever the two machines'
timezones differ by, with nothing anywhere saying so. A value test walks a served
events page and refuses a timestamp with no offset.

## `POST /v1/lane/vend` — the assisted vend

The one route on this contract that changes anything. It completes a decision
this lane already made and could not finish on its own: a driver whose plate was
not read, at a barrier that did not open, with a human or a confirmed display
code saying to let them in.

**It is not a request to open a barrier. It is a completion of a case this lane
is already holding**, and the difference is the whole design: the caller names
the decision it is completing, and this lane checks that the decision is its
own, is recent, and is one there is anything to complete.

### What makes it safe, and it is not a promise in prose

**This lane applies its own refusals.** Presence is read off the arming loop at
the moment of the call, not taken from the body. The malfunction table is this
lane's own. The arming geometry is the same check `run_once` makes before it
arms. All of them come from the code an ordinary arrival goes through, so a
refusal cannot exist on one path and not the other.

**This lane writes the completed identity BEFORE the relay moves**, in this
order and no other:

1. `assisted_identity` — who authorised it, which **kind** of identity it is, a
   `completion_id` this lane mints, the decision it completes, and whether it
   overrode a `deny`
2. the relay, with the **authority** as its reason
3. `vended`
4. the pending entry — the ticket is not the entry here either

If a caller asserted the completion and this lane trusted it, the caller would
be `POST /sessions/open` with a microphone attached, and the fraud the loops and
the presence gate exist to close would be open one layer down.

**This lane verifies NOTHING about the ticket beyond its shape** — 6 to 64
characters of `A-Z`, `0-9` and hyphen. No signature, no expiry, no issuer. It
holds no key and mints no ticket; the agent does both. This lane's whole claim
about a ticket is that it looked like one and that a named authority said to
open.

### The request

```
POST /v1/lane/vend
Authorization: Bearer <the ACT token>
Idempotency-Key: <the caller's own key>
Content-Type: application/json

{
  "authorised_by": "human_open_now",
  "identity": { "kind": "ticket", "ticket_ref": "TKT-4RS9WQ2M" },
  "decision_at": "2026-08-30T14:03:11.482913+00:00"
}
```

| | |
|---|---|
| `authorised_by` | a member of `vend_authorities`, published in full under **The closed sets**. Every one names a decision a person or a confirmed display code made; none of them is "the caller asked". |
| `identity.kind` | a member of `vend_identity_kinds`. **`ticket` is the only one in this version, and a plate is deliberately not on it**: a plate is what the camera reads, and a caller that could assert one would be handing this lane a measurement it did not make. |
| `identity.ticket_ref` | the completed identity. Opaque here; see above. |
| `decision_at` | the `at` of the decision being completed, **exactly as `GET /v1/lane/state` published it**. The instants are compared, not the strings, so a caller that parsed and re-serialised it names the same moment. |
| `Idempotency-Key` | **required**, and there is no generated fallback. A key this lane invented would be unique per request, which is the same as having none: the caller's retry would vend a second time. **It has the ticket's shape** — 6 to 64 characters of `A-Z`, `0-9` and hyphen, `400` otherwise — because it is an opaque token to this lane and an unbounded one is a caller choosing how much memory this process uses. A lowercase UUID is not in the alphabet; upper-case it. |

Anything malformed is a **`400`**, and it is never a `409`. The difference
matters to a caller: a `409` says this lane refused a well-formed completion for
a reason about the lane or the vehicle, and is worth retrying when that reason
clears; a `400` says the caller is broken and retrying will not help.

### The answer

<!--payload:vend-->
```json
{
  "contract_version": 2,
  "vend_commanded": true,
  "event_cursor": 7,
  "transit": "pending"
}
```

**`202`, and the field is `vend_commanded` — never `opened`.** Nothing in this
system measures the boom: `boom_did_not_rise` and `boom_did_not_close` are both
`no_source` on `GET /v1/lane/health` and stay that way until something is built
that can answer them. A field called `opened` here would be this lane's own
health surface contradicted one route away.

`202` and not `201` for that reason and one more: what follows the vend is
decided by the loops after the barrier, over the confirmation window, **after
this answer has been sent**. `transit` is `pending` because that is what it is
at the moment of the answer — a pending entry, created by the vend, waiting on a
crossing. What became of it is on `GET /v1/lane/state`, and `event_cursor` is
where to start reading `GET /v1/lane/events` for the events that followed.

### Idempotency

One key, one vend. A replay returns **the same body** and moves nothing — and it
is answered before any refusal is applied, because a replay is not a new
attempt: the car that was on the loop when the first call vended has driven off
by the time a retry arrives, and a replay checked against the loop would tell a
caller its own successful vend failed.

**THE KEY IS PUBLISHED NOWHERE.** It is held in this process's idempotency store
for the run and reaches no event, no read route, no log line and no session
action. `assisted_identity` carries a `completion_id` this lane mints instead —
opaque, random, and unrelated to the key and to the ticket. The reason is the
sentence this contract already makes about `GET /v1/lane/events`: *no plate text
goes in it, on any route of this contract*, which cannot be true of a field the
caller writes. That event reaches the platform's `events` table, which is
append-only by grant there, so anything a caller put in the key could never be
removed by anybody — and the natural key for "one ticket, one vend" is the
ticket.

**Only accepted vends are held**, for the run, and the last **256** keys — the
same depth as the read window, so a consumer holds one idea of how far back this
lane remembers anything — and at most **64 kB** of keys and answers. Both bounds
are real: the count is the one a consumer reasons about and the bytes are the one
the machine does. A refusal is not an answer to replay: every one of them
is about the world at the moment of the call, and freezing one against a key
would refuse a caller for ever on a condition that has already passed.

### The refusals

Each is a **`409`** carrying its name in `code`, and they are applied in this
order. The full set is published under **The closed sets** as `vend_refusals`.

<!--payload:vend_refused-->
```json
{
  "contract_version": 2,
  "error": "this lane has an active malfunction: arming_loop_stuck_occupied",
  "code": "malfunction_active",
  "malfunction": "arming_loop_stuck_occupied"
}
```

| order | code | when |
|---|---|---|
| 1 | `no_vehicle` | the arming loop reads unoccupied **now**. Not the caller's word for it, and not what was true when the decision was made |
| 2 | `malfunction_active` | a code in `vend_blocking` is `active`. The code is named in `malfunction` |
| 3 | `geometry_incomplete` | a two-loop lane with one loop occupied — the same check that stops the lane arming |
| 4 | `decision_in_future` | `decision_at` is **ahead of this lane's clock** |
| 5 | `decision_stale` | `decision_at` is older than `[lane] completion_max_age_s` |
| 6 | `decision_mismatch` | `decision_at` is not the moment of this lane's last decision — **or this lane has decided nothing**, which is the same fact to a caller |
| 7 | `already_completed` | that decision **has already been completed**, whatever key this call carries |
| 8 | `not_completable` | the last decision's outcome is `allow` or `deny`; see below |
| 9 | `busy` | a vend is in progress on this lane |

`malfunction` is present on **every** refusal, `null` on all but
`malfunction_active`. A field carried by one refusal and absent from six cannot
distinguish "this was not a malfunction" from "this lane is too old to say",
which is the mistake this project has already made once at the platform's
conflict codes.

**`completion_max_age_s` defaults to 120 seconds. It is a SETTING and an
ASSUMPTION**, per site, and nothing here measures how long an intercom
conversation takes. What it is drawn against is what a completion *means*: an
answer to a driver who is at the barrier now. A completion accepted against a
ten-minute-old decision opens a barrier for whoever happens to be there.

**ONE CLOCK AGES A COMPLETION, and it is this lane's.** `decision_at` is stamped
by this lane and must be the instant it published, so the caller's clock never
enters the subtraction — a caller whose clock is an hour out completes exactly
the same decisions this lane would have accepted from a caller whose clock is
right. The bound below it exists for the other direction: a lane whose OWN clock
steps backwards would otherwise widen the window by however far it stepped,
because `age > max_age` has no lower bound of its own. A negative age is never a
fresher decision, so it is `decision_in_future`.

**`already_completed`, and it is the guarantee the barrier needs.** A decision is
one case and one case is one vend. The idempotency store below gives you *one
key, one vend*, which is true and is not the same statement: a caller that
regenerates its key on retry — the commonest idempotency bug there is — would
otherwise mint a second ticket, a second billable stay and a second occupant for
one car, and every other refusal would pass it, because the car is still on the
loop, the decision is seconds old and the outcome is still `fallback`. **The
only way to a second vend is a second decision, which is a second arrival.**
`GET /v1/lane/state` publishes `decision.completed` so a consumer can see the
case is closed without discovering it by trying to complete it.

**`not_completable`, and the one case that is not.** `allow` has already
vended — there is nothing left to complete. `deny` is a **rule**, and a human
overturning a rule is a deliberate, single, named act: `human_open_now` on a
`deny` **IS allowed**, vends, and is recorded as an override —
`assisted_identity` carries `override_of: "deny"`. No other authority overrides
a rule, `human_open_and_flag` included: a completion the human is unsure about
and a human overturning a refusal are different acts, and one of them is not
made safer by being uncertain.

**Refusal 2 refuses on a PUBLISHED SUBSET, `vend_blocking`, and not on the whole
table.** The five members are listed in full under **The closed sets**: they are
the codes about the physical act of opening safely — the boom, the relay that
drives it, and the arming loops that say something is in front of it.

**Every other code, including every one this build measures, no longer refuses a
completion, and the reason is one sentence: they concern the reading and the
record, not the barrier.** Whether the engine answered, whether the outbox is
draining, whether the clock agrees — none of those is a reason to refuse the
driver whose reading failed, which is exactly who this route exists for. The
earlier behaviour refused on any measured code, so a lane whose platform had
been unreachable long enough to grow its outbox refused every intercom
completion at the site.

**Two things about the subset, said plainly rather than left to be discovered.**
Of the five, only `arming_loop_stuck_occupied` can be `active` in this build —
it is measured, and how is under `GET /v1/lane/health` above. The other four are
`no_source`: nothing in this system produces them, so refusal 2 fires today on
one code and not on five, and it will fire on the rest when something is built
that can answer them. And `arming_loop_stuck_occupied` is the one that matters
most: it is the only member that defends the route's FIRST refusal, because a
stuck loop tells `no_vehicle` there is a car there.

### The two tokens

**A read token never authorises an act.** The reads take the token from
`--auth-token-file`; this route takes the token from `--act-token-file`, a
**second file**.

| | |
|---|---|
| the read token on this route | `403` |
| the act token on a read route | `403` |
| either token in a **query string**, on any route | `401`, and the request is not served |
| no credential where one is configured | `401` |

`403` and not `401` for the wrong token, because the caller is known: it holds a
real credential of this lane's and is asking for something that credential does
not buy. A `401` would invite it to retry with the same token for ever, and
would make the read token and a wrong guess look identical — which is the whole
point of there being two.

A credential in a query string is refused **whichever token it is**, by name and
by value: a token in a URL is a token in an access log, in a proxy's log and in
a browser history, none of which anybody decided. Serving such a request would
hand the caller a working integration that publishes its own credential on every
call.

**Loopback takes neither**, exactly as the reads do. Off loopback the service
**refuses to start** without both — the read token because this publishes where
a vehicle was, and the act token for the larger reason that the exposed thing is
no longer a description of a lane, it is a barrier.

### What is on this route and what is not

`ticket_ref` is on the **request**, and from there it goes to exactly one place:
the session action, which becomes `POST /lane/sessions/open` on the platform and
lands in a column that platform's retention purge redacts.

**It is on no read route and in no log line.** `assisted_identity` and
`entry_pending` carry the identity's **kind** and not its value, because both
reach `GET /v1/lane/events` and the platform's `events` table — which is
append-only by grant there, so a reference written into a detail would be the one
identity nothing could ever remove.

**A `ticket_ref` is an OPAQUE IDENTIFIER, and it is not a secret.** It
identifies one stay for as long as that stay is open, and the platform publishes
it in full to any holder of an operator token — the same posture a plate already
had there. It is personal data and it is handled as such; what it is not is a
credential, and nothing anywhere may treat holding one as authority to do
anything. **The secret is the signed token the agent will mint, which never
leaves the agent and never reaches this lane.** Both repositories describe the
value in these terms.

**`assisted_identity` is readable by the READ token, deliberately.** A monitor
polling `GET /v1/lane/events` learns which authority opened a barrier and when,
and that is what a monitor is for: a barrier that opened on a human's say-so
with nothing watching is the thing this event exists to prevent. It carries no
ticket, no plate and no key, so what is exposed is the fact and the authority
and nothing that identifies the driver.

## The closed sets, in full

Everything below is a set this contract declares CLOSED, published here so that
a lane which is not ours can be written **from this document alone**. It used to
say it did not list them, on the reasoning that a hand-written copy of a set the
code defines is the copy that goes wrong. The reasoning is right and the
conclusion was wrong: withholding them did not remove the second copy, it moved
it into every implementer's guess — and the stub in this repository that exists
to prove a stranger can take this seat had to import our Python package for the
sets the document withheld.

So they are published, and the copy is held to the code by a test:
`tests/test_lane_contract.py` compares every member below against the enum it
comes from, in both directions. Dropping one from this block goes red, and so
does adding one to the enum without adding it here.

<!--payload:sets-->
```json
{
  "malfunction_codes": [
    "boom_did_not_rise",
    "boom_did_not_close",
    "vend_relay_fault",
    "arming_loop_stuck_occupied",
    "arming_loops_disagree",
    "closing_loops_never_firing",
    "camera_feed_lost",
    "camera_feed_frozen",
    "lens_obstructed_or_dark",
    "reference_not_recognised",
    "identity_service_down",
    "identity_service_degraded",
    "identity_service_unmeasured_weights",
    "platform_unreachable",
    "lane_gone_quiet",
    "outbox_depth_growing",
    "session_actions_dead_lettered",
    "intercom_registration_lost",
    "controller_on_battery",
    "disk_nearly_full",
    "clock_skew_rejected"
  ],
  "outcomes": [
    "allow",
    "deny",
    "fallback",
    "no_vehicle"
  ],
  "transit_states": [
    "pending",
    "confirmed",
    "held",
    "backed_out",
    "unconfirmable",
    "none"
  ],
  "never_alarm": [
    "reference_not_recognised"
  ],
  "vend_blocking": [
    "boom_did_not_rise",
    "boom_did_not_close",
    "vend_relay_fault",
    "arming_loop_stuck_occupied",
    "arming_loops_disagree"
  ],
  "vend_authorities": [
    "display_code_confirmed",
    "human_open_now",
    "human_open_and_flag"
  ],
  "vend_refusals": [
    "no_vehicle",
    "malfunction_active",
    "geometry_incomplete",
    "decision_in_future",
    "decision_stale",
    "decision_mismatch",
    "already_completed",
    "not_completable",
    "busy"
  ],
  "vend_identity_kinds": [
    "ticket"
  ]
}
```

- **`malfunction_codes`** — every member of `contract.MalfunctionCode`. One
  entry per member ships on every `GET /v1/lane/health`, with a `state`, a
  `source`, a boolean `never_alarm` and a `caveat`.
- **`outcomes`** — every member of `decision.Outcome`, the CLOSED set on
  `GET /v1/lane/state`. A consumer branches on all four.
- **`transit_states`** — every member of `contract.TransitState`.
- **`never_alarm`** — the codes a monitor must never page a human on, and the
  reason each is here travels on the wire in that code's `caveat`.
- **`vend_authorities`** — every member of `contract.VendAuthority`, the closed
  set `POST /v1/lane/vend` takes in `authorised_by`. Every one names a decision
  a person or a confirmed display code made.
- **`vend_blocking`** — the subset of `malfunction_codes` that refuses a
  completion, and the whole of it. Every other code, including every one this
  build measures, does not: they concern the reading and the record, not the
  barrier. Four of the five are `no_source` today.
- **`vend_refusals`** — every member of `contract.VendRefusal`, in the order the
  route applies them. Each arrives as a `409` carrying its name in `code`.
- **`vend_identity_kinds`** — the identity kinds that route accepts. **One**,
  this version, and a plate is deliberately not on it.

`reason` and `cause` are **not** here, and that is not an oversight: `reason` is
an OPEN string with a required closed subset, and a lane that is not ours emits
its own vocabulary in it. What a consumer must do with one it does not
recognise — escalate to a human, never map it onto the nearest thing it knows —
is stated above and is the behaviour that makes the openness safe.

## Running it

```sh
lane-controller serve --config lane.toml
```

Binds `127.0.0.1:8090`. **Local by design** — this is meant to run on the same
device as the lane it describes.

**Off loopback it refuses to start without BOTH credentials.** `--host` anything
but loopback requires `--auth-token-file` **and** `--act-token-file`, and with
them every read route requires the first and `POST /v1/lane/vend` requires the
second — see **The two tokens** above.

Two exposures, and they are not the same size. Without the read token, anything
on the lane's LAN learns where a vehicle was, when it was there and what the
lane decided about it. Without the act token, anything on that LAN **opens the
barrier**, and the moment of the bind is the only place that can be refused
before it is true.

**Each token is read from a FILE, never from a flag value.** A value on the
command line is readable by every user on the box for as long as the process
runs — and it is in that user's shell history afterwards.

There is no flag that turns any of that off.

## What is NOT here, stated rather than left to be discovered

- **No resolve route.** What becomes of a HELD entry is decided by nothing on
  this contract. `POST /v1/lane/vend` is the whole act surface.
- **No state store**, and the vend does not add one. `decision`, `transit`,
  `decision.completed` and the idempotency keys are lost on a restart and the
  contract says so, rather than reporting the last thing it happened to
  remember. A key from before a restart is not recognised, and a replay carrying
  one is a NEW attempt subject to every refusal — which at a barrier the car has
  already left is `no_vehicle`.
- **No durability across a kill in the middle of a vend.** The completion path
  ends by flushing the outbox, exactly as an ordinary arrival does, so a
  completed vend reaches the platform without waiting for the next car — and the
  window that is left is stated rather than implied. **A kill between the
  identity being written and the relay being pulsed loses the record and the
  barrier did not move**, which is consistent. **A kill between the relay being
  pulsed and the flush loses the record and the barrier DID move**, and nothing
  can close that window without making the barrier wait on the network. The
  ordinary arrival path has the same window for the same reason, and this
  contract does not pretend otherwise.
- **No way to kill a hung loop driver.** The settle waits at most the
  confirmation window plus `[lane] settle_grace_s`; past that the transit is
  `unconfirmable` with reason `loop_driver_timeout` and the lane accepts vends
  again. The thread waiting on the driver is ABANDONED, not killed — Python
  cannot kill one — so a driver that eventually returns finds the outcome
  already published and records nothing. What that costs is one leaked thread
  per occurrence, which is a fault somebody is being paged about.
- **No measurement of the boom.** `vend_commanded` is what this lane can stand
  behind; whether the barrier rose is `no_source` and stays that way.
- **No verification of a ticket.** Shape only. No signature, no expiry, no
  issuer, and no key on this box.
- **No SIP identity.** Which lane an intercom call belongs to is unbound. It
  belongs with the agent, and binding it here would mean refusing every
  configuration file that exists today for a field nothing yet reads.
- **No display.** `has_display` is `false` everywhere, because there is no
  display seam in this package.
- **Health states for most codes.** Some are derived; the rest answer
  `unknown` and say why in `source`, which is the field that says which — a
  count here would be a second copy of `contract.SOURCES` and the copy is the
  one that goes stale. A monitor that turns these into alerts is a separate
  process and is not this.
