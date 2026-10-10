# CarMind

**An AI-powered personal car ownership assistant that understands your vehicle, remembers its history, grounds manufacturer-specific answers in your own documentation, and can guide ownership needs into structured aftersales workflows.**

CarMind is a vehicle-agnostic AI system designed to make car ownership easier for everyday drivers.

Instead of acting as a generic automotive chatbot, CarMind combines conversational AI with:

- Persistent vehicle memory
- Jev-powered semantic routing
- Manufacturer-document RAG
- Deterministic maintenance intelligence
- Deterministic safety logic
- Proactive ownership events and reminders
- Structured automotive aftersales workflows
- Simulated service booking
- Human handoff preparation
- CRM-ready local business events
- Durable notification delivery
- Optional Twilio WhatsApp transport
- Agentic tool use
- Web and WhatsApp-style interaction
- Explicit confirmation for persistent state changes

The core idea is simple:

> **The assistant should know your car — and help you take care of it over time.**

---

## Why CarMind?

Most automotive assistants answer isolated questions.

CarMind is designed around **continuous ownership context**.

It can reason using:

- Vehicle profile
- Odometer history
- Service history
- Maintenance state
- Driver-reported symptoms
- Diagnostic evidence
- Manufacturer documentation
- Previous conversation context
- Open ownership events
- Proactive maintenance state
- Service intent
- Existing aftersales requests

Manufacturer-specific knowledge stays outside the core architecture.

That means the same CarMind application can work with different vehicles without changing the Python source code.

A developer can provide documentation for different makes and models while keeping the same CarMind core.

---

## Core Capabilities

### Persistent Vehicle Memory

CarMind maintains structured ownership state across conversations, including:

- Vehicle identity
- Model year
- Known powertrain information
- Odometer readings
- Service records
- Maintenance history
- Vehicle-related context
- Notification preferences
- Active ownership events
- Aftersales requests

State is persisted locally using SQLite.

CarMind also supports multiple vehicles while keeping owner and vehicle state isolated.

When more than one stored vehicle could match a request, CarMind can ask the owner to clarify rather than silently selecting the wrong vehicle.

---

### Personalized Vehicle Intelligence

CarMind builds responses from persisted vehicle state instead of relying only on conversation memory.

When relevant, the system can use:

- Make and model
- Model year
- Current mileage
- Mileage freshness
- Known engine/powertrain information
- Service history
- Manufacturer applicability
- Open maintenance events
- Previous confirmed ownership state

Missing information is handled according to the task.

For example, a maintenance question may require current mileage, while a general ownership question may not.

CarMind should not unnecessarily interrogate the user for fields that do not affect the requested task.

It does not invent unavailable vehicle specifications or maintenance facts.

---

### Jev-Powered Semantic Routing

CarMind uses **Jev** as a semantic routing layer to determine which capabilities are relevant to each request.

Instead of exposing the planner to every capability on every turn, Jev evaluates the request against the capability registry and selects only the relevant capability packs.

Current capability areas include:

- Engine
- Battery
- Cooling
- Tires
- Electrical systems
- Fuel economy
- Diagnostic codes
- Maintenance
- Service history
- Trip readiness
- Manufacturer documentation
- Aftersales assistance

This keeps the planner context focused and reduces unnecessary tool exposure.

Jev is responsible only for **semantic relevance**.

It does **not** decide:

- Safety policy
- Maintenance arithmetic
- Manufacturer applicability
- Persistent writes
- Permissions
- Vehicle ownership boundaries
- Notification timing
- Delivery retries

Those responsibilities remain inside deterministic CarMind components.

---

### Manufacturer-Manual RAG

Users can provide their own legally obtained manufacturer documentation.

CarMind can build a local retrieval layer over documents such as:

- Owner's Manual
- Maintenance Schedule
- Warranty / Service Booklet
- Vehicle specifications
- Market-specific manufacturer guidance
- Other manufacturer documentation

