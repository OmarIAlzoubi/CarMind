# Milestone 4 implementation report

## Outcome and limits

Implemented the deterministic knowledge/maintenance layer, reminder events,
simulation safety engine, 25 read-only tools, full-capability planner baseline,
grounded assessment validation, optional xAI adapter, tracing and offline demos.
Preserved the existing Milestones 1-3 implementation and pending research setup.

Two requirements remain limited by verified evidence/installation scope:

1. The official Thailand manual does not establish model year in the inspected
   document. Its pack is quarantined with `model_year: null`. It cannot match a
   vehicle or produce actionable reminders. The requested verified-year Hyundai
   upcoming/due demonstration therefore cannot honestly be claimed complete.
   The demo shows this UNKNOWN outcome, then exercises the arithmetic separately
   with an explicitly fictional schedule. No model year was guessed.
2. Only `openai==3.19.2` was installed, with `--no-deps`. The user's restriction
   prohibits installing anything else. The SDK's required runtime packages are
   absent: anyio, httpx2, jiter, pydantic, sniffio and typing-extensions.
   `pip check` consequently reports these missing dependencies. The optional
   adapter is implemented and tested with a fake SDK, but is not operational in
   this environment until dependency installation is separately authorized.
   The core has no third-party dependency and remains fully usable offline.

No Jev routing, WhatsApp delivery, real OBD-II, background scheduler, full database,
frontend, remote or push was added. A developer-only demo is not a product UI.

## Resumption audit

Starting HEAD: `ce00b13 Add capability registry and read-only tools`.
Earlier commits: `8b9958f Add deterministic vehicle simulator` and
`eaf65dc Initialize CarMind core contracts`.

Starting status:

```text
 M .gitignore
?? WORK_LOG.md
```

The only tracked diff was three added lines ignoring `.research/`. The work log
recorded planned SDK usage, the initial Canada research candidate, and simulation
safety scope. No Milestone 4 code or extracted schedule existed. Both files were
preserved and continued. The original suite passed all 73 tests before changes.

## Manufacturer research

