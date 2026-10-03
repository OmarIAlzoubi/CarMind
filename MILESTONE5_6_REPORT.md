# CarMind Milestone 5.6 — final assessment compactness (offline)

## Scope

This pass was fully offline. It did not call Jev or xAI, change routing, alter
provider settings, change tools, or change deterministic safety thresholds. The
historical live paired baseline remains the comparison point: routed tires used
one capability and 15,261 input tokens; full used ten capabilities and 23,178
input tokens. Those figures are preserved as historical evidence only.

## Contract audit and change

The old final contract required seven arrays, but `observations` were evidence-ID
selectors that the application expanded into serialized raw dictionaries. This
made the internal result verbose and unsuitable as a conversational response.

The new observation item is:

```json
{"text": "concise factual claim", "evidence_ids": ["cutoff-visible-id"]}
```

The text is bounded at 2,048 characters as a defensive provider-output limit. This
is large enough for a concise factual paragraph while preventing unbounded output;
it is not a product length target. The validator also rejects obvious serialized
raw evidence objects or claim driving
clearance. The validator checks every claim reference, derives the stable
`evidence_ids` union, and retains exact facts in the internal
`ValidatedAssessment.claims` audit record. Hypotheses keep catalog IDs and
claim-level references. Manufacturer `source_ids` remain a separate namespace and
are derived from referenced source-backed facts; an empty exposed source set
produces `source_ids=()` internally.

This preserves fail-closed validation for unknown, future, conflicting and
unsupported references, while avoiding raw payload duplication in the structured
assessment.

## Internal assessment and user response

`src/carmind/response.py` adds a small interface-independent projection. It uses
only validated claim text, approved hypothesis wording, deterministic safety text,
approved action wording and selected limitations. It does not expose evidence IDs,
JSON, rejected candidates, internal rule names or ScenarioTruth. WhatsApp can use
this projection later through an adapter; no WhatsApp code was added.

The seed-42 tire fixture can therefore say that rear-left pressure fell from about
35 psi to about 29 psi across six days, that a leak is possible but unconfirmed,
that inspection is needed, and that the deterministic result is to stop when safe.
This wording is supplied by an offline fixture, not hardcoded into production.

## Safety presentation

`SafetyDecision.limitations` remains the complete internal coverage audit. Each
simulation rule now declares its required evidence channels in
`data/safety_rules.json`; a small `user_limitations` field filters only presentation
for an active domain rule. For
the tire rule, missing coolant and battery channels remain in the internal result
but are omitted from the tire-facing response; relevant tire gaps and the general
simulation-policy limitation remain available to the response layer. The renderer
uses the approved safety message rather than exposing that internal phrase. With no active domain rule, the
approved indeterminate-safety message carries the uncertainty while detailed
channel lists remain internal. `NO_RULE_TRIGGERED` still never means safe to drive,
and the LLM cannot author the disposition.

## Structural size comparison

Using a deterministic seed-42 tire fixture and the same evidence IDs, the earlier
raw-copy versus claim-shape comparison (before this ownership pass) was:

| Component | Previous raw-copy shape | New claim shape |
| --- | ---: | ---: |
| observations | 1,263 | 448 |
| hypotheses | 74 | 395 |
| evidence references | 336 | 336 |
| uncertainties | 42 | 42 |
| limitations | 26 | 26 |
| total final JSON | 1,915 | 1,439 |

The total structural reduction is 24.9%. This is a character measurement from
offline fixtures, not a tokenizer estimate, live token saving, or quality result.

## Validation and regression

The focused contract suite covers concise claims, unknown/future references, raw
payload rejection, empty manufacturer sources, user-facing redaction and tire
safety presentation. Existing planner, routing, benchmark, maintenance and safety
fixtures were updated only to emit the new claim shape. Full-vs-routed selection,
tool exposure, fallback behavior, maintenance facts, grounding and deterministic
safety remain unchanged.

The completed offline suite and `git diff --check` results are reported with the
task completion. No live result was produced in this pass.

## Cross-scenario validation pass

### Simulator inventory

The simulator contains five scenarios. All were generated with seed 42 for this
pass; repository tests and development cases also use seed 42. The simulator
creates no service records, diagnostic-code records or manufacturer maintenance
state, so those remain unavailable unless a separate ownership fixture supplies
them.

