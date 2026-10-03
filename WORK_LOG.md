# Milestone 4 work log

- The official `openai` Python dependency is required only for the optional live
  xAI planner adapter. Core logic and all unit tests must run offline without it.
  No other package is to be installed. The adapter will import it lazily.
- Research is restricted to official Hyundai documentation. Full manuals and
  temporary research assets, if downloaded, belong in ignored `.research/`.
- The indexed Hyundai Canada 2024 ELANTRA N maintenance chapter is the first
  candidate; direct access and table verification are pending. No schedule has
  been extracted yet.
- Safety thresholds will apply only to the fictional demo profile. They are
  application simulation policy, separate from manufacturer maintenance facts.

## Resumed Milestone 4

- Inspected the current diff and recovered the original request from the prior
  chat. Preserved the research ignore rule and this work log. Baseline: 73 tests.
- The earlier Canada candidate is superseded by the official Hyundai Thailand
  manual page and its ELANTRA N PDF. Download and derived research assets are
  local only in ignored `.research/`.
- Installed only `openai==3.19.2` using the carmind interpreter and `--no-deps`.
  No transitive dependency installation was authorized or performed. The live
  SDK requires additional runtime packages and is consequently not usable yet;
  the adapter fails clearly. Offline core/tests never import or require the SDK.
- PDF inspected: 474 pages; cover identifies ELANTRA N; printed 1-2 / PDF 8 names
  Hyundai Mobility Thailand. No explicit model-year statement found. The 2024
  filename and PDF creation date do not establish model year. Stored year=null;
  exact matching refuses unknown years, including null-to-null matching.
- Extracted two concise normal-condition facts after visual table inspection:
  drive-belt inspection 30,000 km / 24 months (9-8 / PDF 395), cabin air filter
  replacement 30,000 km / 18 months (9-11 / PDF 398), whichever comes first.
  All remain research-only, not operational Saudi/GCC or verified-year guidance.
- Built generic maintenance calculations, read-only tools, simulation-only safety,
  full-context planner, strict evidence selection, bounded repair, provider
  adapter, traces and offline demonstrations. No future layers were implemented.
- Design choices and exact validation results are recorded in MILESTONE4_REPORT.md.

## Milestone 5

- Audited clean HEAD 94378a7 and read MILESTONE4_REPORT.md before editing.
  All 156 existing tests passed with sockets blocked. Preserved the quarantined
  Thailand unknown-year pack and existing deterministic safety/maintenance behavior.
- The user explicitly authorized openai runtime dependency repair and official
  typesafe-sdk plus its dependency closure. Installed typesafe-sdk 0.7.2; retained
  openai 3.19.2. pip check passes; xAI adapter constructs offline with test-only
  configuration and blocked network. No unrelated package was requested.
- Verified TypeSafe 0.7.2 source and official Python/Noul documentation. Use one
  System One request containing one Noul per pack, SDK default model behavior,
  no implicit retries, and a 15-second SDK timeout. No live calls were made.
- Default routing policy: include >=0.7; fallback if any relevance is between
  0.3 and 0.7 or peak included relevance is below 0.85. These are development
  policy settings, not calibrated claims about Jev. Labels are frozen before runs.
- Senior review found the sole 10% offline fallback is the intentional ambiguous
  fixture: every relevance score is 0.5, so the configured borderline policy
  falls back to FULL. Relevance-boundary trace reporting was corrected to record
  `borderline_relevance` rather than `empty_selection`.
- Final review validation: 174 tests passed with sockets blocked, `pip check`
  passed, and the paired offline benchmark showed no scripted quality regression.

## Milestone 5.5 offline hardening (2026-09-30)

- Resumed HEAD bfde9fb without resetting the existing tire-summary/grounding work
  or untracked manual runner. Baseline: 189 tests passed, 10 packs, 25 tools.
- The older Milestone 5 policy entry above describes its original implementation.
  Current committed routing falls back for borderline scores only when nothing is
  selected (6386b6f). No Jev prompts, thresholds, scores or labels changed here.
- Audited all packs/tools; compacted duplicate coolant and fuel summaries while
  retaining raw histories and the starting summary's paired state records.
