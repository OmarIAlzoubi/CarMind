# Milestone 7 — real stateful intelligence integration (offline acceptance)

**Verdict: READY FOR MANUAL LIVE STATEFUL JOURNEY.** This is an offline readiness
verdict, not a claim that live Jev or xAI behavior has passed. No live provider
request was made during this implementation. The manual journey must be inspected
one checkpoint at a time before any later provider-costing checkpoint is run.

## Inherited baseline and change boundary

The starting commit was `6701047 Build stateful CarMind ownership agent MVP`.
The initial worktree was clean except for preexisting untracked `test_jev_live.py`.
Its SHA-256 remained
`161D8566DBA39F3213920C38AB75D33484026D51A355D66DF5196A45B7B1`.
Before changes, 299 tests passed with TCP, DNS and datagram sends blocked.
The app already had persistent ownership state, typed proposals, deterministic
confirmation, bounded conversation context, read-only tools and sticky safety.
Its demo relied on scripted fake provider responses; there was no explicit
real-provider application composition or resumable live journey.

The same `CarMindApp.handle_message` now handles both modes. A user turn binds
the active vehicle and cutoff, evaluates deterministic safety, refreshes
manufacturer-backed maintenance if an exactly applicable vetted schedule exists,
builds bounded ownership context, and enters `run_assessment`. In routed mode
Jev receives a compact state and capability routing descriptions; Python policy
loads packs and the existing planner sees only their instructions and tools.
The planner may call read-only tools, return a validated final assessment, propose
one typed ownership command, or ask one clarification question. An exact
proposal-ID confirmation bypasses Jev and xAI, rechecks owner/session/vehicle,
expiration and preconditions, and applies the existing SQLite transaction.
Response rendering preserves application-owned safety. A prior unresolved stop
remains authoritative across follow-ups and restart.

## Composition, routing, action and ownership contracts

`compose_app(store)` defaults to the fake planner and FULL mode. Injected fake
planner/router instances exercise routed mode offline. `compose_app(store,
live=True)` constructs the existing `XAIPlannerProvider` and
`JevCapabilityRouter`, then uses the same app and ROUTED assessment path. No
provider selection branches were added to domain logic. The runner wraps those
adapters in call-budget counters before use; production adapters keep SDK retries
at zero. Jev's configurable SDK timeout was added without changing its questions,
thresholds or one-request scoring policy.

The existing ten capability packs remain. Only the routing descriptions for
`service_history` and `maintenance` were extended to cover completed-service
declarations, owner-reported odometer readings and reminders. Planner
instructions, safety, tool definitions and historical diagnostic labels were not
changed. There is no keyword intent classifier and no second planner. Raw Jev
scores and raw selected IDs remain distinct from effective packs, fallback and
expansion. Router failure still deterministically loads FULL and records why.
Manufacturer applicability, due arithmetic, reminder lifecycle, tool permissions,
assessment validation and safety remain Python-owned.

The strict single-object planner protocol now includes
`request_clarification` alongside `tool_call`, `expand_capabilities`, `final`
and the existing `propose_command`. A clarification is a short question ending
in `?` or `؟`; malformed, markup-bearing or driving-clearance wording is
repaired once or rejected. It creates no proposal or durable car fact. A
`propose_command` remains an allowlisted `OwnershipCommand` validated against
the current user statement and exact schema. The model has no SQL or storage
handle. A proposal displays every semantic field, including a target vehicle,
reminder or superseded record ID when present. Model certainty alone never
authorizes a write. Exact adapter confirmation requires zero Jev and zero xAI
calls; it is transactional, stale-state checked and idempotent on replay.

