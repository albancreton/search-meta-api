from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from search_meta_api.errors import ErrorClass


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerSettings:
    fault_threshold: int = 3
    cooldown_s: float = 60.0
    quota_cooldown_s: float = 300.0


@dataclass
class _Entry:
    faults: int = 0
    opened_until: float = 0.0
    probe_in_flight: bool = False


class CircuitBreaker:
    """Per-instance circuit breaker.

    A QUOTA fault opens the circuit immediately — unlike a transient blip it is
    guaranteed to fail again, so there is nothing to learn from two more tries.
    """

    def __init__(
        self,
        settings: BreakerSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or BreakerSettings()
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def _entry(self, name: str) -> _Entry:
        return self._entries.setdefault(name, _Entry())

    def state(self, name: str) -> BreakerState:
        entry = self._entry(name)
        if entry.opened_until == 0.0:
            return BreakerState.CLOSED
        if self._clock() < entry.opened_until:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def is_open(self, name: str) -> bool:
        """True if the instance should be skipped.

        In HALF_OPEN this returns False exactly once, admitting a single probe;
        every subsequent call is blocked until the probe resolves.
        """
        entry = self._entry(name)
        state = self.state(name)
        if state is BreakerState.CLOSED:
            return False
        if state is BreakerState.OPEN:
            return True
        if entry.probe_in_flight:
            return True
        entry.probe_in_flight = True
        return False

    def record_success(self, name: str) -> None:
        self._entries[name] = _Entry()

    def record_fault(
        self,
        name: str,
        error_class: ErrorClass,
        retry_after_s: float | None = None,
    ) -> None:
        entry = self._entry(name)
        entry.probe_in_flight = False
        entry.faults += 1

        was_half_open = entry.opened_until > 0.0
        is_quota = error_class is ErrorClass.QUOTA

        if not (is_quota or was_half_open or entry.faults >= self._settings.fault_threshold):
            return

        cooldown = self._settings.quota_cooldown_s if is_quota else self._settings.cooldown_s
        if retry_after_s is not None:
            cooldown = max(cooldown, retry_after_s)
        entry.opened_until = self._clock() + cooldown
