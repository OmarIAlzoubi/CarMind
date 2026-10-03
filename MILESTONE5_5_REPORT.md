# CarMind Milestone 5.5 — offline architecture hardening

Date: 2026-09-30. Baseline HEAD: `bfde9fb` (provider diagnostics).
No external calls, credentials, package changes, staging, commits or pushes.

## 1. Executive technical conclusion

The first reported real Grok trace demonstrated a valid tire-only tool sequence,
not a routing failure: history, then summary, then the two-call runner stopped.
It did not establish assessment quality. The old summary repeated raw history.
This pass preserves the existing uncommitted compact tire summary, extends the
same correction to cooling and fuel, strengthens exact-ID resolution, reduces
initial evidence discovery overhead, and makes subsequent context growth visible.

The shared planner, providers, tool permissions, deterministic safety and
manufacturer calculations remain in place. Offline scripted results retain quality
parity. Real model tool choice, completion, tokens, cost and latency remain untested.

## 2. Baseline state

- 189 tests passed with network connections blocked; zero failures/errors.
- 10 packs, 25 tools. Existing tracked edits: tire manifest, planner, tools,
  routing tests and tool tests. Existing untracked runner, runner tests and
  `test_jev_live.py` preserved. Current work was not reset.
- FULL exposure: 10 packs / 25 tools / 4,309 capability instruction characters.
  Tire-only: 1 / 3 / 596. FULL tool definitions: 9,719 characters.
- Ten paired benchmark cases: both modes 100% scripted completion/task success,
  100% required evidence coverage, zero safety violations. ROUTED averaged 3 packs,
  9.3 tools; fallback 10%, expansion 0%; raw recall/precision/exact agreement 0.9,
  effective scores 1.0. These labels and scripts were not changed.
- Safety at seed 42, before and after: healthy `NO_RULE_TRIGGERED`, temperature
  `STOP_WHEN_SAFE`, starting `UNDETERMINED`, tire loss `STOP_WHEN_SAFE`, increased
  fuel consumption `UNDETERMINED`.

## 3. Complete tool/evidence audit

All tools below are model-facing, read-only and allowlisted. All received inspection
and successful exact-record citation coverage in the offline tests. Electrical
needs a synthetic user observation because the development benchmark has no
electrical measurement case. No additional product features were added to cover it.

Capability membership (shared tools are intentional):

| Pack | Tool coverage |
|---|---|
| battery | profile, battery voltage history, starting summary |
| cooling | profile, coolant history, cooling summary |
| diagnostic_codes | profile, code history, active codes |
| electrical | profile, electrical observations |
| engine | profile, engine observations, recent engine history |
| fuel_economy | profile, consumption history, trip-duration history, idle history, fuel summary |
| maintenance | profile, all five manufacturer/maintenance tools, service history, latest service, odometer context |
| service_history | profile, all five manufacturer/maintenance tools, service history, latest service |
| tires | profile, tire history, tire summary |
| trip_readiness | profile, trip collector, tire history, battery history, coolant history, service history, schedule, rule, source, due, upcoming |

Table conventions describe the evidence/cutoff behavior for **each** marked row:

- **O**: original observation ID resolves to its exact record; `_public_observations`
  merges and orders cutoff-visible snapshot/context samples and rejects conflicts.
- **S**: service record ID, filtered by performed_at <= assessment_at.
- **C**: existing stable generated diagnostic-code record ID, filtered by observed_at.
  Active is a stored flag, not a live scan or a historical state reconstruction.
- **P**: exact profile ID; profile is supplied as-of context, not independently dated.
- **M**: rule/source IDs or deterministic reminder IDs; exact supplied knowledge and
  calculation records. Maintenance uses the assessment timestamp and visible service
  history. Unknown applicability remains UNKNOWN; knowledge is not a time-versioned
  external document store.

