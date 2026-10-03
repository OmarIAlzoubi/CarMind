# Project
CarMind is a personal AI car ownership assistant for ordinary drivers.

The user experience must remain simple:
- The user talks to CarMind naturally.
- The long-term primary interface will be WhatsApp.
- V1 will use a CLI conversation interface for development and testing.
- The user should not need to understand OBD-II, diagnostic codes, Jev, planners, tools, or technical automotive terminology.

CarMind should help users:
- understand possible vehicle problems
- investigate symptoms
- understand warning lights and diagnostic codes
- manage maintenance
- remember service history
- track vehicle condition over time
- understand changes such as increased fuel consumption
- prepare for trips
- know what information may be useful for a workshop
- make better car ownership decisions

CarMind must remain useful without OBD-II data. OBD-II and live telemetry are optional data sources that improve the assistant when available, not requirements for using the product.

# Environment
Use only this Python interpreter for all Python commands:
`C:\Users\GOAT\anaconda3\envs\carmind\python.exe`

Do not use the system Python.
Do not use qwen3tts or any other Conda environment.

# Development rules
- Do not make large architectural decisions without explaining them first.
- Prefer simple, modular, readable Python.
- Do not over-engineer the MVP.
- Add dependencies only when needed.
- Before installing any new package, explain why it is needed.
- Never expose or commit secrets, API keys, or .env files.
- Do not push to GitHub unless explicitly requested.
- Do not create Git commits unless explicitly requested.
- Do not delete files unless explicitly requested or clearly necessary for an approved change.
- When modifying code, keep changes scoped to the requested task.
- Run relevant tests after implementation.
- Report what changed and any remaining issues.

# CarMind V1 direction
CarMind V1 will be a conversational ownership assistant, not purely a diagnostic system. V1 will use a CLI conversation interface, with simulator-first vehicle telemetry and fault scenarios where telemetry is needed. Real OBD-II hardware is not required.

CarMind Core must remain independent from the user interface. CLI, WhatsApp, and future interfaces must connect to the same core through adapters:

```text
CLI --------\
             -> CarMind Core
WhatsApp ---/
Future UI --/
```

WhatsApp integration will come later without requiring a redesign of CarMind Core. Keep conversation handling, vehicle context, capabilities, and safety logic independent from interface-specific input and output.

The architecture should support three levels of information:

1. User-provided information
   - natural language messages
   - symptoms
   - mileage
   - maintenance updates
   - receipts or records later
2. Stored vehicle context
   - vehicle profile
   - maintenance history
   - previous problems
   - previous diagnostic codes
   - relevant conversation context
3. Optional vehicle telemetry
   - simulated in V1
   - real OBD-II may be added later

The technical architecture may use:
- Jev capability routing
- dynamic capability loading
- an LLM planner
- read-only tools
- vehicle memory
- deterministic safety rules
- evaluation and benchmarking

These are implementation details that support the ownership assistant; they must not become the product itself or complicate the user experience.

Future capability families may include:

Diagnostics:
- engine
- cooling
- electrical
- battery
- transmission
- emissions

Maintenance:
- oil
- tires
- brakes
- fluids
- filters
- scheduled service

Ownership:
- service history
- warranty
- recalls
- expenses

Driving and usage:
- fuel economy
- trip readiness
- vehicle health

These families describe future directions, not a requirement to build every capability in V1. Keep the MVP small and modular.

Implement only the currently approved milestone; do not proceed to later layers.

# Maintenance intelligence
CarMind is both a conversational car assistant and a proactive personal maintenance secretary.
Manufacturer documentation is authoritative for scheduled maintenance. LLMs may explain
manufacturer-derived facts, but must not invent intervals, due points, provenance or status.
Maintenance calculations and reminder events are deterministic and independent of delivery.
Support multiple manufacturers, models, model years and markets with exact applicability.
The initial Hyundai ELANTRA N PoC must not introduce Hyundai-specific logic into Core.
Unknown model years must remain unknown; never infer a model year from a download filename.
Thailand guidance is not Saudi/GCC guidance. Keep complete manuals in ignored local research.
CarMind must remain useful without OBD-II. WhatsApp remains a future interface adapter.
WhatsApp delivery, real OBD-II and background scheduling remain outside the approved milestone.

# Capability routing and evaluation
- Jev is a capability-routing layer, not the planner.
- Jev must never control safety, manufacturer applicability, maintenance rules or due calculations.
- Full Planner mode must remain available as a baseline and must not call the router.
- Routed mode uses the same planner, tools, validator, safety and maintenance engines after selection.
- Router failures require explicit deterministic fallback behavior and recorded reasons.
- Record raw routing selections separately from effective loaded capabilities, including expansion.
- Benchmark conclusions must report quality tradeoffs honestly; reduced exposure alone is not success.

# Safety
Vehicle-related outputs may have real-world safety implications.
The system must distinguish between:
- informational reasoning
- low-risk recommendations
- safety-critical advice

Safety-critical decisions must not rely solely on probabilistic LLM output.

# Ownership application
- Transport adapters call CarMindApp; they contain no automotive reasoning or SQL.
- The LLM proposes allowlisted commands and never mutates persistent facts directly.
- A model's claim of certainty is not authorization. Apply changes only after explicit confirmation of the exact stored proposal.
- Keep durable owner-reported facts separate from bounded chat summaries. Hypotheses never become service records or ownership facts.
- Use explicit UTC-aware time and canonical km. Correct records by superseding them, and reject contradictory odometer timelines.
- Ownership mutations and deterministic reminder refresh share one transaction; confirmation replay must not duplicate facts.
- Reminders require applicable, vetted source rules. Test-only fictional schedules require explicit opt-in and never become production guidance.
- An unresolved deterministic stop warning cannot be cleared by an LLM, a follow-up message, or merely recording a service.
- Real Jev and xAI providers are selected explicitly at composition time; offline tests and demos never construct them by default.
- Live providers may propose ownership changes but never mutate storage. Exact confirmation remains a zero-provider application operation.
- Live evaluations use isolated, marked databases with explicit provider-call budgets. Test-only manufacturer knowledge is never enabled by production composition.
