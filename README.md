# CarMind

**An AI-powered personal car ownership assistant that understands your vehicle, remembers its history, and grounds manufacturer-specific answers in your own documentation.**

CarMind is a vehicle-agnostic AI system designed to make car ownership easier for everyday drivers.

Instead of acting as a generic automotive chatbot, CarMind combines conversational AI with persistent vehicle memory, manufacturer-document retrieval, maintenance intelligence, deterministic safety logic, and tool-based workflows.

Users can interact with the same ownership state through a Web interface or WhatsApp-style channel.

---

## Why CarMind?

Most automotive assistants answer isolated questions.

CarMind is designed around a different idea:

> **The assistant should know your car.**

It keeps track of the vehicle over time and can reason using:

- Vehicle profile
- Mileage
- Service history
- Maintenance state
- Driver-reported symptoms
- Diagnostic evidence
- Manufacturer documentation
- Previous conversation context

Manufacturer-specific knowledge is kept outside the core architecture, allowing CarMind to work with different vehicles without changing the Python code.

---

## Core Capabilities

### Persistent Vehicle Memory

CarMind maintains structured ownership state across conversations, including:

- Vehicle identity
- Odometer readings
- Service records
- Maintenance history
- Vehicle-related context

State is persisted locally using SQLite.

---

### Manufacturer-Manual RAG

Users can provide their own legally obtained manufacturer documentation.

CarMind can build a local retrieval layer over documents such as:

- Owner's Manual
- Maintenance Schedule
- Warranty / Service Booklet
- Vehicle specifications
- Market-specific manufacturer guidance

Manufacturer answers remain tied to source evidence and applicability.

No production manufacturer manuals are distributed with this repository.

---

### Maintenance Intelligence

CarMind supports maintenance workflows such as:

- Recording completed services
- Tracking mileage
- Checking maintenance state
- Generating reminders
- Referencing manufacturer schedules when an applicable source is available

Maintenance timing and state transitions are handled deterministically rather than being left entirely to an LLM.

---

### Deterministic Safety Layer

Safety-critical decisions are separated from language-model reasoning.

CarMind uses deterministic safety rules for supported evidence instead of allowing an LLM to independently decide whether a vehicle is safe to operate.

Possible safety outcomes include states such as:

- Service review
- Stop when safe
- Undetermined
- No deterministic rule triggered

A lack of a triggered rule does **not** mean the vehicle has been declared safe to drive.

---

### Agentic Tool Use

CarMind uses a bounded planner and capability-based tool system.

Capabilities include areas such as:

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

Semantic routing selects relevant capabilities while deterministic Python logic handles permissions, validation, safety boundaries, and state mutation.

---

### Safe State Changes

CarMind separates conversation from persistent writes.

For important changes, the system generates a structured proposal first.

Examples:

- Record a new odometer reading
- Add a completed service
- Update vehicle information

The change is applied only after explicit confirmation.

A simple conversational `"yes"` is not treated as sufficient authorization for an unrelated stored-state mutation.

---

### Web + WhatsApp Architecture

CarMind's core is channel-independent.

The same application state and reasoning pipeline can be exposed through:

- Local Web UI
- WhatsApp-style messaging
- Other interfaces in the future

This avoids duplicating ownership logic across communication channels.

---

## Architecture

```text
                        ┌─────────────────────┐
                        │        User         │
                        └──────────┬──────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │                             │
               Web Interface                WhatsApp
                    │                             │
                    └──────────────┬──────────────┘
                                   │
                          ┌────────▼────────┐
                          │   CarMind App   │
                          └────────┬────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
               Conversation     Ownership      Safety
                 Context          State          Layer
                    │              │              │
                    └──────────────┼──────────────┘
                                   │
                          ┌────────▼────────┐
                          │ Planner / Router│
                          └────────┬────────┘
                                   │
                     Relevant capability packs
                                   │
             ┌─────────────────────┼─────────────────────┐
             │                     │                     │
         Vehicle Tools       Manufacturer RAG      Maintenance
             │                     │                     │
             └─────────────────────┼─────────────────────┘
                                   │
                           Validated Response
```

The language model is responsible for semantic understanding and bounded reasoning.

Python remains responsible for deterministic behavior such as:

- Safety rules
- Permissions
- State mutation
- Maintenance calculations
- Evidence validation
- Confirmation handling
- Tool constraints

---

## Vehicle-Agnostic Manufacturer Knowledge

CarMind is not tied to a specific manufacturer or model.