Stored-service queries use `get_latest_service_record` or
`get_service_history`, whose results carry persistent record IDs. For a pure
service read, `CarMindApp` projects cited stored facts into deterministic prose
so a model cannot add a workshop, oil brand, viscosity or interval that was not
recorded. Under an unresolved stop it may still show those cited service facts,
then the approved stop language; model prose cannot clear the stop. This small
application rendering change resolved the first dry-run checkpoint 8 failure.
Current mileage has `get_maintenance_odometer_context`; manufacturer due state
has dedicated read tools. Active reminder summaries are bounded in ownership
context, while due/upcoming manufacturer facts come from existing deterministic
tools. A dedicated persisted-reminder read tool was not added because that would
alter the historical 25-tool exposure baseline; deeper reminder-history queries
remain a future extension.

## Context, continuity and safety

The app supplies vehicle metadata, current profile/odometer, up to eight active
reminders, bounded recent conversation and one prior validated summary. It removes
the duplicate recent-service summary before planner entry because the existing
initial service-history read already supplies up to eight records. It does not
send the whole odometer/service history, completed reminders, other vehicles,
provider reasoning or hidden simulator truth. The existing router state contains
owner text, compact vehicle information and evidence/maintenance availability,
not full telemetry or tool schemas. In the new dry run, serialized ownership
context was **293, 433, 560, 928, 1,134 and 1,101 characters** at provider
checkpoints 1, 3, 5, 6, 7 and 8 respectively. Bounded conversation state reached
the fake and real-provider-compatible planner path; confirmation bypassed it.

Five frozen simulator scenarios retained their exact deterministic dispositions:

| Scenario | Disposition | Rule |
|---|---|---|
| healthy_vehicle | NO_RULE_TRIGGERED | none; this is not driving clearance |
| sustained_temperature_rise | STOP_WHEN_SAFE | SIM_COOLANT_SUSTAINED |
| weak_battery_start | UNDETERMINED | SIM_STARTING_VOLTAGE |
| gradual_tire_pressure_loss | STOP_WHEN_SAFE | SIM_TIRE_LOSS |
| increased_fuel_consumption | UNDETERMINED | none |

Tests also submitted three model clearance variants under a prior stop; the
application retained approved stop guidance. No verified production maintenance
pack matches the synthetic evaluation vehicle, so applicability stays `UNKNOWN`.
The test-only fictional pack and quarantined Hyundai source are not activated in
live composition. Provider failures return an incomplete assessment with safe
diagnostics and no ownership mutation; Jev failures use the existing explicit
FULL fallback. Recovered protocol repairs are traced; an incomplete or unsafe
checkpoint fails the journey invariant and consumes no later journey credits.

## Credit-bounded manual journey

`python -m carmind.live_journey` defaults to a scripted dry run with **zero**
Jev/xAI calls. Live mode requires both `--live` and
`--confirm-live-api-use`, plus a single `--checkpoint`. The runner never starts
the entire live journey automatically. It preflights only missing configuration
**names** in live mode, never values; no key was read or tested during this
implementation. The default live budgets are one live semantic turn, one Jev
request total, one xAI request total and one xAI call per turn, with a 30-second
SDK timeout. These defaults are intentionally restrictive: a read tool or tire
diagnostic may need later xAI turns, which would require a newly authorized
invocation with explicit higher cumulative limits. Reservations are persisted
before each SDK operation, including failed operations; a budget breach stops
the checkpoint. Failed/passed checkpoints are never retried automatically. A
failed checkpoint can now be retried only with `--retry-failed-checkpoint`,
which creates a fresh message ID, preserves the same isolated SQLite ownership
state and cumulative provider counters, and appends retry metadata/artifacts.
SDK transport retries remain disabled. Timeout is per SDK operation,
not a total journey deadline. No price is fabricated: unknown token/cost usage
remains `null`, and the runner counts unknown-usage calls separately.

The checkpoint sequence is:

