"""The in-memory representation of a single-channel chromatogram."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class Chromatogram:
    """One detector channel of one injection.

    ``time`` is always in minutes and strictly increasing; ``signal`` is in
    ``signal_unit`` (e.g. ``"mAU"``).  Both arrays are read-only so a
    chromatogram can be shared without being altered, mirroring the rule that
    raw data is never modified.
    """

    time: FloatArray
    signal: FloatArray
    signal_unit: str = ""
    detector: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        t = np.array(self.time, dtype=np.float64)
        y = np.array(self.signal, dtype=np.float64)
        if t.ndim != 1 or y.ndim != 1:
            raise ValueError("time and signal must be 1-D")
        if t.size != y.size:
            raise ValueError(f"time ({t.size}) and signal ({y.size}) lengths differ")
        if t.size < 2:
            raise ValueError("a chromatogram needs at least 2 points")
        if not np.all(np.diff(t) > 0):
            raise ValueError("time must be strictly increasing")
        t.flags.writeable = False
        y.flags.writeable = False
        object.__setattr__(self, "time", t)
        object.__setattr__(self, "signal", y)

    @classmethod
    def from_arrays(cls, time: ArrayLike, signal: ArrayLike, **kwargs: Any) -> Chromatogram:
        return cls(
            np.asarray(time, dtype=np.float64), np.asarray(signal, dtype=np.float64), **kwargs
        )

    @property
    def sampling_interval(self) -> float:
        """Median spacing between points, in minutes."""
        return float(np.median(np.diff(self.time)))

    def __len__(self) -> int:
        return int(self.time.size)
