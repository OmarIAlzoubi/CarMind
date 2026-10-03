# Milestone 6 — Ownership agent MVP

## Final offline acceptance audit — 2026-09-30

**Recommendation: READY TO COMMIT MILESTONE 6 as the local architectural baseline.**
The audit found and corrected two user-visible safety/staleness gaps and one
confirmation race. The full offline suite passes. This recommendation does not
mean CarMind is ready for a real-car safety deployment: configured vehicle safety
thresholds are simulation-only, there is no clearance workflow, and a future
transport must authenticate owner identity before calling the app.

### Findings and corrections

- A prior `STOP_WHEN_SAFE` was only forcing an approved response when a follow-up
  had no telemetry. A fresh but unrelated healthy snapshot could therefore leave
  model-authored driving-clearance prose visible. The app now projects only
  approved safety guidance while any prior stop remains unresolved, regardless
  of fresh unrelated evidence. The assessment validator also rejects common
  direct driving-clearance phrases. Neither new evidence outside the deterministic
  rule nor a service-history write clears the stop.
- Confirmation previously read a pending proposal before opening the write
  transaction. `_confirm` now reads and applies under the same `BEGIN IMMEDIATE`
  transaction, so duplicate confirmations serialize and replay the stored result.
- Vehicle profile proposals now store the original field value as an application-
  owned precondition. Confirmation rejects a stale proposal if that value changed
  while it was awaiting review. This complements the existing owner/session/
  vehicle/proposal binding and apply-time odometer/service/reminder checks.
- These are narrow corrections; no capability selection, Jev scoring, planner
  budgets, maintenance arithmetic, safety thresholds or provider settings changed.

### Routing and safety audit

The Milestone 6 `routing.py` diff adds optional ownership context and a prior
deterministic stop argument to the shared `run_assessment` call. It does not
change FULL versus ROUTED selection, Jev scoring, routing thresholds, fallback,
or benchmark labels. The offline paired benchmark remains byte-for-byte
behaviorally equivalent in its aggregate outputs: FULL averages 10 capabilities,
25 tools and 4,363 instruction characters; ROUTED averages 3 capabilities, 9.3
tools and 1,320.2 instruction characters. Both retain 100% completion/task
success, full required evidence coverage and zero safety violations. Routed raw
recall/precision/exact-set agreement remain 0.9/0.9/0.9; effective selection is
1.0/1.0/1.0 after its 10% deterministic fallback rate. The result is a scripted
development fixture, not evidence of live Jev or model quality.

The `safety.py` diff adds only `retain_unresolved_stop`, which preserves a prior
deterministic STOP as an unresolved constraint, and imports `dataclasses.replace`.
`evaluate_safety` and its thresholds, precedence, dispositions and coverage-gap
semantics are unchanged. The five scenario dispositions at seed 42 remain:
healthy `NO_RULE_TRIGGERED`, temperature rise `STOP_WHEN_SAFE`, weak battery
`UNDETERMINED`, tire loss `STOP_WHEN_SAFE`, and fuel consumption `UNDETERMINED`.
`NO_RULE_TRIGGERED` continues to mean that no configured rule fired, not that
driving is safe.

### Ownership, commands and persistence

Two vehicles for one owner remain isolated by vehicle-scoped queries and
`session.active_vehicle_id`. The acceptance journey and focused tests verify that
service records, odometer state, reminders, conversation context and proposals
do not cross from A to B. Switching vehicles is explicit; switching to B does not
carry A's stop, while switching back to A restores its unresolved stop. A proposal
made for A is rejected while B is selected. Proposals are bound to owner session,
original vehicle, exact stored command and proposal ID; profile updates also
check the captured field precondition. An outsider without an active vehicle
cannot confirm the proposal.

Plain “yes” has no authority without the adapter-supplied confirmation ID. A
missing, expired, wrong-session, wrong-vehicle, stale or ambiguous proposal fails
closed. Multiple pending proposals retain separate IDs. Replaying an applied
service, odometer, reminder-acknowledgement or correction proposal returns its
stored result without a second mutation; pending and completed confirmation
behavior survives reopening the database. Corrections append a replacement and
retain the superseded record for audit. Odometer timeline conflicts reject and
roll back the complete transaction. The same accepted mileage can occur at
different timestamps; an identical reading attached to a service at the same
time is allowed as corroborating event data.

Mutation, correction/supersession, deterministic maintenance recomputation,
reminder lifecycle update and stored command result are applied in one SQLite
transaction. Injected refresh failure leaves neither partial ownership facts nor
reminder changes and leaves the proposal pending. SQLite `BEGIN IMMEDIATE` also
serializes competing confirmation writers. The store has one local connection and
is not a thread-sharing abstraction; a future server adapter should use a
connection-per-unit-of-work or serialize access.