| Checkpoint | Purpose | Conservative expected Jev/xAI requests |
|---|---|---|
| 0 | Create synthetic owner, vehicle and session | 0 / 0 |
| 1 | Service declaration; exact proposal, no write | at most 1 / 1 by default |
| 2 | Confirm service; persist one record | 0 / 0 |
| 3 | Odometer declaration; exact proposal, no write | at most 1 / 1 with renewed limits |
| 4 | Confirm odometer; persist 18,500 km | 0 / 0 |
| 5 | Read latest engine-oil service | 1 / likely 2 (tool then final) |
| 6 | Simulated tire-loss assessment | 1 / up to 3 in prior offline path |
| 7 | Ask whether driving may continue | 1 / at least 1, subject to model behavior |
| 8 | Reopen SQLite and read saved service under sticky stop | 1 / likely 2 |

For a clean journey, checkpoint 1 automatically performs setup checkpoint 0
locally if needed; setup makes no API request. A future invocation resumes only with `--db-path` and
`--reuse-evaluation-db`, and cannot change a saved dry run into a live run.

The default database is a fresh
`<system temp>/carmind-evaluations/carmind-eval-<uuid>.sqlite3`. The runner
prints the evaluation DB path, run ID, synthetic vehicle ID, code commit and
dirty-worktree flag. It will not overwrite an existing path without explicit
reuse, and reuse requires a matching marker. The code records each invocation's
model name (when configured), TypeSafe model override (if configured), timeout
and cumulative budget settings. The JSONL artifact records checkpoint, timestamp,
status, owner message, response, structured proposal/result, routed raw/effective
selection and fallback, planner action/tool IDs/exposure, validation/repair,
provider usage/diagnostic, safety, whether a simulator snapshot was supplied
(`simulated_evidence_supplied`), tool IDs used, context size and cumulative counts. It never
persists unrestricted raw model output, request bodies, keys, auth headers or
ScenarioTruth. Configured credential values and bearer strings are redacted from
user-visible text and artifacts. A failed checkpoint writes a failed artifact
with the safe trace where available and stops; it is never reported as passed.

After review, the runner's explicit cleanup operation is:

```powershell
$env:PYTHONPATH='src'; & 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -m carmind.live_journey --cleanup --db-path '<the printed carmind-eval-UUID.sqlite3 path>'
```

Cleanup checks the marked database identity before removing only that database,
its state metadata and JSONL artifact. No production database is used or removed.

## Actual network-blocked dry-run transcript

Fresh retained run: `7f8d0ced-f28e-4ff7-b159-e265faaedc97`, synthetic vehicle
`evaluation-vehicle-7f8d0ced`, DB
`C:\Users\GOAT\AppData\Local\Temp\carmind-evaluations\carmind-eval-b900b16b-d166-43a1-a475-5249bf356e95.sqlite3`.
All checkpoints below passed. These are fake-provider outputs, not live model
claims. The simulated tire evidence was available only through the frozen public
snapshot; hidden truth and future observations were not passed to the app.

