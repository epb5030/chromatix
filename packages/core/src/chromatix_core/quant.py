"""Components, peak identification and calibration: from peaks to amounts.

A processing method is the integration parameters plus a component table, as
in Empower: each component has an expected retention time and window, and its
own calibration settings.  Standards (with known amounts) calibrate each
component; any injection is then quantitated from its identified peak areas.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from chromatix_core.calibration import CalibrationCurve, Model, Weighting, fit_calibration
from chromatix_core.peaks import IntegrationParams

METHOD_SCHEMA = 2


class HasRetentionTime(Protocol):
    @property
    def retention_time(self) -> float: ...


@dataclass(frozen=True)
class Component:
    name: str
    retention_time: float
    """Expected retention time in minutes."""
    window: float = 0.1
    """A peak is identified as this component if within ± window minutes."""
    model: Model = "linear"
    weighting: Weighting = "none"
    through_origin: bool = False
    unit: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a component needs a name")
        if self.window <= 0:
            raise ValueError(f"{self.name}: window must be positive")


@dataclass(frozen=True)
class ProcessingMethod:
    integration: IntegrationParams = field(default_factory=IntegrationParams)
    components: tuple[Component, ...] = ()

    def __post_init__(self) -> None:
        names = [c.name for c in self.components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": METHOD_SCHEMA,
            "integration": dataclasses.asdict(self.integration),
            "components": [dataclasses.asdict(c) for c in self.components],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProcessingMethod:
        """Read a stored method.  Version-1 records are flat integration parameters."""
        if "integration" not in data:
            return cls(integration=IntegrationParams(**data))
        return cls(
            integration=IntegrationParams(**data["integration"]),
            components=tuple(Component(**c) for c in data.get("components", [])),
        )

    def component(self, name: str) -> Component:
        for c in self.components:
            if c.name == name:
                return c
        raise KeyError(name)


def identify(peaks: Sequence[HasRetentionTime], components: Sequence[Component]) -> dict[int, str]:
    """Map peak index -> component name.

    Each component takes the peak closest to its expected retention time within
    its window; each peak is used at most once.  Conflicts are resolved by
    assigning the closest (component, peak) pairs first.
    """
    candidates = sorted(
        (abs(pk.retention_time - comp.retention_time), i, comp.name)
        for comp in components
        for i, pk in enumerate(peaks)
        if abs(pk.retention_time - comp.retention_time) <= comp.window
    )
    assigned: dict[int, str] = {}
    used: set[str] = set()
    for _, index, name in candidates:
        if index not in assigned and name not in used:
            assigned[index] = name
            used.add(name)
    return assigned


@dataclass(frozen=True)
class CalibrationPoint:
    amount: float
    response: float
    injection_id: int | None = None


def calibrate(component: Component, points: Sequence[CalibrationPoint]) -> CalibrationCurve:
    """Fit ``component``'s calibration curve (response = area) from standard points."""
    if not points:
        raise ValueError(f"{component.name}: no calibration points")
    return fit_calibration(
        [p.amount for p in points],
        [p.response for p in points],
        model=component.model,
        weighting=component.weighting,
        through_origin=component.through_origin,
    )
