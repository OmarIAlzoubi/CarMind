# CarMind

**An AI-powered personal car ownership assistant that understands your vehicle, remembers its history, grounds manufacturer-specific answers in your own documentation, and proactively follows up on maintenance.**

CarMind is a vehicle-agnostic AI system designed to make car ownership easier for everyday drivers.

Instead of acting as a generic automotive chatbot, CarMind combines conversational AI with:

- Persistent vehicle memory
- Jev-powered semantic routing
- Manufacturer-document RAG
- Deterministic maintenance intelligence
- Deterministic safety logic
- Proactive ownership events and reminders
- Durable notification delivery
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

Manufacturer-specific knowledge stays outside the core architecture.

That means the same CarMind application can work with different vehicles without changing the Python source code.

A developer can provide documentation for a Toyota, BMW, Hyundai, Ford, or another vehicle while keeping the same core system.

---

## Core Capabilities

### Persistent Vehicle Memory

CarMind maintains structured ownership state across conversations, including:

- Vehicle identity
- Odometer readings
- Service records
- Maintenance history
- Vehicle-related context
- Notification preferences
- Active ownership events

State is persisted locally using SQLite.

CarMind also supports multiple vehicles while keeping vehicle-specific state isolated.

---

### Jev-Powered Semantic Routing

CarMind uses **Jev** as a semantic routing layer to determine which capabilities are relevant to each user request.

Instead of exposing the planner to every available capability on every turn, Jev evaluates the request against the capability registry and selects only the relevant capability packs.

Available areas include:

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

This keeps the reasoning context focused and reduces unnecessary prompt size and tool exposure.

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
9. Makes the manual capability available when the source is ready

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

Manufacturer-derived reminders require a sufficiently verified and applicable structured maintenance schedule.

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
Current mileage: 18,500 km
Verified service threshold: 20,000 km

CarMind:
"Your oil service is getting close. You're about 1,500 km away."
```

If the odometer becomes stale, CarMind asks for an updated reading instead of silently estimating current mileage.

If the owner later records the matching completed service, the relevant reminder can be resolved and the next maintenance baseline recalculated.

---

### Events and Notifications Are Separate

CarMind separates **domain truth** from **message delivery**.

For example:

```text
Maintenance event:
Oil service is due

Notification:
A message informing the owner
```

A delivery failure does not change the maintenance event itself.

The persistent outbox supports delivery states such as:

- Pending
- Deferred
- In flight
- Sent
- Failed
- Cancelled

This allows delivery to fail, retry, or be cancelled without corrupting the underlying vehicle state.

---

### Reminder Deduplication and Cooldowns

CarMind avoids repeated reminder spam.

Stable event identities prevent the same maintenance condition from creating duplicate events every time proactive evaluation runs.

Persisted cooldown behavior limits repeated notifications while an event remains unresolved.

Running the evaluator repeatedly with unchanged vehicle state remains idempotent.

---

### Quiet Hours and Notification Preferences

Outbound proactive reminders are **disabled by default**.

Owners can configure preferences such as:

- Proactive reminders enabled/disabled
- Preferred channel
- Timezone
- Quiet hours
- Odometer follow-ups

An event can still be recorded during quiet hours while delivery is deferred until an allowed time.

Acknowledging a reminder only means:

> "I saw this."

It does **not** mean:

> "The maintenance was completed."

---

### Owner-Created Reminders

CarMind can also support reminders that come from the owner rather than the manufacturer.

For example:

```text
"Remind me to rotate the tires in 5,000 km."
```

or:

```text
"ذكرني بعد 6 شهور أفحص البطارية."
```

Owner-created reminders remain clearly separated from manufacturer guidance.

CarMind does not present them as manufacturer recommendations.

---

### Deterministic Safety Layer

Safety-critical decisions are separated from language-model reasoning.

CarMind uses deterministic rules for supported evidence instead of allowing an LLM to independently declare whether a vehicle is safe to operate.

Possible safety outcomes include states such as:

- Service review
- Stop when safe
- Undetermined
- No deterministic rule triggered

A lack of a triggered rule does **not** mean the vehicle has been declared safe to drive.

---

### Safe State Changes

CarMind separates conversation from persistent writes.

For important changes, the system first generates a structured proposal.

Examples:

- Record a new odometer reading
- Add a completed service
- Update vehicle information

The change is applied only after explicit confirmation.

A simple conversational:

```text
yes
```

is not treated as sufficient authorization for an unrelated stored-state mutation.

This remains true even when the conversation started from a proactive reminder.

---

### Web + WhatsApp Architecture

CarMind's core is channel-independent.

The same ownership state and reasoning pipeline can be exposed through:

- Local Web UI
- WhatsApp-style messaging
- Future channels

The Web and WhatsApp layers share:

- Vehicle state
- Service history
- Manufacturer knowledge
- Proactive events
- Reminder state
- Conversation behavior

This avoids duplicating ownership logic across interfaces.

---

## Architecture

```text
                              ┌────────────────────┐
                              │        User        │
                              └─────────┬──────────┘
                                        │
                         ┌──────────────┴──────────────┐
                         │                             │
                    Web Interface               WhatsApp Adapter
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
            ┌───────────────────────────┼───────────────────────────┐
            │                           │                           │
      Vehicle Tools              Manufacturer RAG           Maintenance
            │                           │                           │
            └───────────────────────────┼───────────────────────────┘
                                        │
                               Validated Response