Every row's ID list repeats IDs present in its records or summary groups. This is
intentional: the envelope enumerates usable references, while nested IDs explain
which fact/group they support. No compatibility-breaking ID compression was added.
As-of context cannot reconstruct subsequent edits to old records; callers must
provide the appropriate snapshot of stored knowledge.

Sizes are characters in `json.dumps({'tool_result': asdict(result)}, default=str)`.
Except electrical, they are the largest representative envelope among the ten
development fixtures, not universal maxima or tokens. Baseline and final maxima
can occur in different cases. Unchanged sizes are shown once.

| Tool ID | Purpose/class; output structure | IDs/cutoff | Chars before → after | Repeated raw facts, sibling overlap, smaller view | Grounding risk / action |
|---|---|---|---:|---|---|
| get_vehicle_profile | profile; profile object | P | 309 | repeats initial profile; no smaller sibling | exact vehicle record; retain |
| get_engine_observations | raw symptoms/state; observations | O | 1,681 | recent history is bounded sibling | retain detailed evidence |
| get_engine_recent_history | history; latest observations, default 5 | O | 1,425 | subset of engine observations | explicit limit already bounded; retain |
| get_coolant_history | raw temperature history; observations | O | 1,699 | compact cooling sibling | retain chronological source |
| get_cooling_summary | derived arithmetic; summaries | O | 2,231 → 1,060 | raw duplication removed; history remains | exact supporting IDs; changed |
| get_battery_voltage_history | raw voltage history; observations | O | 1,661 | partial overlap with starting summary | retain all operating states |
| get_starting_voltage_summary | paired derived view; observations + summaries | O | 2,027 | repeats paired voltage, adds essential state records; no same-pack raw state sibling | retain pairing evidence; no mechanical compaction |
| get_electrical_observations | raw electrical symptoms/voltage; observations | O | 142 unavailable; 300 synthetic success | no smaller sibling | retain; benchmark coverage gap documented |
| get_tire_pressure_history | raw per-wheel history; observations | O | 6,495 | summary sibling and optional wheel filter | retain prior raw behavior |
| get_tire_pressure_summary | derived wheel trends; summaries | O | 3,886 | no raw duplication; IDs shared with history | preserve existing uncommitted fix |
| get_fuel_consumption_history | raw consumption history; observations | O | 1,698 | compact fuel sibling | retain units and chronological readings |
| get_trip_duration_history | raw usage context; observations | O | 1,707 | compact fuel sibling | retain; no inferred cause |
| get_idle_time_history | raw usage context; observations | O | 1,673 | compact fuel sibling | retain; no inferred cause |
| get_fuel_usage_summary | derived usage trends; summaries per channel/unit | O | 6,363 → 2,923 | three raw histories no longer embedded | exact supporting IDs; changed |
| get_service_history | service history; records | S | 479 | also preloaded; latest/filter are smaller views | retain as global ownership context |
| get_latest_service_record | latest service; one record | S | 304 | subset of service history | exact record retained |
| get_maintenance_odometer_context | profile/history view; mileage + records | P/S | 534 | repeats service records; latest smaller but different meaning | vehicle ID resolves to profile, not aggregate |
| get_diagnostic_code_history | history; code records | C | 404 | active filter may be smaller | exact code record; no DTC interpretation |
| get_active_diagnostic_codes | filtered history; code records | C | 404 | subset of code history | stored active status only |
| get_trip_readiness_evidence | raw aggregate; profile, tires, battery, cooling, service, missing categories | P/O/S | 9,919 | overlaps domain tools; domain siblings are narrower | large by design; not a summary or verdict; retain |
| get_manufacturer_maintenance_schedule | metadata/rules; applicability, profile, rules | M | 1,413 | initial knowledge + rule sibling overlap | retain exact provenance and UNKNOWN |
| get_manufacturer_maintenance_rule | filtered rules; applicability, profile, rules | M | 1,409 | exact-ID filter smaller than schedule | exact rule and exposed source; retain |
| get_maintenance_source_reference | metadata; official sources | M | 655 | initial sources overlap; ID filter available | exact document record; retain |
| get_due_maintenance_items | calculated status; items + unknown_items | M | 1,216 | initial state overlap; status subset | existing first-class reminder IDs; no model arithmetic |
| get_upcoming_maintenance_items | calculated status; items + unknown_items | M | 1,221 | initial state overlap; status subset | same deterministic records; retain |

