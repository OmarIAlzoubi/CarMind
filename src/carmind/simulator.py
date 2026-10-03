"""Deterministic development telemetry, not a diagnostic or safety system.

All numbers describe a fictional gasoline vehicle and are simulation parameters,
not manufacturer specifications or universal automotive guidance. Only pass the
result of freeze_episode to CarMind Core, never the episode or evaluator truth.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from random import Random
from uuid import NAMESPACE_URL, uuid5

from carmind.contracts import Observation, ObservationSource, UserMessage, VehicleProfile
from carmind.evidence import FrozenEvidenceSnapshot


@dataclass(frozen=True)
class ScenarioTruth:
    """Evaluator-only injection metadata; never part of Core-facing evidence."""

    scenario_id: str
    relevant_domains: tuple[str, ...]


@dataclass(frozen=True)
class SimulationEpisode:
    """Development-only complete timeline, including samples after assessment."""

    episode_id: str
    profile: VehicleProfile
    owner_message: UserMessage
    timeline: tuple[Observation, ...]
    assessment_at: datetime


_SCENARIOS = {
    "healthy_vehicle": (
        "I'm planning a long drive tomorrow. Is there anything I should check?",
        ("trip_readiness",),
    ),
    "sustained_temperature_rise": (
        "The temperature warning came on while I was driving.",
        ("cooling",),
    ),
    "weak_battery_start": (
        "My car is struggling to start this morning.",
        ("electrical", "battery"),
    ),
    "gradual_tire_pressure_loss": (
        "One tire keeps losing pressure. Can you check what might be happening?",
        ("tires",),
    ),
    "increased_fuel_consumption": (
        "My car has been using more fuel than usual lately.",
        ("fuel_economy",),
    ),
}


def list_scenarios() -> tuple[str, ...]:
    """List development scenario choices, not user-facing product actions."""
    return tuple(_SCENARIOS)


def generate_episode(scenario: str, seed: int) -> tuple[SimulationEpisode, ScenarioTruth]:
    """Return a reproducible episode and a separate evaluator-only truth record.

    Seven sample times are generated; the sixth is the assessment cutoff.
    Battery samples are seconds apart, cooling samples minutes apart, and tire,
    fuel, and normal ownership histories days apart. Fuel metrics summarize
    recent trips at each sample time; they do not predict future consumption.
    """
    if scenario not in _SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario!r}")
    rng = Random(seed)
    # Opaque IDs distinguish episodes without embedding scenario names.
    episode_id = str(uuid5(NAMESPACE_URL, f"carmind-demo:{scenario}:{seed}"))
    profile = VehicleProfile(
        vehicle_id="demo-vehicle-1",
        make="Fictional",
        model="Everyday",
        year=2024,
        mileage_km=24000.0,
        engine="Fictional gasoline engine",
    )
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    step = timedelta(days=1)
    if scenario == "weak_battery_start":
        step = timedelta(seconds=2)
    elif scenario == "sustained_temperature_rise":
        step = timedelta(minutes=5)
    assessment_at = start + 5 * step
    message, domains = _SCENARIOS[scenario]
    owner_message = UserMessage(f"{episode_id}:message", message, assessment_at)
    observations: list[Observation] = []

    def add(name: str, value: str | float, unit: str | None, timestamp: datetime) -> None:
        observations.append(Observation(
            observation_id=f"{episode_id}:observation:{len(observations):03d}",
            name=name,
            value=value,
            unit=unit,
            timestamp=timestamp,
            source=ObservationSource.SIMULATOR,
        ))

    def noisy(value: float, amplitude: float) -> float:
        return round(value + rng.uniform(-amplitude, amplitude), 3)

    for index in range(7):
        timestamp = start + index * step
        if scenario in ("healthy_vehicle", "sustained_temperature_rise"):
            temperature = 90.0
            if scenario == "sustained_temperature_rise":
                temperature = (89, 95, 102, 110, 115, 116, 117)[index]
            add("coolant_temperature", noisy(temperature, 0.3), "degC", timestamp)
            add("operating_state", "running", None, timestamp)
        if scenario in ("healthy_vehicle", "weak_battery_start"):
            voltage = 14.2
            if scenario == "weak_battery_start":
                voltage = (12.2, 11.9, 9.0, 8.6, 9.1, 11.8, 12.0)[index]
                state = "starting" if 2 <= index <= 4 else "off"
                add("operating_state", state, None, timestamp)
            add("battery_voltage", noisy(voltage, 0.04), "V", timestamp)
        if scenario in ("healthy_vehicle", "gradual_tire_pressure_loss"):
            for wheel in ("front_left", "front_right", "rear_left", "rear_right"):
                pressure = 35.0
                if scenario == "gradual_tire_pressure_loss" and wheel == "rear_left":
                    pressure -= 1.2 * index
                add(f"{wheel}_tire_pressure", noisy(pressure, 0.08), "psi", timestamp)
        if scenario in ("healthy_vehicle", "increased_fuel_consumption"):
            consumption, duration, idle_ratio = 7.0, 25.0, 0.08
            if scenario == "increased_fuel_consumption":
                consumption = (7.0, 7.1, 7.0, 8.3, 9.0, 9.5, 9.6)[index]
                duration = (25, 24, 25, 18, 15, 14, 14)[index]
                idle_ratio = (0.08, 0.08, 0.09, 0.15, 0.20, 0.23, 0.24)[index]
            add("fuel_consumption", noisy(consumption, 0.08), "L/100km", timestamp)
            add("average_trip_duration", noisy(duration, 0.3), "min", timestamp)
            add("idle_time_ratio", noisy(idle_ratio, 0.005), "ratio", timestamp)

    episode = SimulationEpisode(
        episode_id, profile, owner_message, tuple(observations), assessment_at
    )
    return episode, ScenarioTruth(scenario, domains)


def freeze_episode(episode: SimulationEpisode) -> FrozenEvidenceSnapshot:
    """Copy only cutoff-visible evidence into the source-independent boundary."""
    return FrozenEvidenceSnapshot(
        episode_id=episode.episode_id,
        profile=episode.profile,
        owner_message=episode.owner_message,
        assessment_at=episode.assessment_at,
        observations=tuple(
            item for item in episode.timeline if item.timestamp <= episode.assessment_at
        ),
    )