Manufacturer answers remain tied to source evidence and document provenance.

Indexed passages preserve information such as:

- Source document
- Document type
- Physical PDF page
- Vehicle association
- Applicability state

No production manufacturer manuals are distributed with this repository.

---

### Manufacturer Document Onboarding

Manufacturer PDFs can be added through the local Web interface or CLI.

CarMind:

1. Validates the PDF
2. Stores it locally
3. Extracts readable page-level text
4. Registers source metadata
5. Builds a local retrieval index
6. Associates the source with the selected vehicle
7. Tracks indexing status
8. Tracks applicability separately
9. Makes manufacturer retrieval available when the source is ready

Manufacturer documents remain local and are excluded from Git.

Scanned PDFs without extractable text are reported as unavailable instead of silently becoming unusable retrieval sources.

---

### Maintenance Intelligence

CarMind supports maintenance workflows such as:

- Recording completed services
- Tracking mileage
- Checking maintenance state
- Generating reminders
- Evaluating verified maintenance schedules
- Recalculating future maintenance after completed service
- Explaining why a reminder exists

Maintenance timing and state transitions are handled deterministically rather than being left entirely to an LLM.

Manufacturer-derived reminders require sufficiently verified and applicable structured maintenance data.

A PDF by itself does **not** automatically become an authoritative maintenance schedule.

---

### Proactive Ownership

CarMind can evaluate ownership state proactively instead of waiting for the driver to ask first.

The proactive engine can detect conditions such as:

- Maintenance due soon
- Maintenance due
- Maintenance overdue
- Stale odometer readings
- Owner-created reminders
- Follow-up opportunities

Example:

```text
Current mileage: 68,500 km
Verified service threshold: 70,000 km

CarMind:
"Your next documented service is getting close. You're about 1,500 km away."
```

If the odometer becomes stale, CarMind asks for an updated reading instead of silently estimating current mileage.

If the owner later records the matching completed service, the relevant maintenance state can be resolved and the next baseline recalculated.

---

## Smart Aftersales Agent

CarMind can move naturally from understanding a vehicle need to helping the owner take the next appropriate step.

This capability is deliberately built on top of the existing ownership, maintenance, safety, manufacturer, planner, and confirmation architecture.

It does not replace them.

### Service Intent

CarMind can recognize service-related intent such as:

- Routine maintenance
- Inspection
- Repair concern
- Diagnostic follow-up
- Warning-light follow-up
- Appointment interest
- Completed maintenance
- Human assistance

Service intent is routed through the same capability/planner architecture rather than a separate keyword-based chatbot.

---

### Structured Service Requests

When an owner wants service, CarMind can prepare a structured service request connected to the correct vehicle.

A request can preserve information such as:

- Vehicle
- Mileage
- Requested service
- Customer-reported symptoms
- Relevant service history
- Evidence references
- Safety state when relevant
- Customer notes
- Preferred timing when supplied

Important state changes continue to use the existing structured confirmation model.

CarMind can prepare the request conversationally without silently committing unrelated persistent changes.

---

### Booking Simulation

CarMind includes a local **booking simulation** for Proof-of-Concept workflows.

It can demonstrate a multi-turn experience such as:

```text
Customer:
"I want to get this service done."

CarMind:
"I can prepare a service request."

Customer:
"Book it."

CarMind:
"Here are the available demo slots..."
```

Any booking produced by this flow is explicitly a:

```text
SIMULATION / DEMO BOOKING
```

It is not represented as a real dealership appointment.

No dealer receives the request.

A real booking workflow would require an authorized dealer integration.

---

### Human Handoff

CarMind can prepare a structured human handoff when a case should move to a service advisor or another human operator.

A handoff packet can include bounded context such as:

- Vehicle summary
- Mileage
- Service intent
- Reported symptoms
- Relevant maintenance history
- Identified maintenance need
- Evidence references
- Safety disposition
- Open questions