SQLite uses foreign keys, primary and unique keys, non-null columns, allowed-unit
and nonnegative-distance checks, plus indexed vehicle timelines. `PRAGMA
user_version=1` initializes a new database; unknown versions fail closed (also
covered with a synthetic version-99 database). The app validates that the active
vehicle belongs to the session owner. SQLite's individual foreign keys do not
enforce that owner/active-vehicle composite relationship or self-reference
correction targets; app transactions validate these references. A future schema
can add composite/self foreign keys when migration requirements are known. No
Alembic or migration framework is warranted for this V1 schema.

### Facts, reminders and context

Only explicit owner-declared, allowlisted commands become durable ownership facts
after confirmation. Uncertain wording requires clarification; hypotheses such as
“possible tire leak” remain unconfirmed assessment context and never become
service records. The parser requires a verbatim supporting owner quote but that
is not a semantic truth check, so the separate confirmation remains essential.
Odometers normalize explicit `mi` to canonical km, allow zero and finite large
values, reject negative/non-finite/overflow values and unknown units, and reject
future or contradictory timeline entries. Same-value readings at distinct times
are valid. No unit is guessed. Services may omit mileage and be historical, but
future completed-service claims are rejected. Corrections preserve previous
rows and refresh current maintenance state.

The deterministic test-only maintenance journey exercises NOT_DUE/absent,
UPCOMING, DUE, OVERDUE, service completion and correction-backward closure, as
well as acknowledgement, restart and duplicate-cycle behavior. Reminders require
exact manufacturer applicability and a vetted configured source. The fictional
schedule requires explicit test-only opt-in; quarantined Hyundai/Thailand data,
unknown years/markets and missing source applicability do not produce reminders.
The reminder refreshes on app turns; there is no background scheduler or delivery
adapter.

Persistent chat is deliberately bounded to six turns. Initial service context is
limited to eight records and reminder context to eight; the planner receives a
bounded ownership summary rather than every service/odometer row. Read tools can
fetch older relevant service history, while deterministic maintenance sees the
full accepted history. Recent assessments retain a few truncated structured
claims and unconfirmed hypotheses, not raw telemetry. In the demo, the ownership
context measured 1,353 characters. No vector memory or infinite transcript is
stored.

The database stores owner messages, confirmed service notes, odometer/service
provenance, bounded turn summaries, proposals/results, reminders and opaque
unresolved safety constraints. It does not store API keys, provider headers/raw
responses, hidden `ScenarioTruth` or full telemetry samples. Local SQLite is not
encrypted and the app is explicitly not an authentication boundary. A real
WhatsApp/API adapter must authenticate owner identity, protect the database and
present/confirm exact proposals; this work adds no transport, encryption or
retention/deletion policy.

### Boundaries, errors and performance

`CarMindApp.handle_message(owner_id, session_id, message, now, ...)` is the
transport-independent boundary. A future adapter supplies authenticated owner,
session/selected vehicle, message and timestamp, and receives response, state and
proposal/result objects. It does not need direct planner, SQL, maintenance or
safety access. `__main__.py` is only an offline demonstration adapter; the demo
calls the app and marks its fictional maintenance pack TEST_ONLY / NON_PRODUCTION.

Offline error tests cover absent selection, unknown vehicle/session, invalid and
future messages, unknown/expired confirmation, malformed commands, provider and
validator failure, stale references, database closure/path-level SQL errors and
reminder-refresh rollback. Exceptions are reduced to safe categories/messages;
raw provider and SQL details are not returned to the owner or recorded in
application traces. Traces distinguish proposal, application, rejection, replay,
reminder changes, maintenance refresh, routing/planner, safety and response status.

Seven-run median local measurements (milliseconds; illustrative only): new store
startup 3.327, simple app query with a fake provider 13.368, service-history SQL
read 0.269, reminder refresh 0.289, and persistence reopen 1.137. No strict
latency target or optimization is justified by these local measurements.

The end-to-end offline acceptance test records A's maintenance cycle, diagnoses a
tire stop, follows up without clearance, creates a stale A proposal, selects B,
confirms B's own odometer, rejects the stale A confirmation, replays B after
restarting, then returns to A and verifies the stop remains. The standalone demo
also ran successfully. No network calls occurred.

### Final audit validation

- Baseline before audit edits: 290 tests passed.
- Final network-blocked suite: **299 tests passed, 0 failures, 0 errors**. TCP
  connect/connect_ex, DNS lookup/connection helpers and datagram sendto were
  blocked for the process. No live provider tests were run.
- Offline benchmark: 10 paired development cases, unchanged summary above; no
  token or cost values were fabricated where fake providers report none.