- Evidence IDs now fail closed if unresolved or conflicting across source types;
  summary citations resolve to exact cutoff-visible observations.
- Replaced the per-observation planner index with channel availability metadata;
  added per-turn context and per-tool structural metrics to existing traces.
- Found that one expansion request could add multiple packs. It now accepts one
  additional capability; invalid/repeated requests remain incomplete, never FULL.
- Kept safety policy, limitations, manufacturer quarantine, provider settings and
  pre-model service context unchanged. Safety presentation separation is deferred
  pending an explicit rule-dependency/relevance contract, not keyword heuristics.
- Manual runner defaults to two attempts and supports explicit --max-calls 1..4;
  failures count, SDK retries must be zero. It was NOT run live.
- Final validation: 204 tests passed with connections blocked; no failed tests or
  errors. Offline paired fixtures retain complete scripted results and safety
  parity. Character reductions do not establish live quality or token savings.
- See MILESTONE5_5_REPORT.md for the full audit, metrics and one proposed manual
  experiment. No dependencies installed; no keys read; no live calls; no Git
  staging, commit, push or remote changes. test_jev_live.py left untouched.

## Final-assessment forensic hardening (offline)

- Started from committed HEAD 100c80c; baseline 217 passing offline tests.
  The only pre-existing untracked file was test_jev_live.py; it was not edited.
- The reported manufacturer error is raised when assessment.source_ids contains
  a reference outside the exposed manufacturer-document source set. The tire
  runner supplies no MaintenanceRequest, so that set is empty; the old prompt
  represented this indirectly as manufacturer_knowledge=null and maintenance_state=[].
- The validator correctly rejects unsupported provenance. The unavailable real
  candidate prevents identifying its exact offending reference or distinguishing
  a fabricated document ID from confusion with an observation/source label.
  No manufacturer serialization or validation defect was demonstrated.
- Candidate recovery: inspected runner/provider/planner retention and project files
  including ignored research, plus CarMind-named entries in the local temp directory.
  No persisted real candidate was found. The prior runner retained action/usage
  metrics, not final payloads. No original response is reconstructed here.
- Added an explicit allowed_manufacturer_source_ids list and general protocol
  instructions to use source_ids=[] when empty. A source being exposed never
  establishes vehicle applicability. No quarantine, routing, tools or safety changes.
- Added evaluation-only rejected_model_candidate projections with the corresponding
  validation error/category and call number. Only expected structure and exposed/
  catalog IDs survive; unknown IDs, free text and extra keys are not printed.
  Projection is deliberately not an exact raw response and is never published as
  the application assessment. No new persistence or logging framework was added.
- Added independent final_status and repair_status. An invalid final on the last
  call now reports repair_needed_but_call_budget_exhausted alongside its primary
  final_grounding_failed/final_validation_failed outcome. Failed repairs are explicit;
  successful repairs retain rejection history while reporting final_valid.
- Repair control flow is unchanged: one global repair opportunity, normal call
  budget, prior tools/evidence preserved, error returned as a user protocol message.
  The rejected candidate itself is not replayed in model context; the model generates
  a corrected protocol object and may request a tool. Production maximum remains 4;
  manual budgets/defaults and provider configuration are unchanged.
- Final output must include seven arrays. Claim-level and union evidence IDs repeat
  by design for grounding checks; manufacturer source IDs are a separate namespace.
  A large final can therefore be structurally understandable, but the reported 1936
  output tokens cannot be attributed to specific content without the missing payload.
  No output limit, compression or caching/pricing changes were made.
- Synthetic accounting preserves 14883 input, 14720 cached input and 1992 output
  tokens for the reported three usage tuples; cost stays unknown, not free.
- Added 14 forensic tests covering cases A-G, redaction, malformed finals, repaired
  finals, repair failures, ordinary budget stops, provider failure during repair,
  unchanged safety and cached accounting. Existing reliability assertions now
  accommodate the intentional source-contract context addition and repair statuses.
- Recommended next experiment only after authorization: ROUTED, maximum 3 calls,
  existing 120-second explicit evaluation timeout and grok-4.6. Test the clarified
  source contract before spending on an extra repair call. Validation is expected
  for a compliant final, not guaranteed from the unavailable real payload.