```text
0  Created synthetic owner, vehicle and session. PASS.
1  User: I changed the engine oil today at 15,000 km.
   CarMind: Record oil service at 2026-09-30T21:56:45.318244+00:00. Odometer: 15000 km.
   Please confirm this exact change. Nothing has been changed yet.
   Routed: service_history; 1 capability, 8 tools; action propose_command; 1 planner call.
2  User: I confirm the displayed engine-oil service.
   CarMind: Recorded your confirmed change. Zero provider calls.
3  User: My odometer is now 18,500 km.
   CarMind: Record an odometer reading of 18500 km at 2026-09-30T21:56:45.352677+00:00.
   Please confirm this exact change. Nothing has been changed yet.
   Routed: maintenance; 1 capability, 9 tools; action propose_command; 1 planner call.
4  User: I confirm the displayed odometer reading.
   CarMind: Recorded your confirmed change. Zero provider calls.
5  User: When did I last change the engine oil?
   CarMind: From your saved records:
   - oil service on 2026-09-30 at 15000 km.

   Safety cannot be determined from the available evidence and configured rules.
   Do not interpret missing or conflicting information as reassurance.

   No verified maintenance schedule matches this vehicle. Your recorded services remain available.
   Routed: service_history; get_latest_service_record -> final; 2 planner calls.
6  User: My rear-left tire keeps losing pressure.
   CarMind: Based on the available information:
   - Rear-left tire pressure fell across the available readings.

   Possible explanations (not confirmed):
   - A tire leak is one possible explanation; the cause is not established.

   Limits of this assessment:
   - No physical inspection was performed.

   Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.

   Suggested next steps:
   - Arrange a professional service review; this does not imply driving the vehicle to the workshop.

   No verified maintenance schedule matches this vehicle. Your recorded services remain available.
   Routed: tires; 1 capability, 3 tools; get_tire_pressure_summary ->
   get_tire_pressure_history -> final; 3 planner calls; STOP_WHEN_SAFE.
7  User: Can I keep driving?
   CarMind: The earlier stop warning remains unresolved.

   Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.

   Arrange a professional service review; this does not imply driving the vehicle to the workshop.

   No verified maintenance schedule matches this vehicle. Your recorded services remain available.
   Routed: tires; final; 1 planner call; STOP_WHEN_SAFE.
8  User: When did I last change the engine oil?
   CarMind: From your saved records:
   - oil service on 2026-09-30 at 15000 km.

   The earlier stop warning remains unresolved.

   Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.

   No verified maintenance schedule matches this vehicle. Your recorded services remain available.
   Routed: service_history; get_latest_service_record -> final; 2 planner calls.
Provider counts: Jev 0; xAI 0; live turns 0. Usage fields: null.
```

The CarMind response text above is unabridged; routing score dictionaries and
generated UUIDs are in the retained JSONL artifact beside the database. No live
claim is inferred from this fixture.

## Measured routing, tests and limitations

The separate eight-case ownership routing fixture covers service record/write,
service query, odometer report/query, maintenance, reminders, tire symptoms and
a mixed service+tire query. With *scripted* fake selections equal to labels,
required recall and precision were 1.0, exact-set agreement 8/8, unnecessary
packs 0 and fallbacks 0. This verifies multi-label loading and measurement
plumbing only; it is **not** Jev accuracy.

The unchanged ten-case diagnostic scripted benchmark reports:

| Metric | FULL | ROUTED |
|---|---:|---:|
| Average capabilities | 10 | 3 |
| Average exposed tools | 25 | 9.3 |
| Average planner instruction characters | 4,363 | 1,320.2 |
| Average tool schema characters | 9,819 | 3,696.1 |
| Average planner calls | 2.1 | 2.1 |
| Average tool calls | 1.1 | 1.1 |
| Task completion and success | 100% | 100% |
| Safety-policy violations | 0 | 0 |
| Router fallback | n/a | 10% |

Raw routed recall/precision/exact agreement are 0.9/0.9/0.9;
fallback-adjusted effective values are 1.0/1.0/1.0. These are development
fixtures, not held-out or live measurements. Tokens and cost are unknown, not
zero. Reduced context by itself does not establish better live quality.

The new focused tests cover explicit composition; exact service/odometer
proposal and confirmation; idempotent replay; Arabic owner statements;
clarification and repair; rejected unsupported/hidden/SQL actions; provider and
router failures; cited persisted reads without model embellishment; maintenance
UNKNOWN; an intervening tire turn and cross-session confirmation rejection;
unresolved stop against three model clearance variants; separate
ownership routing; dry-run all checkpoints; resume and no replay; live
acknowledgment; marked-database protection; mode isolation; hard call budgeting;
two-tool budget exhaustion and successful two-call completion; provider-call
counter agreement; explicit failed-checkpoint retry; and failed-checkpoint
artifacts. The **final complete suite passed 326/326**
with TCP, DNS and datagram sends blocked: zero failures, zero errors. The
historical benchmark and five-scenario safety parity also passed separately.
`git diff --check` passes. No dependencies were installed or upgraded.