The goal is to give a future service advisor useful structured context without requiring them to read an entire conversation.

Creating a local handoff record does not claim that an actual advisor has been contacted.

---

## Proactive Aftersales

Proactive maintenance and aftersales workflows are connected without duplicating the proactive engine.

A typical flow can look like:

```text
Maintenance due
      │
      ▼
Proactive reminder
      │
      ▼
Customer expresses service interest
      │
      ▼
Structured service request
      │
      ├── Demo booking
      │
      └── Human handoff
```

A maintenance reminder never automatically creates a booking.

The customer still chooses the next step.

If the owner later reports that maintenance was completed, CarMind uses its existing confirmed service-history path rather than creating a second maintenance-completion subsystem.

---

## CRM-Ready Business Events

CarMind records lightweight structured events that can support a future CRM integration.

Examples include:

```text
maintenance_due
maintenance_completed
service_interest
booking_intent
service_request_created
human_handoff_requested
```

Events remain local in SQLite in the current implementation.

They are designed to be structured and extensible without turning CarMind into a CRM.

A business event can include bounded identifiers and metadata such as:

```text
event_id
event_type
schema_version
occurred_at
owner_id
vehicle_id
source_channel
source_entity_id
dedupe_key
payload
```

Business events do not store complete conversation histories or provider credentials.

Internal event creation also does not imply permission to send customer data to an external CRM.

No real CRM integration is included in this milestone.

---

### Lightweight Aftersales Analytics

CarMind can summarize owner-scoped aftersales events without requiring a large dashboard or analytics platform.

Useful counts can include:

```text
maintenance_due
maintenance_completed
service_interest
service_request_created
booking_intent
human_handoff_requested
```

The local Web API exposes owner-scoped aftersales analytics through:

```text
/api/aftersales/analytics
```

This layer is intended for engineering demonstrations and future CRM/analytics integration rather than customer scoring or churn prediction.

---

## Events and Notifications Are Separate

CarMind separates **domain truth** from **message delivery**.

For example:

```text
Maintenance event:
Oil service is due

Notification:
A message informing the owner
```

A delivery failure does not change the maintenance event itself.

The persistent notification layer supports states such as:

- Pending
- Deferred
- In flight
- Sent
- Failed
- Cancelled

This allows delivery to fail, retry, or be cancelled without corrupting vehicle state.

---

### Reminder Deduplication and Cooldowns

CarMind avoids repeated reminder spam.

Stable event identities prevent the same maintenance condition from creating duplicate events every time proactive evaluation runs.

Persisted cooldown behavior limits repeated notifications while an event remains unresolved.

Repeated evaluation with unchanged vehicle state remains idempotent.

---

### Quiet Hours and Notification Preferences

Outbound proactive reminders are **disabled by default**.

Owners can configure preferences such as:

- Proactive reminders enabled/disabled
- Preferred channel
- Timezone
- Quiet hours
- Odometer follow-ups

An event can still exist during quiet hours while outbound delivery remains deferred.

Acknowledging a reminder only means:

> "I saw this."

It does **not** mean:

> "The maintenance was completed."

---

### Owner-Created Reminders

CarMind can support reminders created directly by the owner.

Examples:

```text
"Remind me to rotate the tires in 5,000 km."
```

```text
"ذكرني بعد 6 شهور أفحص البطارية."
```

Owner-created reminders remain separate from manufacturer recommendations.

CarMind does not present user-defined reminders as manufacturer guidance.

---

## Deterministic Safety Layer

Safety-critical decisions are separated from unrestricted language-model reasoning.

CarMind uses deterministic rules for supported evidence instead of allowing an LLM to independently declare whether a vehicle is safe to operate.

Possible safety states include:

- Service review
- Stop when safe
- Undetermined
- No deterministic rule triggered

A lack of a triggered rule does **not** mean the vehicle has been declared safe to drive.

Aftersales functionality does not override deterministic safety behavior.

---

## Safe State Changes