- Offline CLI demo: exit status 0; maintenance, confirmed write, tire assessment,
  sticky stop follow-up and database restart all completed.
- `git diff --check`: clean.
- `test_jev_live.py`: unchanged from its pre-audit SHA-256
  `161D8566DBA39F3213920C38AB75D33484026D51A355D66DF5196A45A545B7B1`.
- No files were staged, committed or pushed. No dependencies were installed.

Audit-specific edits are limited to `assessment.py`, `app.py`, `ownership.py`,
`storage.py`, `tests/test_ownership_app.py`, this report and `WORK_LOG.md`.
Remaining product limitations are intentional: no explicit safety-clearance
workflow, no real-car safety thresholds, no diagnostic-code ownership write
command, no authentication boundary, no secure deletion/encryption, and no
background reminder delivery. These do not prevent this local milestone from
serving as the reviewed architecture baseline, but they must be resolved before
real-vehicle or external-user deployment.

## Pre-implementation architecture note

Baseline: `b634fc9`, with 248 offline tests passing. The only pre-existing
untracked file is `test_jev_live.py`, which is outside this change.

Reuse `VehicleProfile`, `UserMessage`, `MaintenanceRecord`, `VehicleContext`,
`FrozenEvidenceSnapshot`, `MaintenanceReminder`, `SafetyDecision` and
`ValidatedAssessment`. Existing capability packs, tools, routing policy, planner,
provider, assessment validation and manufacturer arithmetic remain authoritative.
`ConversationContext` currently holds only recent messages; a bounded persistent
session will supply application context alongside the existing planner inputs.

Add three boundaries: typed ownership commands, a small SQLite store, and
`CarMindApp`. SQLite owns durable records and atomic writes; the application owns
confirmation, context assembly and reminder refresh. Interfaces call the app.
The existing planner gains an optional command-proposal action only when invoked
by the application. It never receives a database handle or write tool.

All proposed writes require a separate, explicit adapter confirmation of the
exact stored proposal. Model assertions of explicitness are insufficient to
authorize writes. Corrections append replacements and supersede earlier events.
Canonical distance is km, with explicit miles conversion and monotonic timeline
validation. Ownership facts and bounded conversation summaries remain separate.

Manufacturer schedules are configured by trusted application code, never selected
or authored by a model. Unknown applicability creates no due reminders. Fixture
packs require an explicit test-only opt-in. Reminder lifecycles wrap the existing
maintenance engine; they do not duplicate interval arithmetic. A previously
issued stop disposition remains an unresolved safety constraint across follow-ups
and restarts; a model or a recorded service cannot clear it.

No semantic keyword router, second planner, background worker, live provider call,
or new package is needed. Detailed implementation and validation results follow
after the vertical slice is exercised.

## Engineering conclusion

CarMind now has a persistent local ownership application, not just a stateless
diagnostic runner. The same application accepts ordinary owner messages, exposes
bounded context to the existing planner, returns reviewable changes, applies
explicitly confirmed facts transactionally, and refreshes source-backed reminders.
The scripted offline journey includes service-history retrieval, diagnostics,
follow-up safety and reopening the database. No claim of live language-model
quality or production automotive readiness is made.

Baseline inspection found HEAD `b634fc9` (`Minimize final assessment ownership and
payload`). All 248 inherited tests passed before edits. There were no tracked
changes; `test_jev_live.py` was already untracked. AGENTS, WORK_LOG, both Milestone
5 reports and the current domain, planner, routing, tools, maintenance, safety and
test code were reviewed before implementation.

## Architecture before and after

Before: supplied snapshot/context -> FULL or ROUTED capability selection -> shared
planner -> read tools -> validator -> response. State was supplied by fixtures or
callers; there was no durable ownership application.

After:

```text
CLI demo / future transport
            |
            v
CarMindApp.handle_message(owner_id, session_id, message, now=...)
  |       |                |
  |       |                +-- SQLite: facts, sessions, proposals, reminders
  |       +-- deterministic manufacturer matching / maintenance refresh
  +-- frozen optional observations + bounded ownership context
            |
       existing FULL / ROUTED -> existing planner -> existing read tools
            |                          |
       validated assessment       typed command proposal
            |                          |
       response + safety         exact owner confirmation
                                       |
                              transaction: fact + reminder refresh + result
```

`CarMindApp` is transport-independent. SQLite contains no diagnostic decisions;
the CLI does not parse automotive phrases. There is one planner, one assessment
validator, one maintenance calculator and the existing deterministic safety
engine. No capability descriptions, thresholds, routing labels or provider
configuration changed.