Reviewed all ten instruction sets. Cooling unnecessarily prescribed history before
a trend; its instruction now distinguishes raw chronology from arithmetic summary.
Cooling/fuel tool descriptions explain their actual compact outputs. Other packs
already distinguish reported codes, measurements, profile metadata, history and
manufacturer facts adequately. No benchmark hints, cost advice or new tool-order
policy was added. Routing descriptions are untouched.

## 4. Changes implemented, by exact file

- `src/carmind/tools.py`: extend compact summary handling to coolant and fuel;
  preserve endpoint times, extrema, change, units and real source IDs; clarify
  descriptions. Retain existing tire work and all raw tools.
- `capabilities/cooling/manifest.json`: semantic affordances instead of unnecessary
  history-first wording; routing description untouched.
- `src/carmind/planner.py`: channel availability index; exact record registration
  for profile/message; cross-domain collision checks; reject unresolved IDs instead
  of substituting an entire result. Extend trace with context/tool metrics and
  sanitize unknown tool identifiers in trace lists.
- `src/carmind/routing.py`: fix expansion accepting multiple packs in one request;
  now one additional capability per one permitted attempt. No routing model changes.
- `src/carmind/benchmark.py`: report tool envelope sizes and distinct validation
  and grounding failure totals using existing paired harness.
- `scripts/live_xai_compare.py`: preserve existing untracked runner; explicit
  --max-calls with default 2 and maximum 4, plus context/tool reporting.
- `tests/test_hardening.py`: 11 new cross-cutting offline tests with subcases.
- `tests/test_live_xai_compare.py`: preserve prior four tests, add four budget/
  reporting/retry-guard tests.
- `tests/test_benchmark.py`: update synthetic report fixture for new metric fields.
- `WORK_LOG.md`: append findings/results and clarify that the old routing-policy
  description is historical. This report is new.

Pre-existing modifications in `capabilities/tires/manifest.json`,
`tests/test_routing.py` and `tests/test_tools.py` were preserved without further
editing in this pass. The older untracked `test_jev_live.py` was untouched.

## 5. Changes considered and rejected

- Starting summary compaction: removing raw operating-state records would obscure
  why voltage samples were paired. The battery raw sibling lacks those states.
- Trip collector compaction: it deliberately gathers raw cross-domain evidence;
  narrower tools already exist. Calling it a compact summary would change its contract.
- Deleting initial service/manufacturer facts: service knowledge supports ownership
  queries and deterministic maintenance regardless of capability selection. Tire's
  empty service data costs just 2 characters; manufacturer null 4, state empty 2.
  More extensive history can grow, but deleting it based on one query would weaken
  parity. Manufacturer authority and the inactive Thailand pack remain unchanged.
- Removing IDs, truncating histories, discarding prior results, or forcing summary
  first: would reduce auditability or replace planner judgment with heuristics.
- Splitting safety presentation automatically: no approved relevance contract
  connects every rule dependency to user scope. Details below.
- Provider/API/caching changes, new frameworks, dependencies and live tests: out of scope.

## 6. Grounding correctness

Previously the tire patch resolved known observation IDs correctly, but the generic
fallback could still bind an unknown reference to an entire ToolResult.data object.
That fallback is removed. Every returned ID must resolve to an exposed typed record
or a known cutoff-visible observation. Summary IDs resolve to individual observations;
the summary itself is not registered as each observation's fact.

Profile/message/service/manufacturer identities and visible observation identities
are checked for conflicts before a provider call. New records from tools are checked
atomically before they enter the exposed evidence map. Unread telemetry remains in a
private lookup, not the validator's exposed map. Profile facts, service/code records,
rules, sources and existing deterministic reminders retain their exact identity.
No new synthetic evidence class was introduced.