CarMind separates conversation from persistent writes.

For important changes, the system first creates a structured proposal.

Examples:

- Record a new odometer reading
- Add a completed service
- Create a confirmed service request
- Confirm a simulated booking
- Update vehicle information

The change is applied only after the required confirmation flow.

A conversational:

```text
yes
```

is not treated as authorization for an unrelated stored-state mutation.

---

## Web + WhatsApp Architecture

CarMind's core is channel-independent.

The same ownership and reasoning architecture can be exposed through:

- Local Web UI
- WhatsApp
- Other future interfaces

The channel layers can share:

- Vehicle state
- Service history
- Manufacturer knowledge
- Proactive events
- Reminder state
- Aftersales requests
- Conversation behavior

This avoids duplicating ownership logic across communication channels.

---

## Architecture

```text
                              ┌────────────────────┐
                              │        User        │
                              └─────────┬──────────┘
                                        │
                         ┌──────────────┴──────────────┐
                         │                             │
                    Web Interface                  WhatsApp
                         │                             │
                         └──────────────┬──────────────┘
                                        │
                                ┌───────▼───────┐
                                │  CarMind App  │
                                └───────┬───────┘
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             │                          │                          │
       Ownership State           Conversation Context       Safety Layer
             │                          │                          │
             └──────────────────────────┼──────────────────────────┘
                                        │
                              ┌─────────▼─────────┐
                              │ Jev Semantic      │
                              │ Router            │
                              └─────────┬─────────┘
                                        │
                             Relevant capabilities
                                        │
                              ┌─────────▼─────────┐
                              │ Bounded Planner   │
                              └─────────┬─────────┘
                                        │
         ┌──────────────────────────────┼──────────────────────────────┐
         │                              │                              │
   Vehicle Tools                Manufacturer RAG               Maintenance
         │                              │                              │
         ├──────────────────────────────┼──────────────────────────────┤
         │                              │                              │
         ▼                              ▼                              ▼
   Aftersales Tools              Evidence Validation             Safety
         │
         ▼
 Service Request
 Booking Simulation
 Human Handoff
 CRM-ready Events
```

Alongside the conversational flow, CarMind maintains a separate proactive delivery path:

```text
Vehicle State
     │
     ▼
Verified Maintenance Data
     │
     ▼
Proactive Evaluator
     │
     ├── Due Soon
     ├── Due
     ├── Overdue
     ├── Stale Mileage
     └── Owner Reminder
             │
             ▼
        Persisted Event
             │
             ▼
     Notification Outbox
             │
             ▼
       Run-Once Runner
        │           │
        ▼           ▼
      Local       WhatsApp
```

The language model is responsible for semantic understanding and bounded reasoning.

Python remains responsible for deterministic behavior such as:

- Safety rules
- Permissions
- Persistent state
- Maintenance calculations
- Source applicability
- Vehicle isolation
- Evidence validation
- Confirmation handling
- Event creation
- Reminder deduplication
- Quiet hours
- Notification state
- Delivery claims
- Retry policy
- Webhook deduplication
- Tool constraints

---

## Vehicle-Agnostic Manufacturer Knowledge

CarMind is not tied to a specific manufacturer or model.

Vehicle-specific documentation is treated as external data.

A generic setup template is available at:

```text
manufacturer_knowledge/_template/
```

The intended workflow is:

1. Add legally obtained manufacturer documents locally.
2. Associate them with a stored vehicle.
3. Register source metadata.
4. Build the local retrieval index.
5. Verify model, year, powertrain, and market applicability.
6. Use manufacturer-grounded retrieval inside CarMind.
7. Activate manufacturer-derived maintenance behavior only when the required applicability conditions are satisfied.

Local manufacturer documents and generated indexes remain outside Git.

---

## Add Your Vehicle Manual

### Web

1. Add your vehicle to CarMind.
2. Open **My Car → Manufacturer documents**.
3. Choose a PDF.
4. Select **Add PDF**.
5. CarMind stores the document locally, extracts readable pages, builds the local index, and reports its status.

