# CarMind

A personal car ownership assistant with an interface-independent application
core, deterministic maintenance and safety, and optional vehicle evidence.

Run the **offline scripted ownership demo** from this repository in PowerShell:

```powershell
$env:PYTHONPATH='src'; & 'C:\Users\GOAT\anaconda3\envs\carmind\python.exe' -B -m carmind --demo
```

No keys, packages or network calls are needed. The demo uses a fake planner and
explicitly fictional test maintenance data. It records mileage and service,
refreshes reminders, investigates tire evidence, handles a safety follow-up, and
reopens its SQLite store. Its temporary database is removed after the demo.

Real adapters construct `OwnershipStore` with a durable local path and inject a
provider into `CarMindApp`. `handle_message` returns response text, validated
assessment/safety, proposed or applied commands, reminders and an internal trace.
To change a fact, show the exact proposal and pass the owner's explicit
confirmation ID on a separate call. A chat message saying “yes” alone cannot write.
Keep local databases in `.local/`; never commit personal records or credentials.

No production manufacturer schedule is active. Missing applicability stays
unknown. Existing safety thresholds cover fictional simulation data only; this
MVP is not real-vehicle driving clearance.

See [MILESTONE6_REPORT.md](MILESTONE6_REPORT.md) for architecture, validation,
retention, known limits, and the actual offline demonstration transcript.