Primary discovery source:
[Hyundai Mobility Thailand owner's manuals](https://www.hyundai.com/th/en/service/owner-benefits/owners-manual).
The ELANTRA N Download link resolves to the
[official Thai-language manual](https://www.hyundai.com/content/dam/hyundai/th/en/data/marketing/manual/2024_ELANTRA_N.pdf).
The stale Canada candidate was not used as maintenance evidence.

Selected source identity:

| Field | Verified value |
|---|---|
| Manufacturer/model | Hyundai / ELANTRA N |
| Model year | Unknown; stored as null, not 2024 |
| Market | Thailand; not Saudi Arabia/GCC |
| Title | ELANTRA N Owner's Manual / คู่มือการใช้รถ |
| Type/language | Owner's manual / Thai |
| Source ID | hyundai-th-elantra-n-manual |
| Retrieved | 2026-09-28 |
| PDF page count | 474 |
| Document version | Unknown |
| SHA-256 | ad9ead582c93c1b2c3a4dd4f5f6d9cb49dfa95469caed3325f99010f8b322e35 |

PDF page 1 identifies ELANTRA N. PDF page 8 / printed 1-2 names Hyundai Mobility
Thailand. The filename contains 2024, the PDF creation metadata is December 26,
2024, and the introduction contains copyright BE 2566. None establishes model
year. Full-document text was searched for Gregorian/Buddhist year identifiers;
cover, introductory identity page and selected maintenance tables were visually
inspected. Thai text extraction was imperfect, so rule transcription relied on
rendered tables, not search snippets. No third-party maintenance source was used.

Two concise normal-condition facts were transcribed:

| Rule | Fact | Provenance |
|---|---|---|
| th-normal-drive-belt-inspect | Inspect at 30,000 km or 24 months, whichever comes first | Normal maintenance schedule, gasoline engine; drive belts; PDF 395 / printed 9-8, footnote 1 |
| th-normal-cabin-filter-replace | Replace at 30,000 km or 18 months, whichever comes first | Normal maintenance schedule (continued), gasoline engine; cabin air filter; PDF 398 / printed 9-11 |

Both reference `hyundai-th-elantra-n-manual`. The drive-belt footnote includes
tensioner/idler and air-conditioning belt where equipped, with correction or
replacement if needed; only inspection recurrence was encoded. The loaded rule
retains the footnote reference. No standalone replacement interval was inferred.

Intentionally excluded: oil, coolant, transmission, severe-condition intervals,
fuel additives, conditional specifications, Saudi/GCC applicability, and any
model-year assignment. These would require further scope/footnote verification;
their presence elsewhere in the manual is not a claim that they were extracted.

Only metadata, two concise facts and page/section provenance are committed. The
complete PDF, text extraction, rendering script and page images stay in ignored
`.research/`. `git check-ignore` confirms the PDF is ignored; the commit contains
no PDF or rendered manual page.

## Deterministic maintenance behavior

- Immutable pack/profile/rule objects, exact manufacturer/model/year/market/
  engine/transmission/knowledge-version equality; unknown year never matches.
- Separate NORMAL/SEVERE/UNKNOWN conditions. No automatic severe-use inference.
- Mileage, calendar-month, AND and whichever-first triggers; first and repeat
  intervals supported. Calendar months clamp to the last valid day of the month.
- Inputs are explicit current time/mileage, stored service records and optional
  explicit in-service date. Missing history is never invented. A service record's
  exact service_type represents completion of that corresponding maintenance item.
- Future-dated service records are excluded; invalid/rolled-back odometers and
  duplicate service IDs are rejected. Supplied context must be as-of the cutoff:
  the existing contracts do not track when an old record was entered or edited.
- With missing dimensions, a known due/overdue dimension can establish
  whichever-first status. Otherwise uncertainty remains UNKNOWN.
- Upcoming windows default to 1,000 km / 30 days and are configurable application
  policy. They never change manufacturer intervals.
- Reminder IDs are stable for the vehicle/profile/rule/status/due-point/service
  baseline. A new matching service changes its reminder; other items are unchanged.
- All item statuses are calculated; only UPCOMING, DUE and OVERDUE become events.
  No event delivery, scheduler, persisted deduplication or database is implemented.

## Safety boundary

Versioned rules apply only to the fictional Everyday 2024 gasoline simulator
profile and SIMULATOR evidence. They are not Hyundai or universal thresholds.
Rules cover sustained coolant temperature, severe/recent tire loss, and abnormal
voltage paired with starting state. Missing, malformed, conflicting, stale or
insufficient critical history causes uncertainty. No evaluator truth or future
observation is available to the engine.

Precedence: STOP_WHEN_SAFE > UNDETERMINED > SERVICE_REVIEW > NO_RULE_TRIGGERED.
A trustworthy stop trigger remains visible despite missing other channels;
otherwise critical uncertainty prevents a service-only or no-rule reassurance.
The weak-start scenario triggers the starting-voltage rule but its missing tire/
coolant channels make the aggregate disposition UNDETERMINED. A test with all
critical channels present verifies SERVICE_REVIEW for that rule.

The engine chooses both disposition and approved message. The planner's schema
has no safety field. Even incomplete/provider-failed output retains the engine's
decision. NO_RULE_TRIGGERED explicitly does not mean safe to drive. Action IDs
resolve to approved wording and allowed dispositions; there are no vehicle
controls or high-risk DIY repairs.

## Full planner, grounding and provenance

Every run calls `load_all_capabilities()` with no scenario-based preselection.

| Exposure metric | Value |
|---|---:|
| Capability packs | 10 |
| Exposed tools | 25 |
| Capability planner-instruction characters | 4,184 |
| System protocol plus capability instructions | 5,433 |

The initial context includes owner message, profile, visible service history,
compact telemetry index, optional manufacturer pack/calculated maintenance state,
all tool definitions, safety decision and approved catalogs. The raw telemetry
timeline is not initially sent. Historical measurements must be fetched through
read-only tools. Source records and notes stay labeled input data.

Protocol: exactly one strict JSON tool_call or final object per turn. Duplicate
JSON keys, nonfinite JSON constants, markdown/code, unknown fields/tools/actions,
invalid argument shapes and invented IDs are rejected. There is no eval/native
provider tool calling. Maximum four model calls and six tool attempts; with one
tool call per model response the model budget is the tighter default limit.
One repair opportunity is shared across validation failures, only within budget.
Budget/provider failure produces incomplete output, not fabricated completion.

Factual observations select nonempty evidence-ID sets. Core materializes the
exact corresponding facts; it does not accept model-authored fact strings.
Each hypothesis chooses a small catalog ID with cited evidence and explicit
uncertainty. Uncertainty/limitation wording also uses a small catalog. This
deliberately limits expressiveness for this baseline and prevents an LLM from
smuggling safety assurances or invented due dates into free text.

Only actually exposed facts enter the evidence registry. Initial telemetry IDs
alone do not authorize a measurement claim. Future IDs, invented IDs and
colliding record identities fail validation. Source IDs must exist and support
the selected manufacturer-derived facts; those facts must cite their sources.
Maintenance values are copied from deterministic state, never recalculated by
the planner. The local knowledge pack is trusted curated input, not an automatic
web authenticity verifier. Hypothesis relevance is not proof of causation;
evaluation of semantic relevance remains future benchmark work.

Trace fields include actual planner/tool attempt counts, tool IDs, all exposure
counts, instruction lengths, wall-clock elapsed time, completion status,
validation failures and provider token totals. Missing token usage remains null,
including partial usage reports; actual zero is preserved. Input preparation is
not counted as a planner-selected tool execution. `complete` means protocol and
grounding validation completed, not a guarantee of diagnosis or task success.

## Demonstration 1: diagnostic

Run `python -m carmind.demo` with the approved interpreter and source path below.
The developer fixture uses gradual_tire_pressure_loss, seed 42; only the frozen
snapshot reaches the planner. The fake provider is scripted, not intelligent.

- Owner: "One tire keeps losing pressure. Can you check what might be happening?"
- All 10 capabilities / 25 tools exposed.
- Tool: get_tire_pressure_history, wheel=rear_left.
- Visible pressure: 34.964, 33.863, 32.555, 31.424, 30.250, 28.974 psi.
- Evidence prefix: `6e5f80f1-2ab6-55f7-97b4-1f623abaf4e4:observation:`;
  suffixes `002`, `006`, `010`, `014`, `018`, `022`.
- Hypothesis: a tire leak is possible; cause is not established.
- Uncertainties: cause unconfirmed and missing relevant evidence.
- Actions: ARRANGE_SERVICE_REVIEW, SHARE_EVIDENCE_WITH_WORKSHOP.
- Deterministic disposition: STOP_WHEN_SAFE, rule SIM_TIRE_LOSS.
- Approved message: "Stop when it is safe to do so and seek professional
  assistance. Do not treat this assessment as permission to continue driving."
- Trace: 2 model calls, 1 tool execution, complete, no validation failures,
  input/output tokens null. Observed local elapsed time about 0.008 seconds;
  this fake-provider runtime is not a live-model latency benchmark.

## Demonstration 2: maintenance secretary

Official research pack, Thailand, unknown year: current date September 28, 2026,
49,200 km; explicit cabin-filter service on April 1, 2025 at 20,000 km. The engine
returns UNKNOWN with null due points for both source rules and no reminder
events. It preserves rule/source IDs. A verified model-year matching vehicle
cannot be constructed from this source without guessing, so it is not fabricated.

Separate fictional calculation demonstration (not Hyundai guidance):

| Field | Result |
|---|---|
| Item | cabin_air_filter |
| Last service | April 1, 2025 at 20,000 km |
| Current | September 28, 2026 at 49,200 km |
| Due | 50,000 km or October 1, 2026, whichever comes first |
| Remaining | 800 km / 3 days |
| Status | UPCOMING |
| Rule/source | fictional-filter-rule / fictional-demo-source |
| Reminder | reminder:dad2acbc84df57b7a8766dfe |

A plain deterministic template explains the result. No LLM calculates anything.
After an explicit matching service at 49,200 km on September 28, the new due
points are 79,200 km / March 28, 2028 and status NOT_DUE. Tests also show that a
different item's baseline is not reset. This is arithmetic validation only.

## Dependencies and live smoke

Installed addition: official `openai` 3.19.2 only. Core dependencies remain empty;
`pyproject.toml` declares the optional live extra. No other environment was used
or changed, and no .env was created. No SDK transitive dependencies were installed.

Live smoke did not run. Absent required configuration variables: XAI_API_KEY,
XAI_MODEL. Their values were never printed. Independently, SDK runtime
dependencies are missing as noted above. No live-quality or live-latency claim is
made. A fake SDK test verifies base URL, configured model, no implicit retries,
JSON mode and absent-token handling.

Optional command after separately resolving prerequisites:

```powershell
$env:PYTHONPATH = 'C:\Users\GOAT\Projects\CarMind\src'
& 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -B -m carmind.demo --live
```

The xAI adapter uses https://api.x.ai/v1, reads only XAI_API_KEY and XAI_MODEL,
sets a 45-second request timeout and disables SDK retries so model-call budgeting
is not silently bypassed. Error bodies are not returned to the caller.
References: [official OpenAI SDK documentation](https://developers.openai.com/api/docs/libraries)
and [xAI API documentation](https://docs.x.ai/developers/model-capabilities/text/generate-text).

## Validation

Required full suite: **156 total, 156 passed, 0 failed**. All tests use unittest
and the approved carmind interpreter. The final suite was also executed with
socket creation blocked to confirm no external network dependency. Tests cover
all requested knowledge, maintenance, safety, planner, grounding and tracing
categories, plus strict JSON, calendar boundaries, source omission, identity
collisions, sparse/stale safety evidence, SDK failure and immutable request copies.

```powershell
$env:PYTHONPATH = 'C:\Users\GOAT\Projects\CarMind\src'
& 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -B -m unittest discover -s tests -v
& 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -B -m carmind.demo
```

`git diff --check` passes. The local PDF is ignored. No remote is configured.
The requested single commit is titled:
`Add maintenance intelligence safety and full planner baseline`.
The staged diff/stat is reviewed before that commit; final status is reported in
the completion message. No push is performed.

## Project tree (excluding Git metadata, ignored caches and bytecode)

```text
CarMind/
  .gitignore
  AGENTS.md
  WORK_LOG.md
  MILESTONE4_REPORT.md
  pyproject.toml
  capabilities/
    battery/manifest.json
    cooling/manifest.json
    diagnostic_codes/manifest.json
    electrical/manifest.json
    engine/manifest.json
    fuel_economy/manifest.json
    maintenance/manifest.json
    service_history/manifest.json
    tires/manifest.json
    trip_readiness/manifest.json
  data/
    action_catalog.json
    safety_messages.json
    safety_rules.json
  manufacturer_knowledge/hyundai/elantra_n/unverified_year/thailand/
    sources.json
    maintenance_schedule.json
  src/carmind/
    __init__.py
    assessment.py
    capabilities.py
    contracts.py
    demo.py
    evidence.py
    maintenance.py
    manufacturer_knowledge.py
    planner.py
    planner_provider.py
    safety.py
    simulator.py
    tools.py
  tests/
    support.py
    test_assessment.py
    test_capabilities.py
    test_contracts.py
    test_maintenance.py
    test_manufacturer_knowledge.py
    test_planner.py
    test_safety.py
    test_simulator.py
    test_tools.py
```

## Design decisions beyond the explicit requirements

1. Quarantine unverified model years rather than guess or use null as a wildcard;
   demonstrate UNKNOWN for the official pack and calculations with fictional data.
2. Exact case-sensitive identity/configuration equality, including knowledge
   version, rather than fuzzy model/market matching.
3. Extract only two visually verified normal-condition facts for the small PoC.
4. Use standard-library dataclasses and JSON; keep the existing Assessment fields
   and wrap them with per-claim evidence, source references and approved wording.
5. Select facts by evidence IDs and hypotheses/uncertainties/limitations by small
   catalogs. This is stricter and less expressive than arbitrary prose generation.
6. Default upcoming policy is 1,000 km/30 days, separate from source facts.
7. Permit an explicit in-service date for first-service baselines; absent history
   otherwise remains unknown. Calendar-month arithmetic clamps month-end dates.
8. Derive reminder IDs from state/baseline rather than generation timestamp; no
   delivery-state deduplication or persistence is implied.
9. STOP > uncertainty > service review > no rule; scope thresholds strictly to
   the fictional profile and SIMULATOR source; require adequate critical history.
10. One repair total, one tool per model response, count rejected tool attempts
    against execution budget; unknown token totals remain null if any turn lacks usage.
11. Use OpenAI-compatible Chat Completions JSON mode (no native tools), lazy SDK
    import, disabled retries, 45-second timeout and sanitized provider failures.
12. Install only the approved distribution with --no-deps, leaving the optional
    live adapter unavailable rather than silently installing unapproved packages.
13. Add a developer-only demo module and this report; no production interface.

These limits are intentional and documented, not a claim that the requested
verified-year Hyundai live reminder demonstration or live provider has succeeded.
