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
- Agentic tool use
- Web and WhatsApp-style interaction
- Explicit confirmation for persistent state changes

The core idea is simple:

> **The assistant should know your car — and help you take care of it over time.**

---

## Why CarMind?

Most automotive assistants answer isolated questions.

CarMind is designed around continuous ownership context.

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

Manufacturer-specific knowledge remains outside the core architecture.

That means CarMind can work with different vehicles without changing the Python code.

A developer can supply documents for a Toyota, BMW, Hyundai, Ford, or another vehicle while keeping the same CarMind core.

---

# Core Capabilities

## Persistent Vehicle Memory

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

## Jev-Powered Semantic Routing

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

Those responsibilities remain inside deterministic CarMind components.

---

## Manufacturer-Manual RAG

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

## Manufacturer Document Onboarding

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

Scanned PDFs without extractable text are reported as unavailable instead of silently becoming unusable search sources.

---

## Maintenance Intelligence

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

## Proactive Ownership

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

## Events and Notifications Are Separate

CarMind separates **domain truth** from **message delivery**.

For example:

```text
Maintenance event:
Oil service is due

Notification:
WhatsApp/Web message informing the owner
```

A delivery failure does not change the maintenance event itself.

The persistent outbox supports notification states such as:

- Pending
- Deferred
- Sent
- Failed
- Cancelled

This allows notification delivery to fail or retry without corrupting vehicle state.

---

## Reminder Deduplication and Cooldowns

CarMind avoids repeated reminder spam.

Stable event identities prevent the same maintenance condition from creating duplicate events every time proactive evaluation runs.

Persisted cooldown behavior can limit repeated notifications while an event remains unresolved.

Running the evaluator repeatedly with unchanged vehicle state remains idempotent.

---

## Quiet Hours and Notification Preferences

Outbound proactive reminders are **disabled by default**.

Owners can configure preferences such as:

- Proactive reminders enabled/disabled
- Preferred channel
- Timezone
- Quiet hours
- Odometer follow-ups

An event can still be recorded during quiet hours, while delivery is deferred until an allowed time.

Acknowledging a reminder only means:

> "I saw this."

It does **not** mean:

> "The maintenance was completed."

---

## Owner-Created Reminders

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

## Deterministic Safety Layer

Safety-critical decisions are separated from language-model reasoning.

CarMind uses deterministic rules for supported evidence instead of allowing an LLM to independently declare whether a vehicle is safe to operate.

Possible safety outcomes include states such as:

- Service review
- Stop when safe
- Undetermined
- No deterministic rule triggered

A lack of a triggered rule does **not** mean the vehicle has been declared safe to drive.

---

## Safe State Changes

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

## Web + WhatsApp Architecture

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

# Architecture

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
        │            │
        ▼            ▼
       Web       WhatsApp-style
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
- Tool constraints

---

# Vehicle-Agnostic Manufacturer Knowledge

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

# Add Your Vehicle Manual

## Web

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

## CLI

Activate your Python environment first.

### PowerShell

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

### macOS / Linux

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

# Proactive CLI

CarMind does not start a hidden background scheduler.

Proactive evaluation is explicitly invoked by an operator or future scheduler.

### PowerShell

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

The CLI evaluates and inspects local proactive state.

It does not automatically start a daemon or send live outbound messages.

---

# Running the Offline Demo

The repository includes a fully offline scripted ownership demo.

## PowerShell

```powershell
$env:PYTHONPATH="src"
python -m carmind --demo
```

## macOS / Linux

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

# Manufacturer Documentation Is Optional

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

# Testing

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
- Database migration
- Multi-vehicle isolation
- Multi-owner isolation

Current validated suite:

```text
401 tests passed
333 subtests passed
```

The test suite runs without relying on:

- Private manufacturer documents
- Network access
- Live Jev calls
- Live LLM provider calls
- Twilio
- Developer-specific vehicle data

Synthetic manufacturer fixtures are included for reproducible testing.

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

# Project Structure

```text
CarMind/
├── capabilities/                     # Capability manifests
├── data/                             # Safety and action configuration
├── eval/                             # Evaluation cases and synthetic fixtures
├── manufacturer_knowledge/
│   └── _template/                    # Generic manufacturer setup template
├── scripts/                          # Development utilities
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
│   ├── proactive_cli.py              # Proactive operator CLI
│   ├── product.py                    # Product interaction layer
│   ├── web.py                        # Local Web interface
│   ├── web_static/                   # Web UI
│   └── whatsapp.py                   # WhatsApp-style channel adapter
└── tests/
```

---

# Design Principles

CarMind follows several architectural rules:

**LLMs handle semantics — not safety-critical policy.**

**Jev handles relevance — not ownership, safety, or state mutation.**

**Manufacturer facts come from evidence — not model memory.**

**Manufacturer applicability is explicit.**

**Persistent writes require structured confirmation.**

**Vehicle-specific knowledge stays outside the core.**

**Maintenance calculations remain deterministic.**

**Events are separate from notification delivery.**

**Proactive messaging is opt-in.**

**The product remains useful even without manufacturer documents.**

**The user experience stays simple even when the backend is complex.**

---

# Current Status

CarMind is an active proof-of-concept and engineering project focused on reliable AI-assisted vehicle ownership.

The current version demonstrates:

- Stateful vehicle ownership
- Jev semantic capability routing
- Bounded agentic planning
- Manufacturer-grounded retrieval
- PDF onboarding
- Vehicle-specific knowledge isolation
- Deterministic safety boundaries
- Deterministic maintenance intelligence
- Proactive ownership events
- Persistent notification state
- Web interaction
- WhatsApp-style interaction
- Multi-vehicle state
- Database migration and restart durability

CarMind does **not** currently run an always-on scheduler by itself.

Proactive evaluation must be invoked explicitly by an operator or external scheduler.

Live outbound WhatsApp delivery is also not enabled automatically.

---

# Roadmap

Planned areas include:

- Scheduler / periodic proactive runner
- Real outbound WhatsApp transport
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

# Privacy & Local Data

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

---

# Safety Boundaries

CarMind is not a substitute for:

- Official manufacturer documentation
- Qualified automotive technicians
- Professional vehicle inspection
- Emergency services

Manufacturer-derived maintenance behavior requires verified applicability.

Unverified documentation may still support retrieval with appropriate caveats, but it should not automatically become authoritative maintenance guidance.

Safety-critical vehicle decisions remain outside unrestricted language-model reasoning.

---

# Author

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

# Disclaimer

CarMind is currently a proof-of-concept.

Vehicle information, diagnostics, maintenance guidance, proactive reminders, and safety-related outputs must not be treated as a replacement for official manufacturer documentation, professional diagnosis, or qualified automotive service.