Compatible extensions to existing code are optional `ownership_context` and
`previous_stop` inputs to the shared execution path, an optional
`PlannerResult.proposed_command`, and a safety-continuity helper. Without these
application inputs, the inherited planner behavior and initial context are
unchanged. The ownership protocol suffix and command schemas are exposed only
in application mode. Planner budgets remain four model calls / six tool calls,
with existing repair and expansion limits.

## Domain and SQLite design

The existing `VehicleProfile`, `UserMessage`, `MaintenanceRecord`, `VehicleContext`,
frozen evidence, maintenance result and assessment contracts are reused.
`OwnedVehicle` adds owner, trim, market, nickname, preferred unit and timestamps
around `VehicleProfile`; it does not replace the diagnostic vehicle model.
`OwnershipCommand`, `CommandProposal`, `MutationResult` and `OwnershipContext`
describe the new boundary. No generic ownership-fact or vector-memory subsystem
was introduced.

SQLite uses one connection, parameterized values, foreign keys, indexes on event
timelines, primary/unique keys and explicit `BEGIN IMMEDIATE` transactions.
`PRAGMA user_version=1` initializes a new database; unknown versions fail closed.
The store is not shared across threads. Adapters choose a durable local path;
the demo uses a temporary file and actually closes/reopens it.

| Table | Purpose / retained data |
| --- | --- |
| `owners` | Stable ID and UTC creation time |
| `vehicles` | Owner FK, identity, optional VIN/engine/trim/market/nickname, unit and timestamps |
| `odometer_events` | Original reading/unit, canonical km, occurred/created times, confirmed provenance, message ID and supersession links |
| `services` | Existing service-record fields plus vehicle, provenance, creation and supersession |
| `sessions` | Owner and selected vehicle; multiple vehicles are supported |
| `turns` | Last six conversational turns per session, bounded response and validated summary |
| `commands` | Exact pending proposal, session/vehicle scope, expiry, unique source-message identity and idempotent result |
| `reminders` | Stable lifecycle ID, vehicle, source-backed facts, lifecycle/reason and timestamps |
| `safety_constraints` | Unresolved deterministic stop decision and assessment time; no raw telemetry |

IDs are opaque UUIDs for owners/sessions/proposals/events. Reminder cycle IDs use
a stable digest of vehicle + source + rule + last service. Source event and
evaluator labels are never used as ownership facts. Odometer is derived from the
latest accepted non-superseded reading, not a model-supplied cached number.

## Read/write authority and corrections

Read tools still receive a `VehicleContext` and frozen snapshot. The store adapts
persisted services/profile to those contracts. `get_service_history`, latest
service, profile and maintenance tools remain unchanged; no duplicate read-tool
catalog was added. Initial history is limited to eight services in application
mode. An explicit read-tool request can fetch older history. The maintenance
engine receives the complete accepted service history so context truncation cannot
change a due calculation.

The model may produce **one typed proposal instead of a final assessment**:

- `update_odometer`
- `record_service_event`
- `set_vehicle_profile_field`
- `select_vehicle`
- `acknowledge_reminder`

The parser rejects unknown fields, command kinds, category names, invalid numbers,
units, dates and profile fields. Proposals must quote the current owner's words.
This quotation check is not a semantic truth check. Every explicit proposal still
requires a separate confirmation ID, supplied by the interface after displaying
the exact proposed values. Neither a model `confirmed` flag nor a plain chat
message saying “yes” grants write authority. An uncertain proposal asks for a new
explicit statement and produces no confirmable mutation.

Confirmation is scoped to the original session and vehicle and expires after 24
hours. Changing the active vehicle invalidates an unexecuted proposal for the old
selection. Replayed confirmed commands return their stored result without another
provider call or duplicate event, including after reopening SQLite. Rejected
attempts return structured failures; a transient storage failure leaves the
proposal pending for deliberate retry. New message IDs are not treated as semantic
duplicates of an older statement.

`MutationResult` reports applied/reason/entity/changed fields/time/provenance.
Confirmed owner data is **owner-reported**, not independently verified vehicle
truth. Model hypotheses remain only in bounded conversation summaries, explicitly
labelled `unconfirmed_hypotheses`. No command exists to store arbitrary diagnoses,
SQL, a maintenance interval or a safety disposition.

Corrections use `supersedes_id` on the two event commands. Old rows retain a
supersession timestamp and replacement ID. A service's associated odometer reading
must be corrected through that service. No destructive model command exists.

## Odometer and service behavior

All processing times must be UTC-aware (other offsets normalize to UTC). Domain
logic receives explicit `now`; it does not read wall-clock dates. Backdated event
times are supported, but application processing cannot move backward behind the
current stored vehicle state and expose future conversational/ownership state.

Canonical distance is km. Explicit `mi` converts with 1.609344; ambiguous units,
negative, boolean, NaN and infinite values are rejected. Initial, current and
historical readings are accepted only when consistent with both chronological
neighbors. A conflicting reading at the same timestamp also fails. A lower later
reading returns structured `odometer_conflict`, preserving the previous state.
Corrections are checked against the remaining timeline in the same transaction.