- No live requests, credentials loaded, dependency changes, staging, commit or push.

## Final assessment compactness and response projection (offline)

- Replaced model-produced observation payload copies with concise claim text plus
  claim-level evidence IDs. The validator still requires every claim ID to be an
  exposed, cutoff-visible record and preserves the top-level union for downstream
  checks. Raw facts remain available in the internal `ValidatedAssessment.claims`
  audit structure, while `Assessment.observations` contains only concise text.
- Added a small interface-independent response projection. It renders validated
  claims, catalog hypotheses, deterministic safety wording and approved action
  wording; it never exposes evidence IDs, JSON, rejected candidates or hidden
  simulator truth. No extra model call was added.
- Safety retains the complete internal coverage-gap list. A minimal
  `user_limitations` projection presents rule-relevant gaps when a domain rule is
  active; the tire case therefore keeps the missing coolant/battery gaps in the
  internal assessment but does not surface them in the ordinary-driver response.
  Deterministic disposition and thresholds are unchanged.
- Updated offline fixtures and the manual runner for the new observation claim
  contract. A synthetic seed-42 tire comparison reduced serialized final JSON
  from 1,915 to 1,439 characters (24.9%); this is structural character counting,
  not a live token or quality claim.
- Added focused contract and user-response tests. Full offline validation and
  routing/planner behavior remain required before any future live experiment.

## Final cross-scenario assessment validation (offline)

- Revalidated all five simulator scenarios with fake tool/provider flows. Each
  produced a valid concise claim, exact claim-level evidence, correct top-level
  evidence union, allowlisted actions, empty manufacturer sources and deterministic
  safety parity. No scenario truth entered provider messages or user responses.
- Replaced the arbitrary 1,000-character observation bound with the documented
  2,048-character defensive limit. Existing claims are much shorter. Raw evidence
  detection now recognizes obvious serialized evidence objects while allowing
  harmless technical punctuation; HTML and fenced-markdown noise is rejected.
- Added declarative `required_evidence` dependencies to the three simulation safety
  rules. User-visible limitation filtering derives from active rule dependencies,
  not scenario names or user-message keywords. Detailed internal gaps remain
  complete; no active-rule uncertainty is suppressed.
- No-active-rule responses now use the approved general safety uncertainty rather
  than listing every unrelated missing telemetry channel. The internal trace keeps
  each missing channel. Safety thresholds, precedence and dispositions are unchanged.
- Added cross-scenario contract and repair tests covering cooling, battery, fuel,
  tire, manufacturer provenance and application-owned safety. The ten-case FULL vs
  ROUTED offline benchmark retains its routing/exposure/safety results.
- Cross-scenario serialized assessment sizes average 1,542 characters and user
  responses average 569 characters after omitting internal simulation-only wording
  from the user projection. These are structural character counts only;
  no live token or quality claim is made.

## Final-contract ownership and compactness pass (offline)

- Recorded the latest real routed tire result honestly: validation and safety passed,
  but output increased to 2,320 tokens (call-three output 2,264) and latency rose
  to 84.24 seconds. No live output improvement is claimed.
- Reduced the model-owned final payload to semantic fields: claims, hypotheses,
  uncertainty IDs, action IDs and semantic limitation IDs. Top-level evidence and
  manufacturer-source unions are now derived by the application. Safety disposition
  and safety limitations remain application-owned and deterministic.
- Kept optional legacy union fields only for compatibility; when present they must
  exactly match the derived values. New benchmark and cross-scenario fixtures omit
  them, exercising the minimized protocol.
- Updated protocol guidance to prefer one decision-relevant finding per claim and
  the smallest sufficient evidence set. No Python evidence-count or domain-specific
  summarizer was added.
- Offline five-scenario compatibility-vs-minimized payloads reduced average
  serialized characters from 2,412.4 to 1,570.0 (34.9%). This is not a live token
  estimate; verbose model-authored prose remains an unresolved live risk.

## Milestone 6 — Ownership agent MVP (offline)

- Baseline `b634fc9`: 248 tests passed before edits. Added a standard-library
  SQLite ownership store and transport-independent `CarMindApp.handle_message`.
