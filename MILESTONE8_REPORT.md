# Milestone 8 — Product surface, vehicle intelligence and multi-channel UX

## Product objective and result

CarMind now has one channel-neutral product service over the existing `CarMindApp`.
An owner can propose a vehicle in natural language, confirm the exact displayed
details, record services and mileage through the existing confirmed-command
path, and read a deterministic My Car overview. A thin WhatsApp-shaped adapter
and a localhost web view use the same SQLite owner, session, vehicle, reminder
and safety state. This is a local product foundation, not a deployed messaging
service or a claim that unscripted offline chat has semantic intelligence.

## Architecture

Before: interfaces/evaluations invoked `CarMindApp.handle_message` with trusted
owner and session IDs. After: `ProductService.handle(InboundMessage)` resolves a
trusted channel binding, deduplicates external message IDs, invokes the same
application, and returns a concise `ProductReply`. `ProductService.overview` is a
deterministic projection. WhatsApp and Web normalize/transport messages only;
neither has automotive rules, tools, SQL or a separate conversation engine.
The planner, router, assessment validator, safety engine, manufacturer matching,
maintenance arithmetic and command executor remain in `CarMindApp` and its
existing collaborators. Real providers remain opt-in through composition.

```
WhatsApp payload ─┐
                  ├─> ProductService ─> CarMindApp ─> existing core/SQLite
Local Web UI/API ─┘          │
                            └─> deterministic My Car read model
```

## Product contracts and reads

`InboundMessage` has a channel, external owner/message identities, text,
UTC-aware timestamp, optional locale, exact confirmation ID, and trusted frozen
evidence for offline development. It contains no Twilio-specific fields.
`ProductReply` carries answer text, status, selected vehicle, optional proposal
ID, and authoritative safety notice; it does not expose traces, capability
scores, evidence IDs, model tokens or raw provider data.

The overview reads vehicle make/model/year, optional trim/engine/market/nickname,
canonical km and the latest reading's original unit/time, last service and
distance since that service, bounded service history, applicable maintenance,
reminder lifecycle, unresolved deterministic stop, and a bounded recent
validated concern. It makes no router or planner call and invents no health
score. Multiple vehicles are owner-scoped and the selected vehicle is explicit.
The profile fields already supported by the core were reused; VIN remains
optional and was not added to conversational onboarding.

Manufacturer guidance appears only when an exact vetted, market-applicable
schedule is injected and matched by the existing application. An explicitly
opted-in fictional schedule is marked `test_only` in the product read model,
never represented as verified manufacturer guidance. The quarantined
Thailand PoC source remains unavailable for the ordinary Saudi/unknown-market
Hyundai demo. With no match, own service/mileage history still works, while
the response states that the next manufacturer interval is unverified.
Reminder generation and completion remain deterministic and transactional with
confirmed ownership writes. The overview can show active or completed reminder
lifecycle facts; it does not schedule or deliver notifications.

## Confirmation, idempotency and safety

Vehicle onboarding adds a small typed draft alongside existing command
proposals. A model may propose explicit make/model/year, optional details and
an odometer only with its stated unit. Validation and user confirmation precede
an atomic add/select/odometer/reminder refresh. No model output writes facts.
The confirmation copy displays every nonempty draft detail and the exact
distance/unit; regular command copy displays local date/time, notes and
correction reference when present. The web confirmation button and messaging
confirmation both submit the stored proposal ID through the same operation.

Schema v2 adds `channel_bindings`, `external_receipts` and `vehicle_drafts`,
with an explicit v1-to-v2 migration. Receipts use the tuple (channel, external
user ID, external message ID); internal message IDs are SHA-256 namespaced.
Retries replay the persisted reply, and the underlying command/draft state
prevents duplicate facts even if a process stops after application but before
receipt persistence. Bindings are created by trusted local composition; a
public WhatsApp gateway must authenticate senders before calling the adapter.

The product service only renders the application's deterministic safety result.
An unresolved stop is always appended as a short notice after the answer, even
for an unrelated service-history question. The Web overview also exposes the
same unresolved constraint. The service never turns `NO_RULE_TRIGGERED` into a
claim that driving is safe. Real Hyundai demo evidence has no configured
threshold policy, so it yields an indeterminate answer rather than clearance.

## Interfaces and localization