The small service taxonomy is `oil`, `oil_filter`, `tires`, `battery`, `brakes`,
`coolant`, `air_filter`, `other`. It is not a manufacturer schedule. An explicitly
confirmed service odometer creates a linked reading, so the stated service date
and distance participate in the same timeline. A service without an odometer does
not invent one. Future completed services and future readings are rejected.

## Manufacturer, maintenance and reminders

The application defaults to **no approved schedules**. It never selects documents
from model text or activates the quarantined Hyundai pack. Configured schedules
must exactly match stored make/model/year/market/engine and the existing knowledge
profile. Multiple matching configured schedules fail closed. Unknown market or
unavailable applicability yields `maintenance_applicability=UNKNOWN`, an empty
applicable-rule result and no due reminder. With a matching rule but no known
service baseline, the existing engine returns `UNKNOWN` for that item.

Vetting a real source remains an explicit trusted configuration responsibility;
the local app does not certify a document's authenticity. A production pack must
not be PoC-only. A fixture pack additionally requires both the
`TEST_ONLY_NON_PRODUCTION_FICTIONAL` source marker and explicit test opt-in. The
new fixture is fictional, in a test-only market, and loaded only by tests/demo.
The real Hyundai source remains quarantined even when fixture mode is enabled.

The existing maintenance engine owns all interval arithmetic and statuses:
`NOT_DUE`, `UPCOMING`, `DUE`, `OVERDUE`, `UNKNOWN`. FULL and ROUTED application runs
produce identical maintenance results for paired ownership inputs.

Refresh occurs on ordinary queries and after confirmed mutations, with no worker
or notification transport. A lifecycle ID stays stable across UPCOMING/DUE/OVERDUE
changes. Repeated unchanged refreshes produce no new reminder row or change event.
Acknowledgement remains attached to the same cycle. A new matching service, lost
applicability, or a no-longer-due state closes the old reminder. `COMPLETED` means
the reminder lifecycle closed, **not proof that a service was performed**; the
stored closure reason distinguishes source unavailability from a replaced cycle.
Acknowledgement does not record service. Current maintenance facts remain visible
on queries, including acknowledged cycles; delivery/suppression policies are not
implemented.

The fact write, any supersession, derived odometer, reminder refresh and command
result commit atomically. An injected refresh failure rolls all of them back.
An independent safety warning can still be retained even if a fact write fails.

## Conversation, safety and privacy

The prompt contains six recent user turns at most, one compact validated summary,
eight recent services (in the existing service-history slot), eight reminder
summaries and small vehicle metadata. The profile is already present in the
existing planner input; it is not duplicated in the ownership block. Summaries
are conversational context, not new citable observations. Tool evidence and
cutoff validation remain authoritative.

Retention is count-bounded: up to six turns per session; user text at most 2,000
characters, stored response at most 4,000, and up to three 512-character previous
observation summaries. Historical fact rows and confirmation audit records remain
durable. Completed/expired proposals release their raw owner quotation. Expiry
cleanup runs on application turns, not in a background task. SQLite pruning is
logical deletion, not forensic secure erasure; no encryption or backup retention
service is provided.

No raw provider payloads, headers, API keys, simulator truth or raw telemetry are
written by the ownership path. Conversation summaries may describe earlier
observations; they do not populate ownership events. Safety constraints retain
only the earlier deterministic decision, opaque supporting IDs and timestamp.

An earlier `STOP_WHEN_SAFE` remains unresolved across follow-ups, new sessions and
restart. It cannot be cleared by a service record or model output. With no fresh
telemetry, a follow-up presents only the approved safety wording and permitted
actions, preventing arbitrary model-authored prose from becoming driving
clearance. Switching cars retrieves that car's own constraint. Clearing a warning
after a trusted review is deliberately not implemented in this milestone.

## Application API and observability

```python
store = OwnershipStore(local_database_path)
app = CarMindApp(store, injected_provider, mode=ExecutionMode.FULL)
owner = app.create_owner(now=now)
app.add_vehicle(owner, vehicle_profile, now=now, market=known_market)
session = app.start_session(owner, now=now, vehicle_id=vehicle_profile.vehicle_id)
turn = app.handle_message(owner, session, owner_message, now=now,
                          snapshot=optional_frozen_evidence)
# Only after the interface displays the exact proposal and the owner confirms:
confirmed = app.handle_message(owner, session, confirmation_message, now=now,
                               confirmation_id=turn.proposed_commands[0].proposal_id)
```

