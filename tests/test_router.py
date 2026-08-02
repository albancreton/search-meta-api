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
    # A 4th provider ("d") is included specifically so that stopping the loop
    # (break) versus merely skipping this instance (continue) produces a
    # different attempts list: with "c" as the last provider, break vs.
    # continue would be observationally identical and the test would pass
    # either way.
    clock = FakeClock()
    out = await run(["a", "b", "c", "d"],
                    {"a": ProviderError("a", ErrorClass.TRANSIENT, 500),
                     "b": ProviderError("b", ErrorClass.TRANSIENT, 500),
                     "c": ProviderError("c", ErrorClass.TRANSIENT, 500),
                     "d": ["r4"]},
                    clock=clock, deadline_ms=1500, cost_s=1.0)
    assert out.winner is None
    assert out.attempts[-1].outcome == "skipped"
    assert out.attempts[-1].reason == "deadline-exceeded"
    # Exactly 3 attempts: "a" errors, "b" errors, "c" is skipped for the
    # deadline, and the loop stops there — "d" is never attempted.
    assert len(out.attempts) == 3


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


async def test_deadline_check_precedes_breaker_check_to_preserve_half_open_probe():
    """A deadline-truncated request must not permanently consume a HALF_OPEN
    provider's single probe token.

    breaker.is_open() is side-effecting: for a HALF_OPEN instance it sets
    probe_in_flight and returns False, admitting exactly one probe. Only
    record_success/record_fault clear that flag. If the deadline fires before
    the probe's attempt() ever runs, and the breaker check already consumed
    the token, nothing will ever release it — the instance blackholes forever.
    The router must check the deadline before the breaker so the token is
    only taken when an attempt will actually follow.
    """
    clock = FakeClock()
    breaker = make_breaker(clock)
    breaker.record_fault("b", ErrorClass.TRANSIENT)  # opens "b" for cooldown_s=60
    clock.advance(61)  # cooldown elapsed -> "b" is now HALF_OPEN

    out = await run(["a", "b"], {"a": [], "b": ["r2"]}, clock=clock, breaker=breaker,
                    deadline_ms=1500, cost_s=2.0)

    assert out.winner is None
    assert out.attempts[-1].outcome == "skipped"
    assert out.attempts[-1].reason == "deadline-exceeded"
    # "b" was never reached (the deadline expired first), so its probe token
    # must still be available.
    assert breaker.is_open("b") is False


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


async def test_empty_result_resets_the_breakers_fault_count():
    # record_success resets the entry wholesale. An empty result set is a
    # successful (if unhelpful) response, so it must call record_success just
    # like a non-empty one — clearing any fault count accumulated so far.
    clock = FakeClock()
    breaker = CircuitBreaker(
        BreakerSettings(fault_threshold=2, cooldown_s=60.0, quota_cooldown_s=300.0),
        clock=clock,
    )
    breaker.record_fault("a", ErrorClass.TRANSIENT)  # 1 fault, below threshold of 2
    assert breaker.is_open("a") is False

    await run(["a", "b"], {"a": [], "b": ["r2"]}, clock=clock, breaker=breaker)

    # If the empty path had not reset the count, this second fault would bring
    # "a" to 2/2 and open the breaker. Because record_success ran on the
    # empty branch, "a" starts fresh: this is only fault 1 of 2.
    breaker.record_fault("a", ErrorClass.TRANSIENT)
    assert breaker.is_open("a") is False


async def test_error_class_is_recorded_for_auth_and_quota_failures():
    out = await run(["a", "b"], {"a": ProviderError("a", ErrorClass.AUTH, 401),
                                 "b": ProviderError("b", ErrorClass.QUOTA, 432)})
    assert out.attempts[0].error_class == "AUTH"
    assert out.attempts[1].error_class == "QUOTA"


async def test_retry_after_s_extends_the_cooldown_beyond_the_configured_value():
    clock = FakeClock()
    breaker = make_breaker(clock)  # configured cooldown_s=60.0
    await run(
        ["a", "b"],
        {"a": ProviderError("a", ErrorClass.TRANSIENT, 500, retry_after_s=120.0),
         "b": ["r2"]},
        clock=clock, breaker=breaker,
    )

    clock.advance(90)  # past the configured 60s cooldown, before retry_after_s's 120s
    assert breaker.is_open("a") is True

    clock.advance(40)  # now past the full 120s retry_after_s
    assert breaker.is_open("a") is False
