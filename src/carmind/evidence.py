"""Source-independent, immutable evidence available at an assessment cutoff."""

from dataclasses import dataclass
from datetime import datetime

from carmind.contracts import Observation, UserMessage, VehicleProfile


@dataclass(frozen=True)
class FrozenEvidenceSnapshot:
    """Core-facing evidence only; no generator, timeline, or evaluator references.

    Empty observations are valid: telemetry is optional. Included observations
    must already have been available at assessment_at, including the cutoff.
    """

    episode_id: str
    profile: VehicleProfile
    owner_message: UserMessage
    assessment_at: datetime
    observations: tuple[Observation, ...] = ()

    def __post_init__(self) -> None:
        if not self.episode_id.strip():
            raise ValueError("episode_id must not be blank")
        # Copy collection inputs into an immutable container, including lists.
        object.__setattr__(self, "observations", tuple(self.observations))
        timestamps = [self.assessment_at, self.owner_message.timestamp]
        timestamps.extend(observation.timestamp for observation in self.observations)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("evidence timestamps must be timezone-aware")
        if any(timestamp > self.assessment_at for timestamp in timestamps):
            raise ValueError("evidence cannot be later than assessment_at")
        observation_times = [item.timestamp for item in self.observations]
        if observation_times != sorted(observation_times):
            raise ValueError("observations must be ordered by timestamp")
        identifiers = [item.observation_id for item in self.observations]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("observation IDs must be unique")
