from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from search_meta_api.breaker import CircuitBreaker
from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import Attempt

logger = logging.getLogger(__name__)

Attempted = Callable[["ProviderInstance"], Awaitable[tuple[list[Any], list[str]]]]


@dataclass
class ProviderInstance:
    name: str
    adapter: Any


@dataclass
class FailoverOutcome:
    winner: str | None
    items: list[Any]
    degraded: list[str]
    attempts: list[Attempt]
    elapsed_ms: int
    any_answer: bool = field(default=False)


async def run_failover(
    instances: Sequence[ProviderInstance],
    attempt: Attempted,
    breaker: CircuitBreaker,
    deadline_ms: int,
    clock: Callable[[], float] = time.monotonic,
) -> FailoverOutcome:
    """Walk instances in order and return the first non-empty result set.

    Capability-agnostic on purpose: it knows nothing about what `attempt` does,
    only that it yields items and a degraded-filter list, or raises.
    """
    started = clock()
    deadline_s = deadline_ms / 1000
    attempts: list[Attempt] = []
    any_answer = False

    def elapsed_ms() -> int:
        return int((clock() - started) * 1000)

    for instance in instances:
        # The deadline is checked FIRST, and this ordering is load-bearing.
        # breaker.is_open() is side-effecting: for a HALF_OPEN instance it
        # consumes the single probe token and returns False. Breaking on the
        # deadline after that would abandon the token with nothing left to
        # clear it, blackholing a recovering provider permanently.
        if clock() - started >= deadline_s:
            attempts.append(Attempt(provider=instance.name, outcome="skipped",
                                    reason="deadline-exceeded"))
            break

        if breaker.is_open(instance.name):
            attempts.append(Attempt(provider=instance.name, outcome="skipped",
                                    reason="breaker-open"))
            continue

        call_started = clock()
        try:
            items, degraded = await attempt(instance)
        except ProviderError as exc:
            breaker.record_fault(instance.name, exc.error_class, exc.retry_after_s)
            if exc.error_class is ErrorClass.AUTH:
                logger.error("provider %s auth failure (status %s) — check its key",
                             instance.name, exc.status)
            attempts.append(Attempt(
                provider=instance.name, outcome="error", status=exc.status,
                error_class=str(exc.error_class),
                ms=int((clock() - call_started) * 1000),
            ))
            continue
        except Exception:  # an adapter bug must not take down the whole chain
            logger.exception("adapter %s raised an unexpected error", instance.name)
            breaker.record_fault(instance.name, ErrorClass.MALFORMED)
            attempts.append(Attempt(
                provider=instance.name, outcome="error",
                error_class=str(ErrorClass.MALFORMED),
                ms=int((clock() - call_started) * 1000),
            ))
            continue
        except BaseException:
            # asyncio.CancelledError inherits BaseException, so it skips both
            # handlers above — and with them the record_fault/record_success
            # calls that are the only things that release a HALF_OPEN probe
            # token consumed by is_open() just now. Abandoning that token
            # removes the instance from the chain permanently. Hand it back,
            # then let the cancellation propagate untouched: the caller went
            # away, which is not a provider fault and must never become a
            # failover to the next provider.
            breaker.release_probe(instance.name)
            raise

        call_ms = int((clock() - call_started) * 1000)
        any_answer = True
        breaker.record_success(instance.name)

        if not items:
            attempts.append(Attempt(provider=instance.name, outcome="empty",
                                    status=200, ms=call_ms, degraded=degraded))
            continue

        attempts.append(Attempt(provider=instance.name, outcome="success", status=200,
                                ms=call_ms, degraded=degraded))
        return FailoverOutcome(instance.name, items, degraded, attempts,
                               elapsed_ms(), any_answer=True)

    return FailoverOutcome(None, [], [], attempts, elapsed_ms(), any_answer=any_answer)