Multiple documents can belong to the same vehicle.

Examples:

- Owner's Manual
- Maintenance Schedule
- Warranty / Service Booklet
- Vehicle specifications

---

### CLI

Activate your Python environment first.

#### PowerShell

```powershell
$env:PYTHONPATH="src"

python -m carmind manual `
  --db .local/owner.sqlite3 `
  add `
  --vehicle-id YOUR_VEHICLE_ID `
  --file C:\path\to\manual.pdf
```

Check status:

```powershell
python -m carmind manual `
  --db .local/owner.sqlite3 `
  status `
  --vehicle-id YOUR_VEHICLE_ID
```

List registered documents:

```powershell
python -m carmind manual `
  --db .local/owner.sqlite3 `
  list
```

Rebuild vehicle indexes:

```powershell
python -m carmind manual `
  --db .local/owner.sqlite3 `
  rebuild `
  --vehicle-id YOUR_VEHICLE_ID
```

#### macOS / Linux

```bash
PYTHONPATH=src python -m carmind manual \
  --db .local/owner.sqlite3 \
  add \
  --vehicle-id YOUR_VEHICLE_ID \
  --file /path/to/manual.pdf
```

User-provided metadata does **not** automatically prove that a document applies to a particular vehicle configuration.

---

## Proactive CLI

CarMind does not start a hidden background scheduler.

Proactive execution is explicitly invoked by an operator or external scheduler.

### Evaluate ownership state

```powershell
$env:PYTHONPATH="src"

python -m carmind proactive `
  --db .local/owner.sqlite3 `
  run
```

### List proactive events

```powershell
python -m carmind proactive `
  --db .local/owner.sqlite3 `
  list
```

### Inspect pending notification intents

```powershell
python -m carmind proactive `
  --db .local/owner.sqlite3 `
  pending
```

---

## Scheduled Proactive Runs

CarMind follows a **run-once scheduling model**.

It does not start an internal infinite scheduler or hidden background thread.

An external scheduler decides when CarMind should execute.

Examples include:

- Windows Task Scheduler
- cron
- Container schedulers
- Cloud schedulers
- Manual operator execution

The proactive CLI separates the main operations:

```text
run      → evaluate ownership state only
deliver  → deliver currently eligible notifications only
cycle    → evaluate first, then deliver
```

---

### Safe Concurrent Delivery

Multiple scheduler invocations may overlap.

CarMind protects outbound notifications using persistent SQLite delivery leases.

An eligible notification is atomically claimed before delivery.

Claims are designed so that:

- Two runners cannot successfully claim the same notification simultaneously
- A crashed runner does not lock a notification forever
- Expired claims can be recovered
- An old claimant cannot overwrite a newer claim

Local leasing protects CarMind's own processing.

It does not claim exactly-once delivery at an external provider boundary.

A crash after provider acceptance but before local success persistence may still result in a later retry.

---

### Retry and Backoff

Transient delivery failures use persisted retry state rather than long-running `sleep()` loops.

Delivery state can include:

- Attempt count
- Next retry time
- Delivery result
- Sanitized error category
- Active lease state

Retries are bounded.

A permanent notification failure does not alter the underlying maintenance event.

---

### Delivery Eligibility Is Rechecked

A notification being queued does not guarantee that it will still be sent later.

Immediately before delivery, CarMind rechecks conditions such as:

- Proactive reminders remain enabled
- The selected channel remains valid
- The event remains active
- The scheduled delivery time has arrived
- The owner is outside configured quiet hours

For example:

```text
Oil reminder queued
        ↓
Owner records completed oil service
        ↓
Maintenance event resolves
        ↓
Old notification becomes non-deliverable
```

---

## Twilio WhatsApp Integration

CarMind includes an **optional Twilio WhatsApp adapter** built on top of the existing channel, outbox, and delivery-runner architecture.