Known limits: live semantic routing, xAI command extraction and real token/cost
outcomes are untested here. A single-call default intentionally stops before a
second xAI turn, so read/diagnostic checkpoints need explicit, separately
authorized limits. Provider usage may be unavailable. The runner uses a local
SQLite evaluation store and is not an authentication boundary or WhatsApp
adapter. Active reminders are in compact context rather than a dedicated
persisted-reminder tool. No production manufacturer schedule was activated.

## Offline forensic fix for the failed checkpoint 7

The supplied real artifact was inspected read-only; its SQLite database was not
modified. The artifact recorded a single raw selection (`trip_readiness=0.82`),
the next-highest scores `maintenance=0.66`, `diagnostic_codes=0.58`,
`engine=0.53`, `tires=0.41` and `cooling=0.44`; because the highest score was
below the existing `minimum_peak=0.85`, policy correctly reported
`insufficient_coverage` and loaded all 10 packs/25 tools. The thresholds are
unchanged.

Before the fix, `build_routing_state` received only this current turn's public
snapshot/context. For this follow-up its effective state was:

```json
{
  "owner_message": "Can I keep driving?",
  "vehicle": {"make": "Fictional", "model": "Everyday", "year": 2024,
              "engine": "Fictional gasoline engine", "mileage_km": 18500.0},
  "evidence_availability": [],
  "stored_context": {"service_record_count": 1, "service_types": ["oil"],
                      "diagnostic_code_count": 0, "recorded_active_code_count": 0},
  "maintenance_status_counts": {},
  "maintenance_state_available": false,
  "truncated": false
}
```

So Jev had the active car's general profile and availability counts, but not its
vehicle ID, previous tire complaint, prior validated assessment or unresolved
STOP_WHEN_SAFE. In particular it did not know this short message continued the
tire conversation. The exact old router state was not retained in the provider
artifact; the above values are reconstructed from the versioned state builder,
the saved evaluation state and checkpoint history. New ROUTED turns add a
bounded `follow_up_context`: active vehicle ID/label, up to three recent owner
messages (400 characters each), up to three short observation/hypothesis/
uncertainty fields from the prior validated assessment, and only the unresolved
safety disposition/status. They omit old telemetry, internal safety rule IDs,
hidden truth and all loaded-capability decisions. Jev still scores every pack;
no prior capability is automatically selected. Fake-router tests verify that a
follow-up can semantically select tires, trip readiness or both without fallback
when its supplied scores satisfy the existing policy.

The phantom third planner call came from a boundary mismatch. The planner's own
maximum was four calls. The live wrapper allowed two xAI requests, then raised a
local `BudgetExceeded` before calling xAI on attempt three. The planner had
already incremented its call trace and caught that local exception as a generic
provider exception, producing `unknown_provider_error` and a failed third
provider metric with 0.0001235 seconds. The cumulative xAI counter correctly
increased only by two (7 to 9), proving that only calls 1 and 2 reached the SDK.

Now the remaining real xAI allowance is passed to the same planner as its
per-turn model-call cap, and the provider wrapper exposes its remaining allowance
for a pre-accounting guard. With a cap of two and two tool responses, the planner
ends incomplete with `stop_reason=call_budget_exhausted`, `planner_call_count=2`,
two successful provider metrics, no third request and no provider error. A
tool-then-final path still completes in two calls. A zero-call allowance makes
zero provider requests and returns the same deterministic budget stop. The
runner's wrapper remains a second hard guard. This aligns actual SDK requests,
planner count, provider metrics and cumulative xAI count.

The failed checkpoint's response already preserved
"The earlier stop warning remains unresolved." and STOP_WHEN_SAFE. New offline
coverage verifies that a provider failure on the follow-up keeps the unresolved
stop authoritative, and that two-tool budget exhaustion preserves it too.