```

Alongside the conversational flow, CarMind maintains a separate proactive ownership path:

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
         │         │
         ▼         ▼
        Web    Outbound Channel
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

Optional metadata such as model year, market, language, and document type can be supplied during onboarding.

User-provided metadata does **not** automatically prove that a document applies to a particular vehicle configuration.

---

## Proactive CLI

CarMind does not start a hidden background scheduler.

Proactive evaluation is explicitly invoked by an operator or external scheduler.

### PowerShell

Evaluate proactive state only:

```powershell
$env:PYTHONPATH="src"

python -m carmind proactive `
  --db .local/owner.sqlite3 `
  run
```

List proactive events:

```powershell
python -m carmind proactive `
  --db .local/owner.sqlite3 `
  list
```

Inspect pending notification intents:

```powershell
python -m carmind proactive `
  --db .local/owner.sqlite3 `
  pending
```

The CLI evaluates and inspects local proactive state without starting a daemon.

---

## Scheduled Proactive Runs

CarMind follows a **run-once scheduling model**.

It does not run an internal infinite scheduler or hidden background thread.

Instead, an external scheduler decides when CarMind should run.

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

This keeps infrastructure outside the CarMind core while preserving deterministic ownership and delivery behavior.

---

### Safe Concurrent Delivery

Multiple scheduler invocations may overlap.

CarMind protects outbound notifications using persistent SQLite delivery leases.

An eligible notification is atomically claimed before delivery.

Each claim has bounded ownership information so that:

- Two runners cannot successfully claim the same notification simultaneously
- A crashed runner does not leave a notification locked forever
- Expired claims can be recovered later
- An old runner cannot finalize a notification after another runner acquires a newer claim

This prevents overlapping local workers from double-processing the same notification.

For external delivery providers, CarMind can expose the stable notification ID for provider-side idempotency when supported.

> Local storage alone cannot guarantee exactly-once external delivery if a process crashes after a provider accepts a message but before CarMind records the successful result.

Provider-side idempotency should therefore be used when available.

---

### Retry and Backoff

Transient delivery failures are retried deterministically.

Retry state is persisted rather than handled with long-running `sleep()` loops.

Delivery state can include information such as:

- Attempt count
- Next retry time
- Delivery result
- Sanitized error category
- Active lease state

Retries are bounded.

Permanent notification failure does not change the underlying maintenance or ownership event.

---

### Delivery Eligibility Is Rechecked

A queued notification is not automatically guaranteed to send later.

Immediately before delivery, CarMind rechecks conditions such as:

- Proactive reminders are still enabled
- The selected channel is still valid
- The underlying event remains active
- The notification has reached its scheduled time
- The owner is outside configured quiet hours

Example:

```text
Oil reminder is queued
        ↓
Owner records completed oil service
        ↓
Oil event is resolved
        ↓