`whatsapp.normalize_payload` accepts Twilio-shaped sender, message ID, body and
optional ISO timestamp. `handle_payload` calls the product service; `twiml`
escapes the reply. There are no Twilio API calls, live webhook, delivery jobs or
webhook-signature verifier in this milestone.

`carmind.web` serves a small responsive page and JSON API on **127.0.0.1 only**.
The page puts chat beside My Car, service history, verified maintenance status
and concerns. It uses `textContent` for user/model text. Endpoints provide
overview, vehicles, services, reminders, chat, confirmation and vehicle
selection. A fixed local development identity can be deliberately linked to a
WhatsApp demo sender; this is not production authentication. The offline fake
provider is suitable for scripted acceptance, not arbitrary conversation.
`--live` is an explicit future manual choice and was not used in this milestone.

Arabic and English are supported in message contracts and deterministic
response copy. Arabic confirmation, service/mileage answers, applicability
limits, tentative catalog hypotheses and safety notices are covered by offline
journeys. Chat bubbles use automatic text direction. Static Web chrome is
English in this local MVP; a full localized interface is deferred.

## Offline acceptance evidence

- Full unittest discovery with `socket.socket` blocked: **349 passed, 0 failed**.
- Focused cross-scenario/stateful checks: 24 passed. Existing five-scenario
  safety parity and unresolved-stop continuity remain in the full suite.
- Ten-case scripted routing benchmark: FULL and ROUTED both 100% task success,
  100% required-evidence coverage and zero safety-policy violations. Average
  exposed capabilities/tools/instruction characters were 10/25/4,363 in FULL
  and 3/9.3/1,320 in ROUTED. This is scripted development data, not proof of
  live Jev or xAI quality; token and price measurements were unavailable.
- Complete offline WhatsApp/Web/WhatsApp journey: confirmed Hyundai profile
  at 18,500 km; confirmed oil service at 20,000 km; correct history response;
  no invented manufacturer schedule; tire concern retained; Web reads the same
  vehicle/service/concern; Web confirms 20,200 km; WhatsApp subsequently reads
  20,200 km. No active stop was claimed for the real Hyundai profile, whose
  deterministic policy is not configured.
- New focused tests cover schema migration, restart, miles-to-km provenance,
  proposal display, expiry and cross-owner rejection, external replay,
  multiple vehicles, stale proposal protection, unsupported schedules,
  deterministic zero-provider overview, Twilio normalization/escaping, Web
  endpoints without sockets, cross-channel state, and stop presentation.

## Local manual demo

From the repository root in PowerShell, choose a fresh local demo database:

```powershell
$env:PYTHONPATH = 'src'
$env:PYTHONIOENCODING = 'utf-8'
$demoDb = Join-Path $env:TEMP ('carmind-m8-' + [guid]::NewGuid().ToString() + '.sqlite3')
& 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -m carmind.whatsapp_demo --db $demoDb
& 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -m carmind.web --db $demoDb --link-whatsapp-sender demo-sender
```

Open `http://127.0.0.1:8765/`. The scripted WhatsApp journey populates the same
database the Web page reads. It uses only fake provider fixtures and makes no
external calls. Stop the local server with Ctrl+C. For unscripted semantic
conversation, a future explicitly configured real-provider manual test is
needed; this milestone did not spend provider credits.

## Known limits and next candidates

- WhatsApp is a payload adapter, not a deployed, authenticated webhook.
- The Web server uses a fixed localhost demo identity; production sessions,
  authentication, CSRF protection and public deployment are out of scope.
- Offline arbitrary chat cannot be interpreted by a scripted fake provider.
- The real Hyundai demo has no verified exact-market manufacturer pack and no
  real-vehicle deterministic safety thresholds. No interval or clearance is
  inferred from the fictional simulator policy.
- Static Web labels are English. Full Arabic chrome and stronger accessibility
  review are next product UX work.
- Receipt deduplication is persisted, but concurrent multi-process request
  serialization is not implemented for this localhost, single-process server.

Milestone 9 candidates: authenticated channel identity and WhatsApp webhook,
verified market-specific manufacturer packs, a localized Web shell, and
carefully budgeted manual real-provider UX evaluation. Real OBD-II, background
notification delivery and production hosting remain separate future work.

No dependencies were installed. No live Jev, xAI, Twilio or manufacturer API
calls were made. No files were staged, committed or pushed.