Twilio remains a transport layer.

It does not replace:

- CarMind ownership state
- Jev routing
- Planner logic
- Maintenance rules
- Safety rules
- Confirmation handling

The optional project dependency is:

```text
twilio==9.11.2
```

No Twilio credentials are stored in the repository.

---

### Required Configuration

A real Twilio deployment can use environment variables such as:

```text
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_FROM
CARMIND_PUBLIC_WEBHOOK_BASE_URL
```

For proactive sends outside the applicable WhatsApp conversation window, an approved template can optionally be configured through:

```text
TWILIO_WHATSAPP_CONTENT_SID
```

Credentials and sender identifiers must remain outside Git.

---

### Signed Webhooks

The Twilio gateway exposes signed routes for:

```text
POST /webhooks/twilio/whatsapp/inbound
POST /webhooks/twilio/whatsapp/status
```

Inbound webhook processing includes:

```text
signature validation
        ↓
bounded payload parsing
        ↓
MessageSid deduplication
        ↓
owner/channel resolution
        ↓
existing CarMind product flow
        ↓
reply
```

CarMind validates webhook signatures against a configured canonical public origin rather than blindly trusting the incoming Host header.

The signed webhook listener can be enabled explicitly.

Example:

```powershell
$env:PYTHONPATH="src"

python -m carmind.web `
  --db .local/owner.sqlite3 `
  --twilio-webhooks
```

A public HTTPS endpoint or trusted development tunnel is required for real Twilio webhook delivery.

Do not expose the local ownership dashboard publicly merely to receive webhook traffic.

---

### Inbound Deduplication

Twilio may retry webhook requests.

CarMind persists inbound message claims using the provider message identity.

A repeated `MessageSid` does not intentionally trigger:

- Another planner execution
- Duplicate state changes
- Duplicate service requests
- Duplicate business events
- Duplicate normal replies

Inbound processing favors durable duplicate prevention over unrestricted replay.

---

### Delivery Status

Outbound provider messages can be correlated with status callbacks.

Repeated callbacks are handled idempotently.

Delivery state is protected against simple status regression, such as replacing a later delivered/read state with an earlier provider state.

Provider identifiers are stored only where needed for delivery correlation.

---

### Proactive WhatsApp Delivery

Proactive WhatsApp delivery remains opt-in.

An operator can explicitly process eligible WhatsApp notifications through the existing proactive delivery architecture.

Real outbound delivery requires explicit configuration and confirmation.

Without live credentials, CarMind remains fully usable locally.

---

### Live Validation Status

The Twilio integration has automated local/mock coverage.

For the current public milestone:

```text
TWILIO IMPLEMENTATION COMPLETE
LIVE TWILIO DELIVERY NOT VALIDATED
```

No live Twilio send was required for the automated test suite.

The project does not claim exactly-once external delivery.

---

## Enterprise Aftersales Demo

CarMind includes a synthetic offline aftersales demonstration.

Run:

```powershell
$env:PYTHONPATH="src"
python -m carmind.aftersales_demo
```

The demo does not contact:

```text
Twilio
Jev
xAI
a dealership
a CRM
```

Its output can demonstrate:

- Conversation flow
- Vehicle context
- Synthetic maintenance evidence
- Confirmed service request
- Simulated booking
- Local handoff packet
- Business events
- Notification state

Synthetic maintenance data used by the public demo is explicitly non-production test data.

---

### Example Enterprise Journey

Conceptually, the experience can look like:

```text
Customer:
"I have a 2023 vehicle with 70,000 km.
What maintenance should I do?"

        ↓

CarMind resolves:
vehicle identity
mileage
service history
manufacturer applicability

        ↓

Manufacturer-grounded maintenance answer

        ↓

Customer:
"I want to get it serviced."

        ↓

service_interest

        ↓

Structured service-request draft

        ↓

Customer:
"Book it."

        ↓

booking_intent

        ↓

Simulated available slots

        ↓

Confirmed DEMO booking

        ↓

CRM-ready business events
```

