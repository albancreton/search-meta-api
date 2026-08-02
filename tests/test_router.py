import pytest

from search_meta_api.breaker import BreakerSettings, CircuitBreaker
from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.router import ProviderInstance, run_failover


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def instances(*names):
    return [ProviderInstance(name=n, adapter=object()) for n in names]


def make_breaker(clock):
    return CircuitBreaker(BreakerSettings(fault_threshold=1, cooldown_s=60.0,
                                          quota_cooldown_s=300.0), clock=clock)


def scripted(script, clock=None, cost_s=0.0):
    """script: name -> list of items, or a ProviderError to raise."""

    async def attempt(instance):
        if clock is not None:
            clock.advance(cost_s)
        outcome = script[instance.name]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, []

    return attempt


async def run(names, script, clock=None, deadline_ms=30_000, breaker=None, cost_s=0.0):
    clock = clock or FakeClock()
    return await run_failover(
        instances(*names),
        scripted(script, clock, cost_s),
        breaker or make_breaker(clock),
        deadline_ms=deadline_ms,
        clock=clock,
    )


async def test_first_provider_wins():
    out = await run(["a", "b"], {"a": ["r1"], "b": ["r2"]})
    assert out.winner == "a"
    assert out.items == ["r1"]
    assert [x.outcome for x in out.attempts] == ["success"]


async def test_error_falls_through_to_the_next_provider():
    out = await run(["a", "b"], {"a": ProviderError("a", ErrorClass.TRANSIENT, 500),
                                 "b": ["r2"]})
    assert out.winner == "b"
    assert out.items == ["r2"]
    assert [x.outcome for x in out.attempts] == ["error", "success"]
    assert out.attempts[0].status == 500
    assert out.attempts[0].error_class == "TRANSIENT"


async def test_empty_result_set_falls_through():
    out = await run(["a", "b"], {"a": [], "b": ["r2"]})
    assert out.winner == "b"
    assert [x.outcome for x in out.attempts] == ["empty", "success"]


async def test_empty_does_not_open_the_breaker():
    clock = FakeClock()
    breaker = make_breaker(clock)
    await run(["a", "b"], {"a": [], "b": ["r2"]}, clock=clock, breaker=breaker)
    assert breaker.is_open("a") is False


async def test_all_empty_is_a_successful_empty_answer_not_an_error():
    out = await run(["a", "b"], {"a": [], "b": []})
    assert out.winner is None
    assert out.items == []
    assert out.any_answer is True


async def test_all_errored_is_a_failure():
    out = await run(["a", "b"], {"a": ProviderError("a", ErrorClass.TRANSIENT, 500),
                                 "b": ProviderError("b", ErrorClass.AUTH, 401)})
    assert out.winner is None
    assert out.any_answer is False


async def test_open_breaker_instances_are_skipped_and_recorded():
    clock = FakeClock()
    breaker = make_breaker(clock)
    breaker.record_fault("a", ErrorClass.TRANSIENT)
    out = await run(["a", "b"], {"a": ["r1"], "b": ["r2"]}, clock=clock, breaker=breaker)
    assert out.winner == "b"
    assert out.attempts[0].outcome == "skipped"
    assert out.attempts[0].reason == "breaker-open"


async def test_quota_error_opens_the_breaker_for_the_long_cooldown():
    clock = FakeClock()
    breaker = make_breaker(clock)
    await run(["a", "b"], {"a": ProviderError("a", ErrorClass.QUOTA, 432), "b": ["r2"]},
              clock=clock, breaker=breaker)
    clock.advance(120)
    assert breaker.is_open("a") is True
    clock.advance(200)
    assert breaker.is_open("a") is False


async def test_success_after_a_fault_run_closes_the_breaker():
    clock = FakeClock()
    breaker = make_breaker(clock)
    breaker.record_fault("a", ErrorClass.TRANSIENT)
    clock.advance(61)
    out = await run(["a"], {"a": ["r1"]}, clock=clock, breaker=breaker)
    assert out.winner == "a"
    assert breaker.is_open("a") is False


async def test_deadline_stops_the_chain_mid_walk():
    clock = FakeClock()
    out = await run(["a", "b", "c"],
                    {"a": ProviderError("a", ErrorClass.TRANSIENT, 500),
                     "b": ProviderError("b", ErrorClass.TRANSIENT, 500),
                     "c": ["r3"]},
                    clock=clock, deadline_ms=1500, cost_s=1.0)
    assert out.winner is None
    assert out.attempts[-1].outcome == "skipped"
    assert out.attempts[-1].reason == "deadline-exceeded"


async def test_attempts_record_elapsed_milliseconds():
    clock = FakeClock()
    out = await run(["a"], {"a": ["r1"]}, clock=clock, cost_s=0.25)
    assert out.attempts[0].ms == 250
    assert out.elapsed_ms == 250


async def test_degraded_from_the_winning_attempt_is_hoisted():
    async def attempt(instance):
        return (["r1"], ["language"]) if instance.name == "b" else ([], [])

    clock = FakeClock()
    out = await run_failover(instances("a", "b"), attempt, make_breaker(clock),
                             deadline_ms=30_000, clock=clock)
    assert out.winner == "b"
    assert out.degraded == ["language"]
    assert out.attempts[1].degraded == ["language"]


async def test_unexpected_adapter_exception_is_contained_as_malformed():
    async def attempt(instance):
        if instance.name == "a":
            raise KeyError("adapter bug")
        return ["r2"], []

    clock = FakeClock()
    out = await run_failover(instances("a", "b"), attempt, make_breaker(clock),
                             deadline_ms=30_000, clock=clock)
    assert out.winner == "b"
    assert out.attempts[0].error_class == "MALFORMED"