Bootstrap methods are trusted explicit adapter actions, not model tools or an
authentication system. `handle_message` returns response/status, optional validated
assessment, safety, proposed/applied/rejected commands, maintenance applicability
and state, changed reminder IDs and `AppTrace`. The trace references existing
sanitized planner/routing traces and adds opaque scope IDs, command outcomes,
refresh status, context characters, replay/error flags and measured elapsed time.
It is returned in memory, not dumped to SQLite. Future interfaces should render
`response` and reviewable proposals, not internal diagnostic structures.

Unknown/no selected vehicle, invalid commands/odometer/evidence/time, expired
confirmation, unavailable storage, provider failure and invalid final assessments
return explicit incomplete/error outcomes. No successful fact write is claimed
after a rollback. Missing manufacturer knowledge remains a normal unknown state.

## Acceptance and regression results

All 248 inherited tests are preserved. `tests/test_ownership_app.py` adds 42 tests,
including the five-scenario subtests, for **290 total: 290 passed, zero failures,
zero errors**. The final full run took 11.893 seconds. Test discovery ran under
socket patches blocking TCP connect/connect_ex, DNS resolution and datagram sends;
no provider-backed runner was invoked.

| Acceptance area | Verified result |
| --- | --- |
| A: owner/vehicle | Creation, selection, multiple cars, owner scope, restart |
| B: odometer | Initial/new/historical, explicit miles conversion, conflicts, invalid values, restart |
| C: service | Typed proposal/confirmation, existing read tool, provenance, replay including restart |
| D: maintenance | Fixture UPCOMING -> DUE -> new service NOT_DUE; stable reminder, acknowledgement and closure |
| E: unknown | No applicable source -> no due reminder; missing baseline -> item UNKNOWN; Hyundai quarantine preserved |
| F–I: diagnostics | Tire, cooling, battery and fuel remain grounded and retain original safety |
| J–K: follow-ups | Prior validated summary present; stop retained; model clearance prose suppressed; warning survives restart |
| L: inference | No inferred service/odometer/reminder fact from diagnostic hypotheses |
| Transactions | Failure between service write and refresh rolls back service, linked reading and result; failure loading a selected car's safety also rolls back selection |
| Corrections | Append/supersede, monotonic revalidation, failed correction restores original row |
| Authorization | No write from a proposal or plain “yes”; scope, expiry, uncertain statements and extra fields checked |
| Retention / errors | Bounded prompt/history, older service read on demand, provider/storage errors, future evidence rejected |

Safety parity with the unwrapped existing engine (seed 42):

| Scenario | Before / through application |
| --- | --- |
| healthy_vehicle | NO_RULE_TRIGGERED / NO_RULE_TRIGGERED |
| sustained_temperature_rise | STOP_WHEN_SAFE / STOP_WHEN_SAFE |
| weak_battery_start | UNDETERMINED / UNDETERMINED |
| gradual_tire_pressure_loss | STOP_WHEN_SAFE / STOP_WHEN_SAFE |
| increased_fuel_consumption | UNDETERMINED / UNDETERMINED |

The existing ten-case paired FULL/ROUTED benchmark has unchanged labels,
thresholds, fallback and exposure: 10 vs 3 average capabilities, 25 vs 9.3 tools,
4,363 vs 1,320.2 capability-instruction characters. Both have 100% completion/task
success, zero grounding failures and zero safety violations. Raw routed metrics
are 0.9 recall/precision/exact-set; effective metrics are all 1.0, with 10% fallback.
These are scripted plumbing/regression results, not evidence of real Jev accuracy.

## CLI and measured context

Run from the repository in PowerShell:

```powershell
$env:PYTHONPATH='src'; & 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -B -m carmind --demo
```

The demo is explicitly scripted/fake, and its test schedule is clearly labelled.
The complete captured transcript follows below. It uses the same application for
proposals, confirmations, history retrieval, maintenance, diagnosis and follow-up.
Its selected follow-up ownership block was **1,353 serialized characters**.
This excludes the existing planner protocol/tools and is not a token estimate.
The bounded-history test retains 12 real service records but exposes only eight
initially; a requested history tool can retrieve all 12. The store does load full
history in Python for maintenance and on-demand reads; this local MVP does not
yet paginate large histories or optimize those calculations.

## Exact change inventory

Created:

- `src/carmind/ownership.py` — command contracts, parsing, units and ownership context.
- `src/carmind/storage.py` — SQLite schema and repository operations.
- `src/carmind/app.py` — one application entry, authorization, transactions and refresh.
- `src/carmind/ownership_demo.py` — clearly fictional fixtures and scripted journey.
- `src/carmind/__main__.py` — thin offline CLI adapter.
- `tests/test_ownership_app.py` — offline ownership/diagnostic acceptance suite.
- `MILESTONE6_REPORT.md` — architecture, results and actual transcript.
- `README.md` — short developer entry point and limits.