| Scenario | Owner problem | Visible channels | Safety | Service / maintenance / manufacturer |
| --- | --- | --- | --- | --- |
| healthy_vehicle | Long-drive preparation | coolant, operating state, battery, four tires, fuel, trip duration, idle ratio | `NO_RULE_TRIGGERED` | absent / unavailable / unavailable |
| sustained_temperature_rise | Temperature warning | coolant, operating state | `STOP_WHEN_SAFE` | absent / unavailable / unavailable |
| weak_battery_start | Difficult starting | battery, starting/off operating state | `UNDETERMINED` | absent / unavailable / unavailable |
| gradual_tire_pressure_loss | One tire repeatedly loses pressure | four tire channels | `STOP_WHEN_SAFE` | absent / unavailable / unavailable |
| increased_fuel_consumption | Higher fuel use | fuel, trip duration, idle ratio | `UNDETERMINED` | absent / unavailable / unavailable |

The benchmark also has non-simulator ownership fixtures for maintenance, service
history, diagnostic-code history, ambiguous coverage, and a combined battery/tire
case. These are separate from simulator truth.

### Contract results

Each simulator scenario was run through a fake tool call followed by a fake final
assessment using the concise claim contract. All five completed with zero
validation failures, exact deterministic safety parity, valid action IDs, empty
manufacturer sources, and claim-level evidence references. No provider request
contained `ScenarioTruth` or a scenario label.

The repair suite additionally covers invalid grounding repaired to a cooling claim,
an unsupported battery action repaired to an approved action, invalid fuel-case
safety fields repaired to the application-owned result, and an unsupported
manufacturer source repaired to the exposed source ID.

### Actual offline user-facing responses

The following are deterministic offline projections from validated fake assessments.
They are representative engineering fixtures, not live model output.

**healthy_vehicle** (`NO_RULE_TRIGGERED`)

> Based on the available information:
> - Available vehicle checks are present for the planned trip.
>
> No configured deterministic rule triggered. This is not a determination that the vehicle is safe to drive.
>
> Suggested next steps:
> - Review available maintenance, tire, battery and cooling information before a trip; this is not driving clearance.

**sustained_temperature_rise** (`STOP_WHEN_SAFE`)

> Based on the available information:
> - Coolant temperature rose and remained elevated near the assessment time.
>
> Possible explanations (not confirmed):
> - A cooling-system issue is possible; the cause is not established.
>
> Limits of this assessment:
> - No physical inspection was performed.
>
> Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.
>
> Suggested next steps:
> - Arrange a professional service review; this does not imply driving the vehicle to the workshop.
> - Share the recorded symptoms and observations with a qualified workshop.

**weak_battery_start** (`UNDETERMINED`)

> Based on the available information:
> - Battery voltage dropped during the simulated starting event.
>
> Possible explanations (not confirmed):
> - A battery or starting-system issue is possible; testing is needed.
>
> Limits of this assessment:
> - No physical inspection was performed.
>
> Safety cannot be determined from the available evidence and configured rules. Do not interpret missing or conflicting information as reassurance.
>
> Suggested next steps:
> - Ask a qualified technician to check the battery and starting system.
> - Arrange a professional service review; this does not imply driving the vehicle to the workshop.

**gradual_tire_pressure_loss** (`STOP_WHEN_SAFE`)

> Based on the available information:
> - Rear-left tire pressure fell from about 35 psi to about 29 psi across six days; other tires remained comparatively stable.
>
> Possible explanations (not confirmed):
> - A tire leak is one possible explanation; the cause is not established.
>
> Limits of this assessment:
> - No physical inspection was performed.
>
> Stop when it is safe to do so and seek professional assistance. Do not treat this assessment as permission to continue driving.
>
> Suggested next steps:
> - Arrange a professional service review; this does not imply driving the vehicle to the workshop.
> - Share the recorded symptoms and observations with a qualified workshop.

**increased_fuel_consumption** (`UNDETERMINED`)

> Based on the available information:
> - Recent fuel consumption increased while trip duration and idle time also changed.
>
> Possible explanations (not confirmed):
> - Changes in driving usage could contribute; causation is unconfirmed.
>
> Limits of this assessment:
> - Available evidence may not represent the full vehicle condition.
>
> Safety cannot be determined from the available evidence and configured rules. Do not interpret missing or conflicting information as reassurance.
>
> Suggested next steps:
> - Review the recent observations and any missing information.
> - Arrange a professional service review; this does not imply driving the vehicle to the workshop.

The renderer remains a projection layer: it does not infer a diagnosis, choose
actions by keywords, or reconstruct telemetry. Its awkward boundary is that
natural conversational tone is limited when a claim needs richer explanation;
that is a future response-model concern, not a reason to add a second provider
call in this milestone.

### Capability coverage