A developer can provide local documentation for their own vehicle and register it through manufacturer metadata.

Example structure:

```text
manufacturer_knowledge/
└── your_vehicle/
    ├── manual_source.json
    ├── manual_facts.json
    └── local_documents/
```

A generic setup template is available at:

```text
manufacturer_knowledge/_template/
```

The intended workflow is:

1. Add legally obtained manufacturer documents locally.
2. Register the source metadata.
3. Build the local retrieval index.
4. Verify model, year, powertrain, and market applicability.
5. Use manufacturer-grounded retrieval inside CarMind.
6. Activate maintenance guidance only when applicability is verified.

Local manufacturer documents and generated indexes should remain outside Git.

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

It uses fictional test data to demonstrate:

- Vehicle ownership state
- Mileage updates
- Service recording
- Maintenance reminders
- Diagnostic evidence
- Safety follow-up
- Persistent SQLite state

---

## Manufacturer Documentation Is Optional

CarMind can start and operate without a manufacturer manual.

Without manufacturer documents:

```text
✓ Vehicle memory
✓ Mileage tracking
✓ Service history
✓ Diagnostics
✓ Safety logic
✓ Web interface
✓ WhatsApp-style interaction

✗ Manufacturer-specific RAG
✗ Manufacturer-grounded specifications
```

Once a valid manufacturer source is configured, the manufacturer capability becomes available automatically.

CarMind does not invent missing manufacturer specifications.

---

## Testing

CarMind includes unit, integration, safety, routing, ownership, product, and manufacturer-retrieval tests.

Current public test suite:

```text
372 passed
```

The public repository is tested without relying on:

- Private manufacturer documents
- Network access
- Live LLM providers
- Twilio
- Developer-specific vehicle data

Synthetic manufacturer fixtures are included specifically for reproducible testing.

Run the suite with:

```bash
PYTHONPATH=src python -m pytest -q
```

---

## Project Structure

```text
CarMind/
├── capabilities/                # Capability manifests
├── data/                        # Safety and action configuration
├── eval/                        # Evaluation cases and synthetic fixtures
├── manufacturer_knowledge/
│   └── _template/               # Generic manufacturer setup template
├── scripts/                     # Development utilities
├── src/carmind/
│   ├── app.py                   # Core application layer
│   ├── planner.py               # Bounded planning loop
│   ├── routing.py               # Capability routing
│   ├── tools.py                 # Tool execution
│   ├── safety.py                # Deterministic safety logic
│   ├── maintenance.py           # Maintenance intelligence
│   ├── ownership.py             # Vehicle ownership state
│   ├── storage.py               # Persistent storage
│   ├── manufacturer_manual.py   # Manufacturer-document RAG
│   ├── product.py               # Product interaction layer
│   ├── web.py                   # Local Web interface
│   └── whatsapp.py              # WhatsApp channel adapter
└── tests/
```

---

## Design Principles

CarMind follows several architectural rules:

**LLMs handle semantics — not safety-critical policy.**

**Manufacturer facts come from evidence — not model memory.**

**Persistent writes require structured confirmation.**

**Vehicle-specific knowledge stays outside the core.**

**The product remains useful even without manufacturer documents.**

**The user experience stays simple even when the backend is complex.**

---

## Current Status

CarMind is an active proof-of-concept and engineering project focused on building a reliable architecture for AI-assisted vehicle ownership.

The current version demonstrates the core architecture, stateful ownership model, manufacturer-grounded retrieval, deterministic safety boundaries, tool-based reasoning, and multi-channel interaction.

It is **not** a substitute for a qualified mechanic, manufacturer instructions, or professional vehicle inspection.

---

## Roadmap

Planned areas of development include:

- Real WhatsApp deployment
- Broader manufacturer-document ingestion
- Improved document indexing and retrieval
- Vehicle telemetry / OBD-II integration
- Proactive maintenance workflows
- Multi-vehicle ownership support improvements
- Workshop preparation and service summaries
- Production observability and evaluation
- Expanded multilingual interaction

---

## Author

**Omar Al-Zoubi**

AI Engineer focused on Generative AI, RAG, agentic systems, multimodal AI, and production-oriented AI architecture.

GitHub: [OmarIAlzoubi](https://github.com/OmarIAlzoubi)

---

## Disclaimer

CarMind is currently a proof-of-concept.

Vehicle information, diagnostics, maintenance guidance, and safety-related outputs must not be treated as a replacement for official manufacturer documentation, professional diagnosis, or qualified automotive service.