For a real manufacturer/model-specific demonstration, a legally obtained applicable local manufacturer source should be configured through the existing onboarding path.

Without applicable evidence, CarMind does not fabricate that vehicle's maintenance schedule.

---

## Running the Offline Demo

The original ownership demo remains available.

### PowerShell

```powershell
$env:PYTHONPATH="src"
python -m carmind --demo
```

### macOS / Linux

```bash
PYTHONPATH=src python -m carmind --demo
```

The demo uses fictional data and does not require API keys or network access.

---

## Manufacturer Documentation Is Optional

CarMind can operate without manufacturer documentation.

Without an applicable manufacturer source:

```text
✓ Vehicle memory
✓ Mileage tracking
✓ Service history
✓ Diagnostics
✓ Deterministic safety
✓ Web interface
✓ WhatsApp-style interaction
✓ Aftersales workflow
✓ Owner-created reminders
✓ Stale-mileage follow-up

✗ Manufacturer-specific RAG
✗ Manufacturer-grounded specifications
✗ Manufacturer-derived maintenance schedules
```

Once a valid manufacturer source is configured, its retrieval capability becomes available for the associated vehicle.

CarMind does not invent missing manufacturer specifications.

---

## Testing

CarMind includes unit, integration, regression, migration, routing, ownership, product, aftersales, proactive-delivery, and WhatsApp gateway coverage.

Current validated suite:

```text
435 tests passed
333 subtests passed
0 failures
```

The full automated suite can run without:

```text
Private manufacturer documents
External network access
Live Jev calls
Live xAI calls
Live Twilio calls
Developer-specific vehicle data
```

The Milestone 11 synthetic enterprise demo also completed successfully.

Run the suite with:

### PowerShell

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
```

### macOS / Linux

```bash
PYTHONPATH=src python -m pytest -q
```

---

## Project Structure

```text
CarMind/
├── capabilities/
│   ├── aftersales/                   # Aftersales capability manifest
│   ├── maintenance/
│   └── manufacturer_manual/
├── data/                             # Safety and action configuration
├── eval/                             # Evaluation scenarios and fixtures
├── manufacturer_knowledge/
│   └── _template/                    # Generic manufacturer setup template
├── scripts/
│   └── run_proactive_cycle.ps1       # Portable run-once scheduler wrapper
├── src/carmind/
│   ├── app.py                        # Core application composition
│   ├── ownership.py                  # Ownership and vehicle state
│   ├── storage.py                    # SQLite persistence and migrations
│   ├── routing.py                    # Capability routing
│   ├── planner.py                    # Bounded planning loop
│   ├── tools.py                      # Tool execution
│   ├── safety.py                     # Deterministic safety logic
│   ├── maintenance.py                # Maintenance intelligence
│   ├── manufacturer_manual.py        # Manufacturer-document RAG
│   ├── manufacturer_ingestion.py     # PDF onboarding and indexing
│   ├── manual_cli.py                 # Manufacturer document CLI
│   ├── proactive.py                  # Proactive ownership engine
│   ├── proactive_runner.py           # Run-once delivery runner
│   ├── proactive_cli.py              # Proactive operator CLI
│   ├── aftersales.py                 # Aftersales domain workflows
│   ├── aftersales_demo.py            # Offline enterprise demo
│   ├── twilio_gateway.py             # Optional Twilio WhatsApp transport
│   ├── product.py                    # Product interaction layer
│   ├── web.py                        # Local Web / webhook entry points
│   ├── web_static/                   # Local Web UI
│   └── whatsapp.py                   # WhatsApp channel abstraction
└── tests/
    ├── test_aftersales.py
    ├── test_twilio_gateway.py
    └── ...