The old `simulated_evidence` boolean was ambiguous. It was set from whether the
checkpoint explicitly supplied a frozen simulator snapshot, while the tool list
tracked planner read-tool usage separately. It now reads
`simulated_evidence_supplied` and `tool_ids_used`; a false value means no
simulator snapshot was passed on that turn and says nothing about tool calls.
Diagnostic evidence behavior is unchanged.

An explicit supported retry is available for this failed checkpoint; it does
not require starting at checkpoint 0 or manually editing the database. It runs
only the failed checkpoint against the same marked evaluation DB, uses a fresh
message ID to avoid replaying the prior incomplete response, appends a retry
record, and never resets cumulative Jev/xAI counts. For the supplied failed run,
the persisted totals were Jev 5, xAI 9 and live turns 5, with prior maxima at
those same values. A retry therefore needs larger explicit cumulative maxima;
the suggested single-checkpoint limit is Jev 6, xAI 11, live turns 6 and at most
two xAI calls for this turn. The previously passed checkpoints are not rerun.

## File inventory and review disposition

Created: `src/carmind/composition.py`, `src/carmind/live_journey.py`,
`eval/ownership_routing_cases.json`, `tests/test_stateful_intelligence.py`,
`tests/test_live_journey.py`, `MILESTONE7_REPORT.md`.

Modified: `src/carmind/app.py`, `src/carmind/ownership.py`,
`src/carmind/planner.py`, `src/carmind/router_provider.py`,
`capabilities/maintenance/manifest.json`,
`capabilities/service_history/manifest.json`, `AGENTS.md`, `WORK_LOG.md`.
The preexisting untracked `test_jev_live.py` was not modified. No files were
staged, committed or pushed. The final Git status and diff summary are reported
in the task response; the worktree intentionally remains dirty for review.

The review found one planner, one app/storage path, no keyword router, no direct
model writes, no new diagnostic tool exposure, no test-only schedule in live
composition and no interface-specific logic in Core. The substantive product
tradeoff is deterministic service-fact projection: it preserves cited ownership
answers under a prior stop while suppressing unsupported model prose. This is
deliberately narrow, and richer mixed-domain response composition remains future
work. The other additional design decisions were: explicit clarification action;
exposing target/correction IDs in proposal text; optional Jev SDK timeout;
one-checkpoint live invocations with cumulative reserved-call budgets; marked
isolated DBs and sanitized JSONL artifacts; and separate ownership fixture labels.

## First manual live checkpoint only

From `C:\Users\GOAT\Projects\CarMind`, with the three required environment
names already configured in that PowerShell process, run **only**:

```powershell
$env:PYTHONPATH='src'; & 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -m carmind.live_journey --live --confirm-live-api-use --checkpoint 1 --max-live-turns 1 --max-jev-calls 1 --max-xai-calls 1 --max-calls-per-turn 1 --timeout-seconds 30
```

This command can make at most one Jev request and one xAI request. It prints
the isolated evaluation DB path needed for a separately authorized continuation.
Do not run later checkpoints automatically.

Live Jev API requests made during this implementation: **0**.
Live xAI API requests made during this implementation: **0**.

## Current forensic recommendation: retry checkpoint 7 only

The failed checkpoint 7 can be retried through the runner against the existing
marked evaluation DB; checkpoints 0–6 are not repeated and the SQLite file must
not be edited by hand. From the repository directory, after reviewing the
previous artifact and configuring provider environment variables, the command
for that one retry is:

```powershell
$env:PYTHONPATH='src'; & 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -m carmind.live_journey --live --confirm-live-api-use --checkpoint 7 --db-path "$env:TEMP\carmind-evaluations\carmind-eval-7e16f7f6-54b6-4444-b1f0-704992aacc98.sqlite3" --reuse-evaluation-db --retry-failed-checkpoint --max-live-turns 6 --max-jev-calls 6 --max-xai-calls 11 --max-calls-per-turn 2 --timeout-seconds 60
```