Modified:

- `src/carmind/planner.py` — optional app context, bounded initial history and typed proposal result.
- `src/carmind/routing.py` — forward optional app inputs to the same planner.
- `src/carmind/safety.py` — deterministic unresolved-stop continuity helper; no threshold changes.
- `.gitignore` — ignore local ownership databases and journals.
- `AGENTS.md` — durable application, mutation, privacy and safety principles.
- `WORK_LOG.md` — milestone completion record.

No dependencies were added or installed. No existing domain contracts, tools,
manufacturer packs, simulator, provider settings, benchmark labels or prior tests
were edited. The existing untracked `test_jev_live.py` remains byte-identical:
SHA256 `161D8566DBA39F3213920C38AB75D33484026D51A355D66DF5196A45A545B7B1`.

## Explicit design choices and limitations

1. Conservative confirmation for **every** model-proposed write; bootstrap and
   explicit vehicle selection are trusted adapter operations.
2. One proposal per planner completion, separate from a final assessment; no
   command batching or implicit continuation after a write.
3. Twenty-four-hour proposals, per-session/vehicle scope and durable command replay;
   bounded read-only message replay ends when old conversation turns are pruned.
4. Six turns / eight initial services / eight reminder summaries; one compact
   validated summary. These are configurable-in-code MVP constants, not learned
   relevance judgments. No vectors or permanent raw-chat archive.
5. Service odometers create linked readings; corrections supersede both together.
   Miles normalize to km. Same-time conflicting readings fail closed.
6. Reminder lifecycle identity is separate from the existing engine's status-based
   event ID. Acknowledgement persists within a cycle; closure records its reason.
7. Test schedules require explicit opt-in and fixture markers. Real schedule
   activation needs human-vetted trusted configuration; transmission-specific
   schedules cannot match until the vehicle contract supports that known identity.
8. Stop warnings are retained per vehicle without a model-accessible clearance
   command. In no-telemetry follow-ups, only approved guidance is presented.
9. SQLite schema version one, no ORM/migration framework; one local connection,
   explicit transactions and no concurrency service. No remote accounts/auth.
10. The CLI is a reproducible fake journey with a temporary database, not an
    interactive language-model chat. Durable paths are available to app callers.
11. Query-time refresh is proactive calculation, not notification delivery.
    There is no scheduler, WhatsApp adapter, real OBD-II or cloud integration.
12. Existing claim validation checks references/schema, not complete semantic
    entailment of model prose. Physical facts remain unverified owner reports.
    Real-vehicle safety coverage is still absent; current thresholds are fictional.

Self-review removed direct tool calls from the demo's ownership history flow and
routed it through the application, checked warning isolation between cars,
rejected backdated processing that could expose newer state, preserved exact
confirmation replay after restart, and tested transaction failures. No semantic
keyword intent router, second planner or interval/safety calculation was added.

Before Milestone 6, none of the durable confirmed-write/reminder/restart journey
was possible. It is now executable through a single service boundary. Recommended
Milestone 7: harden a real conversational adapter around explicit confirmation
UX, validate one genuine applicable manufacturer source, and define an
auditable professional-review path for resolving stored stop constraints. Any
future live-provider evaluation should be separately budgeted and authorized.
WhatsApp deployment would additionally need authenticated owner/session mapping,
secure storage/backups, retention/export/deletion policy, transport replay
controls and operational delivery monitoring; none is silently assumed here.

## Git and network result

Changes are deliberately unstaged and uncommitted. No push, reset or remote
change occurred. `git diff --check` passed. The final status/stat are included
in the completion message; ordinary `git diff --stat` excludes new untracked
files, so the inventory above is also authoritative for this task's additions.

Live Jev API requests made: 0

Live xAI API requests made: 0

## Recorded offline benchmark output

These measured latencies are local fake-provider timings, not live API forecasts.