Frozen snapshots reject future samples; tools also filter future context observations,
services and code records. The model receives no ScenarioTruth. Derived summaries
group finite numeric observations by name/unit and cite only their supporting samples;
raw histories remain available for intermediate ordering and detailed inspection.

## 7. Context architecture and observability

The old initial index repeated ID/name/unit/timestamp per reading before the model
had requested a tool. It scaled with sample count while providing no values. The
new index supplies name, unit, source, count and first/last availability times per
channel. It preserves all channels and does not choose a domain, tool or diagnosis.
Detailed sample times and IDs become available from tools; no arbitrary cap is used.
Exact irregular timing requires history. Effect on live tool choice remains unknown.

Tire initial context is now 9,333 characters: protocol 1,528 + capability 596 +
initial JSON 7,209. Within that JSON: tool definitions 1,417; index 676 (previously
3,984); safety 811; owner message 186; vehicle 163; service 2; manufacturer 4;
maintenance 2. Remaining characters are catalogs, availability IDs, cutoff and JSON
syntax. Catalogs remain necessary for the strict selection protocol.

`PlannerTrace.call_exposures` now records call number, capabilities/tools, total
request size, protocol, pack instructions, tool definitions, initial JSON, index,
safety, manufacturer, maintenance, service, message/profile, prior assistant actions,
other history, accumulated/latest tool results and exposed evidence-ID count.
Component values exclude their enclosing key syntax and overlap initial JSON;
they must not all be summed. Exact request size equals protocol + instructions +
initial JSON + prior history + accumulated tool results. Expansion updates the
initial tools/instructions and is reflected in the next turn's metrics.

`tool_executions` records catalog ID (unknown IDs redacted), phase, success/error,
serialized envelope characters, unique reference count and execution latency.
Pre-model service preparation is separately marked and does not consume the
planner's tool budget. Its envelope size is diagnostic: only its data is inserted
in initial context. Benchmark payload totals count planner-requested executions.
No tool payloads, message text, headers or keys are copied into these metrics.
Provider tokens remain authoritative; characters are not token estimates.

## 8. Safety review

The engine checks global evidence coverage for coolant, battery and all four tires,
plus starting-state pairing. Missing, stale, malformed, conflicting or insufficient
history contributes uncertainty/limitations. Disposition precedence still allows
an evidenced STOP_WHEN_SAFE rule to dominate unrelated missing channels. Complete
and incomplete assessments append the engine's limitations without model choice.
Thus tire cases retain coolant/battery coverage gaps in user-facing limitations.

This conflates global coverage with question relevance. **No presentation split was
implemented.** Safely omitting only irrelevant gaps needs an explicit dependency/
relevance contract for active and potentially unassessable rules. Selection by loaded
packs, keywords or just the triggered rule could suppress important missing evidence.
A future design should store structured coverage separately and conservatively derive
presentation from that contract while keeping the authoritative decision unchanged.

All five simulator dispositions above are unchanged. Existing unsafe-action and
safety-override tests pass. The paired benchmark has 20/20 safety-correct runs and
zero violations. Missing-rule dependencies remain visible; NO_RULE_TRIGGERED is
still never driving clearance. No threshold, rule, wording or manufacturer status
was relaxed. Thailand unknown-year/market applicability remains quarantined.

## 9. Routing and expansion review

Jev adapter, questions, routing state, descriptions, thresholds, fake scores and
expected labels are unchanged. Reported real tires=0.97 / trip_readiness=0.44 is
historical evidence, not a new call. FULL still bypasses routing.

The only routing-module change enforces the requested single additional capability.
Previously a list could load multiple packs during one attempt. Unknown, repeated,
redundant or multi-pack requests return incomplete with an explicit reason and no
silent FULL fallback. A valid expansion updates tools/instructions under the same
call/tool budgets. Raw initial selection remains distinct from effective coverage.