Queued notification becomes non-deliverable
```

CarMind does not intentionally send stale reminders simply because they were queued earlier.

---

### Windows Task Scheduler

The repository includes:

```text
scripts/run_proactive_cycle.ps1
```

A Windows Task Scheduler action can invoke one CarMind cycle.

Example arguments:

```powershell
-NoProfile -File "C:\path\to\CarMind\scripts\run_proactive_cycle.ps1" `
  -PythonExe "C:\path\to\python.exe" `
  -DatabasePath "C:\path\to\owner.sqlite3" `
  -Limit 50
```

Replace all paths with your own environment paths.

The script runs one cycle and exits.

It does **not** install or configure Windows Task Scheduler automatically.

---

### cron

Linux/macOS deployments can invoke the same run-once cycle through cron.

Example:

```text
0 * * * * cd /path/to/CarMind && PYTHONPATH=src /path/to/python -m carmind proactive --db /path/to/owner.sqlite3 cycle --limit 50 --console --json
```

The scheduling frequency is intentionally external.

CarMind does not assume that proactive evaluation must run hourly, daily, or at any other universal frequency.

---

### Dry Run

Operators can preview a cycle without sending notifications or consuming delivery attempts.

```powershell
python -m carmind proactive `
  --db .local/owner.sqlite3 `
  cycle `
  --dry-run
```

This is useful for local verification and deployment testing.

---

### Machine-Readable Reports

Scheduler runs can produce structured JSON for monitoring and future automation.

```powershell
python -m carmind proactive `
  --db .local/owner.sqlite3 `
  cycle `
  --limit 50 `
  --json
```

A cycle report can include operational information such as:

- Owners evaluated
- Vehicles evaluated
- Events created
- Events updated
- Notifications created
- Notifications claimed
- Notifications sent
- Notifications deferred
- Notifications retried
- Notifications failed
- Notifications cancelled

Provider secrets are not included in cycle reports.

---

### Current Delivery Boundary

The public scheduler examples use local, fake, or explicit console delivery.

No live WhatsApp transport is activated automatically.

A production outbound provider can be connected through the notification transport layer without changing the proactive ownership engine.

---

## Running the Offline Demo

The repository includes a fully offline scripted ownership demo.

### PowerShell

```powershell
$env:PYTHONPATH="src"
python -m carmind --demo
```

### macOS / Linux

```bash
PYTHONPATH=src python -m carmind --demo
```

The demo does not require API keys or network access.

It uses fictional data to demonstrate:

- Vehicle ownership state
- Mileage updates
- Service recording
- Maintenance behavior
- Diagnostic evidence
- Safety follow-up
- Persistent SQLite state

---

## Manufacturer Documentation Is Optional

CarMind can start and operate without manufacturer documentation.

Without a manufacturer source:

```text
✓ Vehicle memory
✓ Mileage tracking
✓ Service history
✓ Diagnostics
✓ Deterministic safety
✓ Web interface
✓ WhatsApp-style interaction
✓ Owner-created reminders
✓ Stale-mileage follow-up

