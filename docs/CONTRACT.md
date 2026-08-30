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

## This version is READ ONLY

Four routes, all `GET`. There is **no vend route and no resolve route**: nothing
here opens a barrier, closes a session, or changes anything at all. Every other
method is answered `405` with `Allow: GET`.

That is not an omission to be filled in quietly. The act surface is a later
round, deliberately after the display and the agent, because it is the first
thing that can open a barrier — and a new route to a vend is the boundary every
outside reviewer of this project has named. `capabilities.can_vend` exists so a
consumer can **ask**, and it is `false` here.

## Compatibility

`contract_version` is `1`, and every payload carries it.

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
  "contract_version": 1,
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
    "can_vend": false
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
| `can_vend` | Whether this lane exposes a route that opens a barrier. `false` for the whole of this contract version, derived from the service's own act-route table — so a vend route cannot be added without this answer changing with it. |

## `GET /v1/lane/state` — the last decision, and the current transit

<!--payload:state-->
```json
{
  "contract_version": 1,
  "decision": {
    "outcome": "fallback",
    "reason": "engine_unreachable",
    "fallback": "engine_unreachable",
    "cause": "unreachable",
    "presence": true,
    "at": "2026-08-30T14:03:11.482913+00:00",
    "read_ref": null
  },
  "transit": {
    "state": "pending",
    "since": "2026-08-30T14:03:11.482913+00:00"
  }
}
```

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
  "contract_version": 1,
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

- `arming_loops_disagree` and `closing_loops_never_firing` — the lane already
  writes an event per vehicle (`arming_incomplete`, `entry_held` / `exit_held`).
  What does not exist is the aggregation that turns a RUN of them into a fault,
  and nothing has measured how many in a row a run is. A threshold invented here
  would be a number nobody measured, applied at every site;
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
  "contract_version": 1,
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

## The closed sets, in full

Everything below is a set this contract declares CLOSED, published here so that
a lane which is not ours can be written **from this document alone**. It used to
say it did not list them, on the reasoning that a hand-written copy of a set the
code defines is the copy that goes wrong. The reasoning is right and the
conclusion was wrong: withholding them did not remove the second copy, it moved
it into every implementer's guess — and the stub in this repository that exists
to prove a stranger can take this seat had to import our Python package for
exactly these four sets.

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

**Off loopback it refuses to start without a credential.** `--host` anything but
loopback requires `--auth-token-file`, and with a token every route requires
`Authorization: Bearer <token>` and answers `401` without it. The exposure that
rule exists for is real: on a lane's own LAN this publishes where a vehicle was,
when it was there, and what the lane decided about it.

**The token is read from a FILE, never from a flag value.** A value on the
command line is readable by every user on the box for as long as the process
runs.

There is no flag that turns any of that off.

## What is NOT here, stated rather than left to be discovered

- **No act surface.** No vend, no resolve, no route that changes anything.
- **No state store.** `decision` and `transit` are lost on a restart and the
  contract says so, rather than reporting the last thing it happened to
  remember.
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