## 10. OFFLINE STRUCTURAL / ORCHESTRATION RESULTS

Ten existing paired cases, identical inputs/scripts in each mode. Means unless
stated. Before = current working-tree baseline, not pristine HEAD.

| Metric | FULL before | FULL after | ROUTED before | ROUTED after |
|---|---:|---:|---:|---:|
| Capabilities | 10 | 10 | 3 | 3 |
| Tools | 25 | 25 | 9.3 | 9.3 |
| Capability instruction chars | 4,309 | 4,363 | 1,304 | 1,320.2 |
| Tool-definition chars | 9,719 | 9,819 | 3,672.1 | 3,696.1 |
| Initial request chars | 23,610.9 | 21,586.5 | 14,559 | 12,420.8 |
| Sum of request chars per case | 52,883 | 48,019.9 | 33,699.8 | 28,593.7 |
| Planner calls | 2.1 | 2.1 | 2.1 | 2.1 |
| Tool calls | 1.1 | 1.1 | 1.1 | 1.1 |
| Planner tool-result envelope chars | not traced | 2,368.6 | not traced | 2,368.6 |
| Scripted completion / task success | 100% | 100% | 100% | 100% |
| Required evidence coverage | 100% | 100% | 100% | 100% |
| Safety violations (total) | 0 | 0 | 0 | 0 |
| Validation / grounding failures (total) | not summarized | 0 / 0 | not summarized | 0 / 0 |
| Routing fallback | 0% | 0% | 10% | 10% |
| Expansion | 0% | 0% | 0% | 0% |

ROUTED raw recall/precision/exact-set remain 0.9/0.9/0.9; effective metrics remain
1/1/1 after the ambiguous fixture's explicit FULL fallback. Scripted results are
not measured Jev accuracy or real LLM quality. Unknown tokens and cost remain null.
One offline timing sample averaged FULL planner/end-to-end 5.59/8.17 ms and ROUTED
5.24/7.96 ms (fake router 0.108 ms). These host timings are incidental, not estimates
of provider latency or evidence of routing performance improvement.

Current explicit exposure: FULL 10/25/4,363; tires 1/3/596; fuel_economy+engine+
maintenance+tires 4/17/1,852 (packs/tools/instruction chars). Every case completes
with valid actions, grounding, safety and maintenance parity. Maintenance fixture
uses manufacturer-derived UNKNOWN results identically in both modes.

| Case | Initial FULL / ROUTED chars | Cumulative FULL / ROUTED chars |
|---|---:|---:|
| ambiguous | 21,001 / 21,001 | 42,553 / 42,553 |
| code_history | 20,968 / 8,338 | 42,420 / 17,160 |
| fuel_usage | 21,184 / 15,629 | 45,351 / 34,241 |
| maintenance | 23,653 / 13,585 | 48,610 / 28,474 |
| multiple_domains | 22,124 / 11,176 | 71,664 / 38,820 |
| service_history | 20,964 / 10,178 | 42,335 / 20,763 |
| starting | 21,311 / 9,575 | 44,360 / 20,888 |
| temperature | 21,142 / 8,594 | 44,055 / 18,959 |
| tire_loss | 21,502 / 9,333 | 44,820 / 20,482 |
| trip | 22,016 / 16,799 | 54,031 / 43,597 |

The fixture tire script filters one wheel; it is intentionally distinct from the
all-wheel reconstruction below. Neither script predicts real model behavior.

## 11. Offline tire-trace reconstruction

Seed 42, ROUTED tires, unchanged owner message. Canonical JSON tool actions each
cost 78 characters. No live responses were generated. Fake final turns only make
the next request measurable; they do not establish model assessment quality.

| Measurement | Historical original | Pre-pass compact baseline | After |
|---|---:|---:|---:|
| Tire history envelope | 6,494 | 6,494 | 6,494 |
| Tire summary envelope | 8,595 | 3,886 | 3,886 |
| Initial request | 12,354 | 12,641 | 9,333 |
| History then call 2 | — | 19,213 | 15,905 |
| History → summary → hypothetical call 3 | 27,599 | 23,177 | 19,869 |
| Summary → hypothetical call 2 | — | 16,605 | 13,297 |