✗ Manufacturer-specific RAG
✗ Manufacturer-grounded specifications
✗ Manufacturer-derived maintenance reminders
```

Once a valid manufacturer source is configured, its retrieval capability becomes available for the associated vehicle.

CarMind does not invent missing manufacturer specifications.

---

## Testing

CarMind includes unit and integration coverage across:

- Ownership state
- Routing
- Planner behavior
- Tools
- Safety
- Maintenance
- Manufacturer knowledge
- Manufacturer ingestion
- Product behavior
- Web behavior
- WhatsApp-style interaction
- Proactive ownership
- Notification outbox
- Notification leasing
- Delivery retries
- Crash recovery
- Database migration
- Multi-vehicle isolation
- Multi-owner isolation
- Concurrent runner behavior

Current validated suite:

```text
415 tests passed
333 subtests passed
```

The test suite runs without relying on:

- Private manufacturer documents
- Network access
- Live Jev calls
- Live LLM provider calls
- Twilio
- Developer-specific vehicle data

Synthetic manufacturer and delivery fixtures are used for reproducible testing.

Run:

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
├── capabilities/                     # Capability manifests
├── data/                             # Safety and action configuration
├── eval/                             # Evaluation cases and synthetic fixtures
├── manufacturer_knowledge/
│   └── _template/                    # Generic manufacturer setup template
├── scripts/
│   └── run_proactive_cycle.ps1       # Portable run-once scheduler wrapper
├── src/carmind/
│   ├── app.py                        # Core application layer
│   ├── ownership.py                  # Ownership state
│   ├── storage.py                    # SQLite persistence and migrations
│   ├── routing.py                    # Capability routing
│   ├── planner.py                    # Bounded planning loop
│   ├── tools.py                      # Tool execution
│   ├── safety.py                     # Deterministic safety logic
│   ├── maintenance.py                # Maintenance calculations
│   ├── manufacturer_manual.py        # Manufacturer-document RAG
│   ├── manufacturer_ingestion.py     # PDF onboarding and indexing
│   ├── manual_cli.py                 # Manufacturer document CLI
│   ├── proactive.py                  # Proactive ownership engine
│   ├── proactive_runner.py           # Run-once delivery/evaluation runner
│   ├── proactive_cli.py              # Proactive operator CLI
│   ├── product.py                    # Product interaction layer
│   ├── web.py                        # Local Web interface
│   ├── web_static/                   # Web UI
│   └── whatsapp.py                   # WhatsApp-style channel adapter
└── tests/
```

---

## Design Principles

CarMind follows several architectural rules:

**LLMs handle semantics — not safety-critical policy.**

**Jev handles relevance — not ownership, safety, state mutation, or delivery policy.**

**Manufacturer facts come from evidence — not model memory.**

**Manufacturer applicability is explicit.**

**Persistent writes require structured confirmation.**

**Vehicle-specific knowledge stays outside the core.**

**Maintenance calculations remain deterministic.**

**Events are separate from notification delivery.**

**Proactive messaging is opt-in.**

**Scheduling is external and run-once.**

**Notification claims are durable and transactional.**

**Retries are persisted rather than implemented as sleeping background loops.**

**The product remains useful even without manufacturer documents.**

**The user experience stays simple even when the backend is complex.**

---

## Current Status

CarMind is an active proof-of-concept and engineering project focused on reliable AI-assisted vehicle ownership.

The current version demonstrates:

- Stateful vehicle ownership
- Jev semantic capability routing
- Bounded agentic planning
- Manufacturer-grounded retrieval
- Manufacturer PDF onboarding
- Vehicle-specific knowledge isolation
- Deterministic safety boundaries
- Deterministic maintenance intelligence
- Proactive ownership events
- Persistent notification state
- Run-once proactive execution
- Durable notification claims
- Retry and backoff behavior
- Crash recovery
- Overlapping-run protection
- Web interaction
- WhatsApp-style interaction
- Multi-vehicle state
- Database migrations
- Restart durability

CarMind does **not** currently run an internal always-on daemon.

Proactive cycles are invoked by an operator or external scheduler.

Live outbound WhatsApp delivery is also not enabled automatically.

---

## Roadmap

Planned areas include:

- Real outbound WhatsApp transport
- Provider-side delivery idempotency
- Delivery receipts and status synchronization
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

The repository does not include:

- Personal ownership databases
- User phone numbers
- Uploaded manufacturer PDFs
- Notification histories
- Provider credentials
- Private manufacturer indexes

Runtime state should remain under ignored local paths such as:

```text
.local/
```

Manufacturer PDFs are not sent to external providers as part of the local ingestion pipeline.

Operational delivery records should contain bounded, sanitized information rather than secrets or full conversation histories.

---

## Safety Boundaries

CarMind is not a substitute for:

- Official manufacturer documentation
- Qualified automotive technicians
- Professional vehicle inspection
- Emergency services

Manufacturer-derived maintenance behavior requires verified applicability.

Unverified documentation may still support retrieval with appropriate caveats, but it should not automatically become authoritative maintenance guidance.

Safety-critical vehicle decisions remain outside unrestricted language-model reasoning.

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

Vehicle information, diagnostics, maintenance guidance, proactive reminders, and safety-related outputs must not be treated as a replacement for official manufacturer documentation, professional diagnosis, or qualified automotive service.