```

---

## Design Principles

**LLMs handle semantics — not safety-critical policy.**

**Jev handles relevance — not ownership, safety, state mutation, or delivery policy.**

**Manufacturer facts come from evidence — not model memory.**

**Manufacturer applicability is explicit.**

**Persistent writes require structured confirmation.**

**Vehicle-specific knowledge stays outside the core.**

**Maintenance calculations remain deterministic.**

**Events are separate from notification delivery.**

**Aftersales actions are structured workflows rather than free-form model side effects.**

**Booking simulation never pretends to be a real dealer appointment.**

**Proactive messaging is opt-in.**

**Scheduling is external and run-once.**

**Notification claims are durable and transactional.**

**Retries are persisted rather than implemented as sleeping background loops.**

**Twilio is a transport adapter — not the CarMind application core.**

**The product remains useful without manufacturer documents or live providers.**

**The user experience stays simple even when the backend is complex.**

---

## Current Status

CarMind is an active proof-of-concept and engineering project focused on reliable AI-assisted vehicle ownership and automotive aftersales workflows.

The current version demonstrates:

```text
Stateful vehicle ownership
Jev semantic capability routing
Bounded agentic planning
Manufacturer-grounded retrieval
Manufacturer PDF onboarding
Vehicle-specific knowledge isolation
Personalized vehicle context
Multi-vehicle clarification
Deterministic safety
Deterministic maintenance
Proactive ownership events
Structured service requests
Booking simulation
Human handoff packets
CRM-ready business events
Aftersales analytics
Persistent notification state
Run-once proactive execution
Durable notification claims
Retry and backoff
Crash recovery
Overlapping-run protection
Signed Twilio webhook support
Inbound message deduplication
Delivery-status correlation
Web interaction
WhatsApp integration boundary
Database migration and restart durability
```

CarMind does **not** currently claim:

```text
A real dealer booking integration
A real CRM integration
Exactly-once external message delivery
Production dealer deployment
Live Twilio validation
```

---

## Roadmap

Planned areas include:

- Live Twilio validation
- Authorized dealer booking integration
- CRM/DMS integration adapters
- Production-grade asynchronous webhook processing
- Broader manufacturer-document ingestion
- OCR support for scanned manuals
- Improved applicability verification
- Vehicle telemetry / OBD-II integration
- Workshop preparation and service summaries
- Production observability
- Expanded evaluation
- Improved multilingual interaction
- Production deployment hardening

---

## Privacy & Local Data

CarMind is designed so vehicle-specific ownership data can remain local.

The public repository does not include:

- Personal ownership databases
- User phone numbers
- Uploaded manufacturer PDFs
- Private notification histories
- Provider credentials
- Private manufacturer indexes

Runtime state should remain under ignored local paths such as:

```text
.local/
```

Manufacturer PDFs are not sent to external providers as part of the local ingestion pipeline.

Operational records are designed to contain bounded, sanitized information rather than secrets or complete conversation histories.

CRM-ready events do not authorize external data sharing by themselves.

---

## Safety Boundaries

CarMind is not a substitute for:

- Official manufacturer documentation
- Qualified automotive technicians
- Professional vehicle inspection
- Emergency services

Manufacturer-derived maintenance behavior requires verified applicability.

Unverified documentation may support appropriately qualified retrieval, but it should not automatically become authoritative maintenance guidance.

Safety-critical vehicle decisions remain outside unrestricted language-model reasoning.

Aftersales and booking workflows do not override safety policy.

---

## Author

**Omar Al-Zoubi**

AI Engineer focused on:

- Generative AI
- RAG
- Agentic systems
- Multimodal AI
- AI system architecture
- Production-oriented AI engineering

GitHub: [OmarIAlzoubi](https://github.com/OmarIAlzoubi)

---

## Disclaimer

CarMind is currently a proof-of-concept.

Vehicle information, diagnostics, maintenance guidance, proactive reminders, service-request workflows, booking simulations, and safety-related outputs must not be treated as a replacement for official manufacturer documentation, professional diagnosis, qualified automotive service, or a confirmed dealership appointment.