- Persisted multi-vehicle owner state, chronological odometer readings, service
  records, bounded sessions, exact command proposals/results, source-backed
  reminder lifecycles and unresolved deterministic stop constraints.
- The existing planner can propose typed allowlisted commands in application mode.
  Every write requires separate confirmation of the exact proposal; uncertainty,
  hypotheses and plain conversational assent do not authorize ownership writes.
- Service/odometer corrections append replacements and supersede earlier rows.
  Mutations, reminder refresh and idempotent result storage commit atomically.
  Explicit miles conversion, monotonic timeline checks and UTC time are enforced.
- Existing tools read persisted contracts through a VehicleContext adapter. Initial
  service/chat context is bounded; deterministic maintenance still sees full
  accepted service history. No manufacturer schedule is activated by default;
  the Hyundai source remains quarantined. A marked test-only fictional schedule
  exercises upcoming/due/reminder/service-completion flows.
- Added an offline fake CLI journey, restart tests, correction/rollback/replay
  tests, multi-turn safety continuity and all-five-scenario parity checks.
  FULL/ROUTED benchmark retains 100% scripted completion/task success, effective
  routing metrics of 1.0 and zero safety violations; no live quality claim.
- Final network-blocked suite: 290 passed, zero failures/errors. New dependencies:
  none. Detailed architecture, actual demo transcript, measurements, limitations
  and change inventory are in `MILESTONE6_REPORT.md`.
- No live Jev or xAI requests, staging, commits or pushes. `test_jev_live.py` remains
  untouched. All milestone changes are left in the working tree for review.

### Milestone 6 final offline acceptance audit

- Started from the existing 290-test passing baseline and reviewed routing,
  safety, ownership app/storage, planner, demo, schema, and milestone tests.
- Fixed sticky-stop response projection for fresh unrelated telemetry; direct
  model-authored driving-clearance claims are rejected. Confirmation lookup and
  mutation now share `BEGIN IMMEDIATE`; profile-field proposals retain and check
  an application-owned original-value precondition.
- Added focused tests for idempotent odometer/service-correction/reminder-ack
  replay, unresolved stop after vehicle switch-back and fresh unrelated evidence,
  schema-version rejection, and a full two-vehicle/restart acceptance journey.
- Final network-blocked suite: 299 passed, zero failures/errors. The paired
  offline benchmark remains unchanged; the fake CLI demo and `git diff --check`
  pass. Seven-run median local timings and limitations are documented in
  `MILESTONE6_REPORT.md`.
- No Jev/xAI calls, dependency installation, staging, commit or push. The
  untracked `test_jev_live.py` remains byte-identical.

## Milestone 7 — Real stateful intelligence integration (offline implementation)

- Added explicit provider composition for the existing `CarMindApp`: offline fake
  providers by default; real Jev plus xAI only with explicit live selection. The
  application, planner, tools, safety engine, validator and storage paths are shared.
- Extended the strict planner action protocol with a validated clarification
  request. Existing typed ownership proposals still require a separate exact
  confirmation with zero provider calls. Expanded proposal display to include
  semantic target IDs and correction references.
- Kept the ten existing capability packs. Additive routing descriptions cover
  service declarations, odometer reports and reminders. Historical diagnostic
  planner instructions and tool counts are unchanged; ownership routing fixtures
  are separate from the diagnostic benchmark.
- Added deterministic projection of cited persisted service facts, including
  when a prior STOP_WHEN_SAFE remains unresolved. Model-authored driving
  clearance remains suppressed; no manufacturer schedule is invented.
- Added a checkpointed, resumable evaluation runner with a fresh marked SQLite
  database, sanitized JSONL artifacts, cumulative call reservations, conservative
  one-call defaults, explicit live acknowledgment and no automatic live journey.
- Fresh socket-blocked dry run passed checkpoints 0–8 with zero Jev/xAI calls.
  Historical scripted benchmark remains 100% completion/task success in FULL
  and ROUTED; routed average exposure is 3 capabilities and 9.3 tools versus
  FULL 10 and 25. Five scenario safety dispositions remain unchanged.