| Capability | Coverage in current repository |
| --- | --- |
| battery | Directly exercised by weak-battery telemetry and battery tool |
| cooling | Directly exercised by sustained-temperature telemetry and coolant tool |
| tires | Directly exercised by tire-loss telemetry and tire tools |
| fuel_economy | Directly exercised by increased-fuel telemetry and fuel summary |
| trip_readiness | Directly exercised by healthy-vehicle trip case |
| electrical | Indirectly exercised through starting/battery routing; no dedicated electrical simulator channel |
| engine | Indirectly selected in fuel fixtures; no dedicated engine simulator evidence |
| maintenance | Indirectly exercised by trip/fuel routing and directly by separate ownership fixture, not simulator telemetry |
| service_history | Directly exercised by separate ownership fixture; absent from simulator episodes |
| diagnostic_codes | Directly exercised by separate ownership fixture; absent from simulator episodes |

No artificial capability success was added for unsupported simulator domains.

### Action-display review

All eight catalog actions remain allowlisted with unchanged permissions. Their
wording is understandable for ordinary drivers: tire checks refer to a parked,
safe location and the vehicle placard; service actions refer to a qualified
technician or workshop; trip preparation explicitly says it is not driving
clearance; maintenance review points to upcoming and due items. No wording change
was necessary, and no new action IDs were added.

### Observation-bound and raw-JSON review

The previous 1,000-character limit was arbitrary. It is now a documented
2,048-character defensive upper bound: large enough for a concise factual
paragraph, small enough to prevent unbounded provider output. It is a validation
guard, not a target and not a token budget. Existing claims are far below it.

Raw-evidence detection now parses obvious JSON objects/lists or key-value-shaped
serialized records containing evidence fields. Ordinary punctuation such as
`The scan returned code P0300 {historically recorded}.` remains valid. Markdown
fences and HTML tags are rejected as output noise. No NLP classifier was added.

### Hypotheses and limitations

Hypotheses remain catalog IDs with their own evidence references. The repeated
references are deliberate: a tentative explanation must remain auditable without
being treated as a diagnosis. They do not copy observation text.

Limitations are classified as model/PoC, evidence coverage, physical inspection,
manufacturer applicability, and safety coverage. The full set remains in the
internal assessment. The user response includes model and physical-inspection
limits, relevant safety presentation, and deterministic safety wording. Detailed
unrelated channel lists stay internal when no domain rule is active.

### Declarative safety dependencies and parity

Each simulation safety rule now declares `required_evidence` in
`data/safety_rules.json`. User-visible limitation filtering derives active-rule
dependencies from that data; it does not branch on scenario names or user text.
All internal gaps remain in `SafetyDecision.limitations`. Active-rule gaps remain
eligible for presentation; unrelated global gaps may be omitted when the approved
safety message already states that the result is indeterminate. Rule IDs,
thresholds, precedence and dispositions are unchanged across all five scenarios.

### Size statistics across simulator scenarios

Offline fake-provider measurements, seed 42:

| Measure | Average | Median | Maximum |
| --- | ---: | ---: | ---: |
| Internal assessment JSON | 1,541.6 | 1,185 | 2,420 |
| Observation characters | 74.0 | 76 | 85 |
| Hypothesis characters | 57.4 | 70 | 74 |
| Evidence-reference characters | 806.4 | 336 | 2,016 |
| Uncertainty characters | 83.0 | 104 | 104 |
| Limitation characters | 315.4 | 386 | 463 |
| User-response characters | 568.8 | 614 | 655 |

These are serialized character counts only. They are not provider-token estimates.

### Adversarial and repair coverage

Offline tests now cover raw serialized payloads, oversized and empty claims,
unknown and future evidence, markup noise, duplicate IDs, unsupported sources,
unknown actions, invented safety fields, unsafe driving language, concise repair
after invalid grounding, action repair, manufacturer-source repair and cross-
scenario safety parity.

### FULL / ROUTED regression

The ten-case offline paired benchmark remains unchanged in routing and exposure:

- FULL: 10 capabilities, 25 tools, 100% completion and task success.
- ROUTED: average 3 capabilities, 9.3 tools, 100% completion and task success.
- Routed fallback rate: 10%; raw recall/precision/exact-set: 0.9/0.9/0.9.
- Effective routing recall/precision/exact-set: 1.0/1.0/1.0.
- Grounding failures: 0; safety violations: 0; token/cost values: unavailable.

The new contract works in both modes under the same fake-provider fixtures. No
Jev policy, threshold, capability description, tool behavior or provider setting
was changed.

## Final-contract ownership and live-output review

### Historical live result

The latest real ROUTED tire run completed and validated successfully with
`STOP_WHEN_SAFE`, but it did not reduce output size:

