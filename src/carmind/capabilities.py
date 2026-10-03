"""Local versioned capability metadata and deterministic context selection."""

from dataclasses import dataclass
import json
from pathlib import Path

from carmind.tools import TOOL_CATALOG, ToolDefinition


DEFAULT_CAPABILITIES_DIRECTORY = Path(__file__).resolve().parents[2] / "capabilities"


@dataclass(frozen=True)
class CapabilityPack:
    id: str
    version: int
    routing_description: str
    planner_instructions: str
    tool_ids: tuple[str, ...]
    required_inputs: tuple[str, ...]


@dataclass(frozen=True)
class LoadedCapabilities:
    packs: tuple[CapabilityPack, ...]
    tools: tuple[ToolDefinition, ...]

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(tool.tool_id for tool in self.tools)

    @property
    def planner_instructions(self) -> str:
        return "\n\n".join(f"[{pack.id}]\n{pack.planner_instructions}" for pack in self.packs)

    @property
    def capability_count(self) -> int:
        return len(self.packs)

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    @property
    def instruction_character_count(self) -> int:
        return len(self.planner_instructions)


class CapabilityRegistry:
    def __init__(self, directory: Path = DEFAULT_CAPABILITIES_DIRECTORY, *, include_manual=False) -> None:
        paths = sorted(Path(directory).glob("*/manifest.json"))
        if not paths:
            raise ValueError("No capability manifests found")
        packs = {}
        for path in paths:
            # The manual pack is available only when a prepared, registered index
            # is injected. Legacy/no-telemetry paths retain their original surface.
            if (Path(directory).resolve() == DEFAULT_CAPABILITIES_DIRECTORY.resolve()
                    and path.parent.name == "manufacturer_manual" and not include_manual):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Manifest must be an object")
                required = {"id", "version", "routing_description", "planner_instructions", "tool_ids", "required_inputs"}
                if not required <= data.keys():
                    raise ValueError("Missing required manifest fields")
                for name in ("id", "routing_description", "planner_instructions"):
                    if not isinstance(data[name], str) or not data[name].strip():
                        raise ValueError(f"Blank or invalid {name}")
                if type(data["version"]) is not int or data["version"] != 1:
                    raise ValueError("Unsupported manifest version")
                for name in ("tool_ids", "required_inputs"):
                    if not isinstance(data[name], list) or any(not isinstance(item, str) or not item.strip() for item in data[name]):
                        raise ValueError(f"Invalid {name}")
                    if len(data[name]) != len(set(data[name])):
                        raise ValueError(f"Duplicate entries in {name}")
                if any(tool_id not in TOOL_CATALOG for tool_id in data["tool_ids"]):
                    raise ValueError("Unknown referenced tool")
                if data["id"] in packs:
                    raise ValueError("Duplicate capability ID")
                packs[data["id"]] = CapabilityPack(
                    data["id"], data["version"], data["routing_description"], data["planner_instructions"],
                    tuple(sorted(data["tool_ids"])), tuple(data["required_inputs"]),
                )
            except (ValueError, OSError) as error:
                raise ValueError(f"Invalid manifest {path}: {error}") from error
        self._packs = dict(sorted(packs.items()))

    def list_capabilities(self) -> tuple[CapabilityPack, ...]:
        return tuple(self._packs.values())

    def routing_descriptions(self) -> dict[str, str]:
        """Router-facing view excludes planner instructions and tool schemas."""
        return {pack.id: pack.routing_description for pack in self._packs.values()}

    def load_capabilities(self, capability_ids: list[str] | tuple[str, ...]) -> LoadedCapabilities:
        if not isinstance(capability_ids, (list, tuple)) or any(not isinstance(item, str) for item in capability_ids):
            raise ValueError("Capability IDs must be a list or tuple of strings")
        unknown = set(capability_ids) - self._packs.keys()
        if unknown:
            raise ValueError(f"Unknown capabilities: {sorted(unknown)}")
        packs = tuple(self._packs[key] for key in sorted(set(capability_ids)))
        tool_ids = sorted({tool_id for pack in packs for tool_id in pack.tool_ids})
        return LoadedCapabilities(packs, tuple(TOOL_CATALOG[key] for key in tool_ids))

    def load_all_capabilities(self) -> LoadedCapabilities:
        return self.load_capabilities(list(self._packs))

    def load_additional(self, loaded: LoadedCapabilities, capability_ids: list[str]) -> LoadedCapabilities:
        return self.load_capabilities([pack.id for pack in loaded.packs] + capability_ids)


def list_capabilities() -> tuple[CapabilityPack, ...]:
    return CapabilityRegistry().list_capabilities()


def load_capabilities(capability_ids: list[str]) -> LoadedCapabilities:
    return CapabilityRegistry().load_capabilities(capability_ids)


def load_all_capabilities() -> LoadedCapabilities:
    return CapabilityRegistry().load_all_capabilities()