- Full network-blocked suite and detailed results are in `MILESTONE7_REPORT.md`.
  No live calls, dependency changes, staging, commits or pushes. Preexisting
  `test_jev_live.py` remains byte-identical.

### Milestone 7 checkpoint 7 offline forensic fix

- Reconstructed the previous Jev input: it lacked the prior owner turns, tire
  assessment and unresolved stop state. Added a compact, bounded follow-up block
  carrying recent owner text, prior validated observations/hypotheses and the
  application-owned unresolved safety disposition. Jev still chooses capabilities;
  no threshold, label or forced-pack rule changed.
- Confirmed the phantom third call: the local xAI budget wrapper rejected it
  before SDK invocation, after planner tracing had incremented its count. The
  runner now passes the remaining per-turn/cumulative xAI allowance into the
  shared planner. Two actual requests yield two planner/provider records and
  `call_budget_exhausted`; no fake provider error is emitted. Zero allowance
  yields zero attempts.
- Added an explicit `--retry-failed-checkpoint` path with a unique turn ID,
  append-only retry history, preserved SQLite ownership facts and cumulative
  Jev/xAI counters. The prior failed DB was read-only inspected and not changed.
- Clarified artifact semantics as `simulated_evidence_supplied` plus `tool_ids_used`.
  Safety continuity under planner failure and call exhaustion remains
  STOP_WHEN_SAFE. The final network-blocked suite passes 326 tests. Routing
  benchmark, five-scenario safety parity, fresh dry-run and retry tests pass;
  details are recorded in `MILESTONE7_REPORT.md`.
- No live Jev/xAI requests, staging, commit or push. `test_jev_live.py` remains
  byte-identical.

### Milestone 7 checkpoint 7 retry forensic hardening

- Inspected the saved retry artifact and state sidecar read-only. The retry
  returned `STOP_WHEN_SAFE` plus deterministic stop guidance; the planner was
  `incomplete` after its two-call allowance. The runner incorrectly treated
  planner incompletion as lost safety and reported a misleading invariant.
- Added distinct current, prior-stop and effective safety disposition fields to
  the app trace, plus a structured stop-guidance-applied contract in artifacts.
  The checkpoint invariant uses these fields and no longer requires planner
  completion or fixed English wording.
- Documented that a stop persists until a dedicated clearance mechanism or
  deterministic resolution event exists. Stop persistence is vehicle-scoped
  under an owner-owned vehicle and survives sessions/restarts; other vehicles
  do not inherit it.
- Added offline coverage for neutral/unrelated turns, no-stop inheritance,
  cross-session restart, and a real temporary-DB checkpoint 7 fail/retry path.
  Existing tests cover provider failure, model clearance attempts, multi-vehicle
  isolation and prior-stop restoration.
- Validation: 330 tests passed with network socket operations blocked; 7 focused
  safety parity/cross-scenario tests passed; all 9 dry-run checkpoints passed
  with zero provider calls; `git diff --check` passed. No live Jev/xAI request,
  staging, commit, push or edit to the saved evaluation database.

## Milestone 8 — Product surface and multi-channel UX

- Added a channel-neutral `ProductService` over the existing `CarMindApp`, with
  concise English/Arabic replies, explicit confirmation, persisted external
  message receipts and a deterministic My Car read model. Core reasoning,
  maintenance, manufacturer matching and safety remain shared.
- Added schema v2 with channel bindings, external receipts and vehicle drafts;
  v1 databases migrate explicitly. Natural vehicle onboarding follows the same
  proposal → exact confirmation → atomic write pattern as ownership commands.
- Added a thin Twilio-shaped WhatsApp adapter, localhost-only JSON API and small
  My Car/chat page, plus a scripted offline WhatsApp/Web acceptance journey.
  No live messaging or provider call is part of the demo.
- Product tests cover cross-channel state, restart/replay, owner/vehicle scope,
  miles provenance, unsupported schedules, stop continuity, Web API and Arabic
  interactions. Full validation and limitations are in `MILESTONE8_REPORT.md`.
- No dependency installation, live Jev/xAI/Twilio request, staging, commit or
  push. The preexisting untracked `test_jev_live.py` remains untouched.
