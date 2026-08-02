from search_meta_api.breaker import BreakerSettings, BreakerState, CircuitBreaker
from search_meta_api.errors import ErrorClass


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_breaker(**kwargs):
    clock = FakeClock()
    settings = BreakerSettings(fault_threshold=3, cooldown_s=60.0, quota_cooldown_s=300.0)
    for k, v in kwargs.items():
        setattr(settings, k, v)
    return CircuitBreaker(settings, clock=clock), clock


def test_starts_closed_and_unknown_names_are_closed():
    breaker, _ = make_breaker()
    assert breaker.state("tavily-a") is BreakerState.CLOSED
    assert breaker.is_open("tavily-a") is False


def test_opens_only_after_threshold_consecutive_faults():
    breaker, _ = make_breaker()
    for _ in range(2):
        breaker.record_fault("tavily-a", ErrorClass.TRANSIENT)
    assert breaker.state("tavily-a") is BreakerState.CLOSED
    breaker.record_fault("tavily-a", ErrorClass.TRANSIENT)
    assert breaker.state("tavily-a") is BreakerState.OPEN
    assert breaker.is_open("tavily-a") is True


def test_success_resets_the_fault_run():
    breaker, _ = make_breaker()
    breaker.record_fault("tavily-a", ErrorClass.TRANSIENT)
    breaker.record_fault("tavily-a", ErrorClass.TRANSIENT)
    breaker.record_success("tavily-a")
    breaker.record_fault("tavily-a", ErrorClass.TRANSIENT)
    assert breaker.state("tavily-a") is BreakerState.CLOSED


def test_instances_are_independent():
    breaker, _ = make_breaker()
    for _ in range(3):
        breaker.record_fault("tavily-a", ErrorClass.TRANSIENT)
    assert breaker.is_open("tavily-a") is True
    assert breaker.is_open("tavily-b") is False


def test_open_becomes_half_open_after_cooldown():
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(59)
    assert breaker.is_open("brave") is True
    clock.advance(2)
    assert breaker.is_open("brave") is False
    assert breaker.state("brave") is BreakerState.HALF_OPEN


def test_half_open_admits_exactly_one_probe():
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(61)
    assert breaker.is_open("brave") is False   # the probe
    assert breaker.is_open("brave") is True    # everyone else waits
    assert breaker.is_open("brave") is True


def test_half_open_success_closes_the_circuit():
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(61)
    breaker.is_open("brave")
    breaker.record_success("brave")
    assert breaker.state("brave") is BreakerState.CLOSED
    assert breaker.is_open("brave") is False


def test_half_open_fault_reopens_immediately():
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(61)
    breaker.is_open("brave")
    breaker.record_fault("brave", ErrorClass.TRANSIENT)
    assert breaker.state("brave") is BreakerState.OPEN
    assert breaker.is_open("brave") is True


def test_half_open_fault_hands_the_probe_token_back():
    """`test_half_open_fault_reopens_immediately` passes from the OPEN branch of
    is_open() whether or not record_fault clears probe_in_flight, so it cannot
    pin the release. Advance past the *new* cooldown: only if the token was
    handed back does the next window admit a fresh probe. Without the release
    the instance is HALF_OPEN with an unresolvable token — gone for good.
    """
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(61)
    assert breaker.is_open("brave") is False   # takes the probe token
    breaker.record_fault("brave", ErrorClass.TRANSIENT)   # the probe failed
    clock.advance(61)                          # past the NEW cooldown
    assert breaker.state("brave") is BreakerState.HALF_OPEN
    assert breaker.is_open("brave") is False   # a fresh probe is admitted


def test_release_probe_returns_an_unresolved_token():
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(61)
    assert breaker.is_open("brave") is False   # takes the probe token
    assert breaker.is_open("brave") is True    # nobody else gets in

    breaker.release_probe("brave")

    assert breaker.state("brave") is BreakerState.HALF_OPEN
    assert breaker.is_open("brave") is False   # the token is available again


def test_release_probe_does_not_close_or_reopen_the_circuit():
    """Releasing an unresolved token says nothing about the provider's health —
    it must neither count as a success (closing the circuit) nor as a fault
    (extending the cooldown).
    """
    breaker, clock = make_breaker()
    for _ in range(3):
        breaker.record_fault("brave", ErrorClass.TRANSIENT)
    clock.advance(61)
    breaker.is_open("brave")
    breaker.release_probe("brave")
    assert breaker.state("brave") is BreakerState.HALF_OPEN


def test_release_probe_on_a_closed_instance_is_a_no_op():
    breaker, _ = make_breaker()
    breaker.release_probe("never-seen")
    assert breaker.state("never-seen") is BreakerState.CLOSED
    assert breaker.is_open("never-seen") is False


def test_quota_fault_opens_on_first_occurrence_with_long_cooldown():
    """A quota error is guaranteed to fail again — no point waiting for a run of three."""
    breaker, clock = make_breaker()
    breaker.record_fault("tavily-a", ErrorClass.QUOTA)
    assert breaker.state("tavily-a") is BreakerState.OPEN
    clock.advance(61)
    assert breaker.is_open("tavily-a") is True
    clock.advance(240)
    assert breaker.is_open("tavily-a") is False


def test_retry_after_extends_but_never_shortens_the_cooldown():
    breaker, clock = make_breaker()
    breaker.record_fault("linkup", ErrorClass.QUOTA, retry_after_s=600.0)
    clock.advance(301)
    assert breaker.is_open("linkup") is True
    clock.advance(300)
    assert breaker.is_open("linkup") is False


def test_short_retry_after_does_not_shorten_cooldown():
    breaker, clock = make_breaker()
    breaker.record_fault("linkup", ErrorClass.QUOTA, retry_after_s=5.0)
    clock.advance(10)
    assert breaker.is_open("linkup") is True