The additional 3,308-character reduction is entirely the initial index change
(3,984 → 676). Tire instructions and schemas are unchanged from pre-pass state.
The older 287-character initial difference predates this pass. Both all-wheel tools
retain 24 source IDs. Three-turn accumulated tool envelopes total 10,380 characters;
the second summary adds no new unique IDs. Context still grows when the model
chooses overlapping tools; no heuristic prevents that choice.

## 12. Manual live runner readiness

The runner was exercised only through fake-provider tests, never against a provider.
It reports mode/model, selected capabilities/tools, per-turn size breakdown, tool
sequence/results sizes and evidence IDs/counts, provider tokens/latency, validation,
safety, expansion and sanitized provider errors. Cached tokens remain null if the
current adapter does not expose them. No provider configuration was changed.

Conservative default: 2 provider attempts. Explicit range: 1..4, no higher than
production. Failed attempts count; the wrapper independently blocks over-budget
calls; the entry point rejects SDK clients with nonzero retries. No automatic
second mode, no Jev requests. Exit 1 means incomplete, including budget exhaustion.
The retained local tool observation wrapper is restored in finally; this manual
script is single-process instrumentation, not a concurrent service framework.

PowerShell syntax for **later manual authorization**, not executed here:

```powershell
& 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -B 'C:\Users\GOAT\Projects\CarMind\scripts\live_xai_compare.py' --mode routed --max-calls 3
```

`--mode full` remains supported under the same explicit budget, but is not the
recommended next experiment and should not run automatically after routed mode.

## 13. Tests

Final: **204 passed, 0 failures, 0 errors** (baseline 189; 15 new tests).
All tests use the required carmind Python interpreter, src on PYTHONPATH and
blocked network connections. No SDK live runner invocation or credential loading.

New coverage includes coolant/fuel arithmetic and source retention, nonnumeric/
future/unit separation, successful exact-record citations for every tool, unresolved
IDs, cross-domain collisions, channel index coverage, exact context accounting,
safe structural traces, scenario safety parity, invalid/multi-pack expansion,
benchmark metric aggregation, explicit 3/4-call bounds, failed-attempt accounting
and zero-retry enforcement. Existing permissions, grounding, provider diagnostics,
quarantine, expansion and safety tests still pass. `git diff --check` passes.

## 14. Complexity review

No new core module, framework, semantic router, keyword matching or tool scheduler.
Two small planner helpers summarize availability and count context; one local
execution wrapper traces both preparation and planner tools. Existing dictionary
registration is extended rather than replaced by an evidence framework. Exact-ID
validation uses a private identity map and atomic exposed-map updates; no tool
result payload is retained by traces. Provider parameters and production budgets
are unchanged. New character metrics are useful diagnostics, not optimization targets.

## 15. Git state and scope

All changes remain unstaged on main; no commit/push/remote operation. Final tracked
modifications include WORK_LOG, cooling/tires manifests, benchmark/planner/routing/
tools, benchmark/routing/tool tests. Final untracked entries include this report,
scripts/live_xai_compare.py, tests/test_hardening.py, tests/test_live_xai_compare.py,
and the pre-existing untouched test_jev_live.py. Diff statistics compare against
HEAD, so they include preserved prior work and exclude untracked files.

## 16. One recommended next experiment

After separate approval, manually run only ROUTED tires/seed42 with grok-4.6 and
an explicit **maximum of three xAI attempts**, replaying the recorded tires
selection (zero Jev calls). Observe whether a valid final assessment arrives within
that budget and inspect per-turn context, tool sequence, provider tokens and
deterministic safety. Do not run FULL, tune settings or retry automatically. This
is a proposed experiment only; its outcome is unknown.

Live Jev API requests made during this task: 0
Live xAI API requests made during this task: 0