| Metric | Previous routed run | Latest routed run |
| --- | ---: | ---: |
| Input tokens | 15,261 | 15,381 |
| Output tokens | 1,989 | 2,320 |
| Final-call output tokens | 1,933 | 2,264 |
| Orchestration latency | ~62.24 s | 84.24 s |

The latest model emitted verbose natural-language telemetry despite the concise
contract. This is recorded as a regression in efficiency, not hidden or reframed.

### Live-output contributors

The live final payload itself was not persisted in the repository, so an exact
field-by-field character decomposition of the 2,264 output tokens is unavailable.
From the recorded behavior, the largest contributors were the model-authored
observation prose: full ISO timestamps, six explicit rear-left readings, extrema,
and one detailed statement per wheel. The repeated 24-ID top-level union was a
separate redundant contributor under the previous contract. JSON/protocol
overhead was smaller than the repeated telemetry content. The minimized protocol
removes the union from model output, but only model behavior can prevent verbose
claim text.

### Field ownership

| Field | Ownership | Rationale |
| --- | --- | --- |
| `observations` | Model semantic | The model chooses decision-relevant factual claims; Python validates text and evidence IDs. |
| `hypotheses` | Model semantic, catalog constrained | The model selects a tentative explanation; catalog wording prevents diagnosis. |
| `evidence_ids` | Derived | The application computes the stable union of claim references. |
| `source_ids` | Derived | The application derives manufacturer sources from validated source-backed facts. |
| `uncertainties` | Model semantic, catalog constrained | The model identifies uncertainty categories. |
| `safety_disposition` | Application-owned | Deterministic safety rules inject the authoritative result. |
| `recommended_action_ids` | Model selection, application permission | The model selects from the catalog; disposition permissions are deterministic. |
| `limitations` | Mixed | The model may select semantic limitations; application safety limitations are appended deterministically. |

The model protocol now omits top-level `evidence_ids` and `source_ids`. Optional
legacy fields remain accepted only when they exactly match the derived values, so
existing internal consumers continue to receive the full `Assessment` contract.

### Evidence and source derivation

Claim references are validated directly against exposed evidence. CarMind then
deduplicates them in first-seen claim order. Unknown, future and duplicate IDs
remain fail-closed. Manufacturer source IDs are derived from `source_id` fields on
validated referenced facts; optional legacy source lists must exactly match that
derived union.

### Observation concision and minimal evidence

The protocol now instructs the model to produce one decision-relevant factual
finding per claim and to cite only the smallest evidence set sufficient for that
claim. Python does not choose which samples are semantically sufficient. The
validator checks only exposure, cutoff visibility and identity integrity.

The deterministic response renderer remains intentionally simple. It presents
validated claim text, catalog wording, approved actions and deterministic safety.
It does not summarize telemetry or infer causes. The live run shows that prompt
guidance alone may not guarantee concise model output; a future response model or
stronger structured claim protocol may be evaluated separately, without adding
that call now.

### Offline current-vs-minimized payload comparison

For the five simulator scenarios, the current compatibility-shaped candidate
included top-level evidence/source unions. The minimized candidate omitted those
derived fields while keeping the same claim references.

| Measure | Current average | Minimized average | Current median | Minimized median | Current max | Minimized max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Total model payload chars | 2,412.4 | 1,570.0 | 1,438 | 1,066 | 4,316 | 2,402 |

Average structural reduction: **34.9%**. This is an offline character comparison,
not a live token forecast. It does not reproduce the latest model's verbose prose.
The largest removable contributor is the repeated top-level evidence union; the
latest live run also shows that verbose observation text remains a major model
output risk.

### Cross-scenario responses

The five validated responses documented above remain unchanged semantically after
ownership minimization. Their content contains no IDs or JSON, preserves
uncertainty, uses approved actions, and retains deterministic safety wording.

### Repair behavior

Repair feedback now concerns only model-owned fields: claim evidence, hypothesis
IDs, uncertainty IDs, action IDs, limitation IDs, and claim text. Safety
disposition, safety limitations, evidence unions and source unions are not repair
targets because the application owns or derives them.

### Files changed in this pass

- `src/carmind/assessment.py`
- `src/carmind/planner.py`
- `src/carmind/benchmark.py`
- `src/carmind/demo.py`
- `tests/support.py`
- `tests/test_assessment.py`
- `tests/test_cross_scenario_validation.py`
- `tests/test_response.py`
- `MILESTONE5_6_REPORT.md`
- `WORK_LOG.md`

The existing response layer, safety rule declarations, routing, tools, simulator,
provider and `test_jev_live.py` were not modified in this pass.