```json
{
  "FULL": {
    "cases": 10,
    "capability_count": 10,
    "tool_count": 25,
    "instruction_characters": 4363,
    "tool_schema_characters": 9819,
    "planner_calls": 2.1,
    "tool_calls": 1.1,
    "initial_context_characters": 22648.6,
    "cumulative_request_characters": 50249.4,
    "routing_latency": 0.0,
    "planner_latency": 0.009187489998294041,
    "end_to_end_latency": 0.013565169996581972,
    "tool_result_characters": 2368.6,
    "validation_failures": 0,
    "grounding_failures": 0,
    "completion_rate": 1,
    "task_success_rate": 1,
    "required_evidence_coverage": 1.0,
    "safety_policy_violations": 0,
    "fallback_rate": 0,
    "expansion_rate": 0,
    "planner_input_tokens": null,
    "planner_output_tokens": null,
    "total_tokens": null,
    "total_cost_usd": null
  },
  "ROUTED": {
    "cases": 10,
    "capability_count": 3,
    "tool_count": 9.3,
    "instruction_characters": 1320.2,
    "tool_schema_characters": 3696.1,
    "planner_calls": 2.1,
    "tool_calls": 1.1,
    "initial_context_characters": 13482.9,
    "cumulative_request_characters": 30823.2,
    "routing_latency": 0.00019137999624945222,
    "planner_latency": 0.008162039995659143,
    "end_to_end_latency": 0.012854020000668242,
    "tool_result_characters": 2368.6,
    "validation_failures": 0,
    "grounding_failures": 0,
    "completion_rate": 1,
    "task_success_rate": 1,
    "required_evidence_coverage": 1.0,
    "safety_policy_violations": 0,
    "fallback_rate": 0.1,
    "expansion_rate": 0,
    "planner_input_tokens": null,
    "planner_output_tokens": null,
    "total_tokens": null,
    "total_cost_usd": null,
    "raw_routing": {
      "required_recall": 0.9,
      "selection_precision": 0.9,
      "exact_set_agreement": 0.9
    },
    "effective_routing": {
      "required_recall": 1.0,
      "selection_precision": 1.0,
      "exact_set_agreement": 1
    }
  },
  "quality_regression_observed": false,
  "conclusion": "No regression on these cases; this alone does not establish live routing benefit."
}
```

## Actual offline demo transcript

```text
OFFLINE SCRIPTED DEMO — fake provider, no live intelligence or API calls.
Maintenance data: TEST_ONLY / NON_PRODUCTION / FICTIONAL. Not manufacturer guidance.
Created owner, added and selected My demo car.
User: My odometer was 15000 km on January 1.
CarMind: Record an odometer reading of 15000 km at 2026-01-01T00:00:00+00:00.
Please confirm this exact change. Nothing has been changed yet.
Owner explicitly confirms the displayed proposal.
CarMind: Recorded your confirmed change.
User: I changed the oil on January 1 at 15000 km.
CarMind: Record oil service at 2026-01-01T00:00:00+00:00. Odometer: 15000 km.
Please confirm this exact change. Nothing has been changed yet.
Owner explicitly confirms the displayed proposal.
CarMind: Recorded your confirmed change.
User: When did I last change the oil?
CarMind: Based on the available information:
- Your recorded oil service was on January 1 at 15000 km.

Safety cannot be determined from the available evidence and configured rules. Do not interpret missing or conflicting information as reassurance.
User: My odometer is now 24200 km.
CarMind: Record an odometer reading of 24200 km at 2026-01-06T12:00:00+00:00.
Please confirm this exact change. Nothing has been changed yet.
Owner explicitly confirms the displayed proposal.
CarMind: Recorded your confirmed change.

Maintenance reminders:
oil: coming up (scheduled at 25000 km / 2026-07-01).
User: What maintenance is coming up?
CarMind: Safety cannot be determined from the available evidence and configured rules. Do not interpret missing or conflicting information as reassurance.

Maintenance reminders:
oil: coming up (scheduled at 25000 km / 2026-07-01).
Deterministic maintenance: UPCOMING
User: The odometer now reads 25000 km.
CarMind: Record an odometer reading of 25000 km at 2026-01-06T12:01:00+00:00.
Please confirm this exact change. Nothing has been changed yet.
Owner explicitly confirms the displayed proposal.
CarMind: Recorded your confirmed change.

Maintenance reminders:
oil: due (scheduled at 25000 km / 2026-07-01).
User: I changed the oil just now at 25000 km.
CarMind: Record oil service at 2026-01-06T12:01:00+00:00. Odometer: 25000 km.
Please confirm this exact change. Nothing has been changed yet.
Owner explicitly confirms the displayed proposal.
CarMind: Recorded your confirmed change.
Active reminders after service: 0.
User: One tire keeps losing pressure. Can you check what might be happening?
CarMind: Based on the available information:
- Rear-left tire pressure fell across the available readings.

Possible explanations (not confirmed):
- A tire leak is one possible explanation; the cause is not established.

Limits of this assessment:
- No physical inspection was performed.

Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.

Suggested next steps:
- Arrange a professional service review; this does not imply driving the vehicle to the workshop.
Diagnostic safety: STOP_WHEN_SAFE
User: Can I keep driving?
CarMind: The earlier stop warning remains unresolved.

Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.

Arrange a professional service review; this does not imply driving the vehicle to the workshop.
Bounded ownership context: 1353 characters.
Restart: My demo car; 25000 km; 2 service records; 1 reminder lifecycle retained.
Unresolved stop warning retained: True
```