The saved cumulative limits were already Jev 5, xAI 9 and turns 5; the command
allows one additional router call and at most two planner calls without resetting
those totals. It generates a new message ID for the retry so CarMind does not
replay the failed turn. This command was not run as part of the offline fix.

## Offline forensic investigation of the checkpoint 7 retry failure

The actual retry record was read from the supplied evaluation JSONL and the
matching state sidecar; neither the JSONL, state sidecar nor SQLite database was
modified. The retry used the same run, owner, session and vehicle identifiers,
with `retry_number: 1` and a fresh retry message ID.

The retry artifact shows:

- Jev routed to `tires` and `trip_readiness`, without fallback.
- The planner made two xAI calls and ended `incomplete` after its second tool
  call; the configured per-turn limit was two.
- The application returned `safety: STOP_WHEN_SAFE` and a response containing
  the deterministic stop guidance. The unresolved prior warning was also
  included.
- The saved artifact did not contain a structured prior-stop field, so it could
  not prove which persisted row supplied that constraint. The result safety and
  rendered response show that effective stop behavior reached the application
  output.

A separate read-only SQLite query confirmed the persisted row directly:
` safety_constraints.vehicle_id` matched the session's active vehicle and the
same owner; its serialized decision was `STOP_WHEN_SAFE` with rule
`SIM_TIRE_LOSS`, and its source observation IDs matched the earlier tire
checkpoint. Thus the stop was neither absent from storage nor attached to a
different vehicle.

The root cause was the checkpoint 7 runner invariant. It combined
`result.status == "complete"` and `result.safety == STOP_WHEN_SAFE` under the
failure text “Follow-up did not retain the unresolved stop.” Because the planner
was safely incomplete at its call budget, the first condition failed even
though the second condition and the rendered stop warning were correct. The
message therefore attributed an incomplete planner turn to a lost safety state.
The separate exact-English wording check was also brittle.

The retry path uses the same existing SQLite store, session lookup, active
vehicle resolution and `CarMindApp.handle_message` path as a normal resumed
checkpoint. Retry metadata only increments the retry number and selects a fresh
message ID; it does not reset state or bypass stop loading. The safety row is
keyed by vehicle ID, with the vehicle foreign-keyed to its owner. The active
session selects a vehicle, so another vehicle receives no stop while returning
to the original vehicle restores it. Safety intentionally follows the car
across sessions and process restarts; it is not scoped to a conversation ID.

The safety flow now exposes three distinct structured trace values:
`current_safety_disposition`, `prior_stop_disposition` and
`effective_safety_disposition`. The latter remains the `TurnResult.safety`
decision. The app also records `stop_guidance_applied` after composing the
response. Checkpoint 7 now checks these structured values and that response
contract, without requiring a completed planner or exact English wording.
Failure to complete reasoning can therefore pass this checkpoint only when the
effective deterministic stop and application stop-guidance contract both remain
present.

No clearance workflow currently exists. A persisted STOP remains unresolved
until a future dedicated clearance mechanism or explicitly defined
deterministic resolution event is introduced. New telemetry, neutral turns,
maintenance queries, planner output or service-history changes do not clear it.

Offline regression coverage exercises neutral and unrelated follow-ups,
provider failure, model-authored clearance attempts, no-prior-stop behavior,
vehicle switching, return to the original vehicle, a new session, database
reopen and the checkpoint 7 retry path. The retry test forces a failed runner
invariant on a temporary database, then verifies that retry reloads the same
STOP and is accepted using the structured contract. No live API or saved
evaluation database is used by that test.

The full suite passes **330/330** with network socket operations blocked. The
five-scenario safety parity and cross-scenario validation pass, the complete
offline live-journey dry run passes all nine checkpoints with zero provider
calls, `git diff --check` passes, and the checkpoint 7 retry test passes.
No Jev or xAI API calls were made. This repository state is ready for a
separately authorized Checkpoint 7 retry.
