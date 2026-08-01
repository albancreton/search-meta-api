# search-meta-api Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single web-search API that walks an ordered list of third-party search providers and returns the first positive response.

**Architecture:** A capability-agnostic failover router over a registry of provider adapters. Each adapter translates one unified request into a vendor's dialect, calls it, and translates results back; it never knows other providers exist. The router walks instances in configured order, applies circuit-breaking per instance, and returns the first non-empty result set. This seam lets a future `/extract` capability reuse the router unchanged.

**Tech Stack:** Python 3.12, FastAPI, httpx (async), pydantic v2, PyYAML, pycountry, pytest + pytest-asyncio + respx, Docker.

**Spec:** [`docs/superpowers/specs/2026-08-01-search-meta-api-design.md`](../specs/2026-08-01-search-meta-api-design.md)

## Global Constraints

- Python 3.12. All provider I/O is async via a single shared `httpx.AsyncClient`.
- Every task is test-first: write the failing test, watch it fail, implement, watch it pass, commit.
- No test may touch the network. Provider tests use `respx` fixtures; router and breaker tests use fakes and an injected clock. No `time.sleep` anywhere in tests.
- Only `title`, `url` and `snippet` are non-nullable on a result. `published_date`, `score` and `favicon` are nullable by construction.
- Success = a well-formed response with **at least one result**. Zero results is not a fault and must not penalise the circuit breaker.
- API keys live inline in `config.yaml`, which is gitignored. Only `config.example.yaml` is tracked. Never commit a real key.
- The config loader refuses to boot on an unknown provider type, a duplicate instance name, or an instance name colliding with a type name.
- Bing is excluded — retired 2025-08-11.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | deps, pytest config, ruff config |
| `config.example.yaml` | tracked config template with placeholder keys |
| `src/search_meta_api/models.py` | `UnifiedFilters`, `SearchRequest`, `EffectiveRequest`, `SearchResult`, `Attempt`, `SearchResponse` |
| `src/search_meta_api/errors.py` | `ErrorClass`, `ProviderError`, `classify_status` |
| `src/search_meta_api/breaker.py` | `BreakerState`, `BreakerSettings`, `CircuitBreaker` |
| `src/search_meta_api/providers/base.py` | `Capabilities`, `Adapter` ABC, `resolve_request` |
| `src/search_meta_api/router.py` | `ProviderInstance`, `AttemptRecord`, `FailoverOutcome`, `run_failover` |
| `src/search_meta_api/config.py` | YAML → validated `Settings` |
| `src/search_meta_api/providers/registry.py` | type name → adapter class, `build_adapter` |
| `src/search_meta_api/providers/*.py` | one adapter per vendor |
| `src/search_meta_api/validation.py` | `validate_params` — edge validation of the namespaced map |
| `src/search_meta_api/app.py` | FastAPI wiring, auth, endpoints |
| `Dockerfile`, `docker-compose.yml` | deployment |

## Locked Interfaces

Every task depends on these exact names. Do not rename.

```python
UNIFIED_FIELDS: frozenset[str]   # every settable unified field, incl. max_results
FILTER_FIELDS:  frozenset[str]   # degraded-eligible subset, excl. query/max_results

class ErrorClass(StrEnum):     TRANSIENT | AUTH | QUOTA | MALFORMED
class ProviderError(Exception): .provider .error_class .status .retry_after_s
def classify_status(status: int) -> ErrorClass

class BreakerState(StrEnum):   CLOSED | OPEN | HALF_OPEN
class CircuitBreaker:
    def is_open(name) -> bool
    def state(name) -> BreakerState
    def record_success(name) -> None
    def record_fault(name, error_class, retry_after_s=None) -> None

class Adapter(ABC):
    type_name: ClassVar[str]
    capabilities: ClassVar[Capabilities]
    async def search(req: EffectiveRequest, passthrough: dict) -> list[SearchResult]

def resolve_request(req, instance_name, type_name, capabilities)
    -> tuple[EffectiveRequest, dict[str, Any], list[str]]

async def run_failover(instances, attempt, breaker, deadline_ms, clock) -> FailoverOutcome
```

---

### Task 1: Project scaffold and unified models

**Files:**
- Create: `pyproject.toml`, `src/search_meta_api/__init__.py`, `src/search_meta_api/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `UnifiedFilters`, `SearchRequest`, `EffectiveRequest`, `SearchResult`, `Attempt`, `SearchResponse`, `UNIFIED_FIELDS`, `FILTER_FIELDS`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "search-meta-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "httpx>=0.28",
    "pydantic>=2.9",
    "pyyaml>=6.0",
    "pycountry>=24.6",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "respx>=0.22", "ruff>=0.8"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
src = ["src"]
```

- [ ] **Step 2: Create the package and install**

```bash
mkdir -p src/search_meta_api/providers tests/providers tests/fixtures
touch src/search_meta_api/__init__.py src/search_meta_api/providers/__init__.py
python -m venv .venv && .venv/bin/pip install -q -e '.[dev]'
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_models.py`:

```python
import pytest
from datetime import date
from pydantic import ValidationError

from search_meta_api.models import (
    FILTER_FIELDS, UNIFIED_FIELDS, EffectiveRequest, SearchRequest, SearchResult,
)


def test_query_is_required_and_non_empty():
    with pytest.raises(ValidationError):
        SearchRequest(query="")


def test_defaults():
    req = SearchRequest(query="hello")
    assert req.max_results == 10
    assert req.topic == "general"
    assert req.include_domains == []
    assert req.freshness is None
    assert req.params == {}


def test_accepts_full_filter_set():
    req = SearchRequest(
        query="hello", max_results=5, include_domains=["a.com"], exclude_domains=["b.com"],
        freshness="week", start_date=date(2026, 1, 1), country="fr", language="fr",
        safe_search="strict", topic="news",
    )
    assert req.freshness == "week"
    assert req.start_date == date(2026, 1, 1)


def test_rejects_unknown_enum_values():
    with pytest.raises(ValidationError):
        SearchRequest(query="hello", freshness="fortnight")
    with pytest.raises(ValidationError):
        SearchRequest(query="hello", safe_search="paranoid")


def test_country_and_language_are_normalised():
    req = SearchRequest(query="hello", country="FR", language="FR")
    assert req.country == "fr"
    assert req.language == "fr"


def test_field_sets():
    assert "max_results" in UNIFIED_FIELDS
    assert "max_results" not in FILTER_FIELDS
    assert "query" not in FILTER_FIELDS
    assert FILTER_FIELDS < UNIFIED_FIELDS


def test_effective_request_has_no_params_field():
    eff = EffectiveRequest(query="hello")
    assert not hasattr(eff, "params")


def test_search_result_nullable_fields_default_to_none():
    r = SearchResult(title="t", url="https://x.com", snippet="s", rank=1)
    assert r.published_date is None and r.score is None and r.favicon is None
```

- [ ] **Step 4: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.models'`

- [ ] **Step 5: Implement `src/search_meta_api/models.py`**

```python
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Freshness = Literal["day", "week", "month", "year"]
SafeSearch = Literal["off", "moderate", "strict"]
Topic = Literal["general", "news"]


class UnifiedFilters(BaseModel):
    """The vendor-neutral request surface. Adapters translate this."""

    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=10, ge=1, le=100)
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    freshness: Freshness | None = None
    start_date: date | None = None
    end_date: date | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    language: str | None = Field(default=None, min_length=2, max_length=2)
    safe_search: SafeSearch | None = None
    topic: Topic = "general"

    @field_validator("country", "language")
    @classmethod
    def _lowercase(cls, v: str | None) -> str | None:
        return v.lower() if v else v


class EffectiveRequest(UnifiedFilters):
    """A UnifiedFilters after namespaced overrides are applied, bound to one provider."""


class SearchRequest(UnifiedFilters):
    params: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    rank: int
    published_date: str | None = None
    score: float | None = None
    favicon: str | None = None


class Attempt(BaseModel):
    provider: str
    outcome: Literal["success", "empty", "error", "skipped"]
    status: int | None = None
    error_class: str | None = None
    reason: str | None = None
    ms: int | None = None
    degraded: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    provider: str
    results: list[SearchResult]
    degraded: list[str] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    elapsed_ms: int


UNIFIED_FIELDS: frozenset[str] = frozenset(UnifiedFilters.model_fields) - {"query"}
FILTER_FIELDS: frozenset[str] = UNIFIED_FIELDS - {"max_results"}
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: PASS — 8 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/search_meta_api tests/test_models.py
git commit -m "feat: project scaffold and unified request/response models"
```

---

### Task 2: Error classification

**Files:**
- Create: `src/search_meta_api/errors.py`
- Test: `tests/test_errors.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ErrorClass`, `ProviderError(provider, error_class, status=None, message="", retry_after_s=None)`, `classify_status(status) -> ErrorClass`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_errors.py`:

```python
import pytest

from search_meta_api.errors import ErrorClass, ProviderError, classify_status


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses(status):
    assert classify_status(status) is ErrorClass.AUTH


@pytest.mark.parametrize("status", [402, 429, 432, 433])
def test_quota_statuses(status):
    """432 (plan limit) and 433 (pay-as-you-go cap) are Tavily-specific."""
    assert classify_status(status) is ErrorClass.QUOTA


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_transient_statuses(status):
    assert classify_status(status) is ErrorClass.TRANSIENT


@pytest.mark.parametrize("status", [400, 404, 422])
def test_everything_else_is_malformed(status):
    assert classify_status(status) is ErrorClass.MALFORMED


def test_provider_error_carries_context():
    err = ProviderError("tavily-a", ErrorClass.QUOTA, status=432, message="plan limit",
                        retry_after_s=90.0)
    assert err.provider == "tavily-a"
    assert err.error_class is ErrorClass.QUOTA
    assert err.status == 432
    assert err.retry_after_s == 90.0
    assert "tavily-a" in str(err) and "432" in str(err)


def test_provider_error_defaults():
    err = ProviderError("brave", ErrorClass.MALFORMED)
    assert err.status is None and err.retry_after_s is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.errors'`

- [ ] **Step 3: Implement `src/search_meta_api/errors.py`**

```python
from __future__ import annotations

from enum import StrEnum

AUTH_STATUSES = frozenset({401, 403})
QUOTA_STATUSES = frozenset({402, 429, 432, 433})
TRANSIENT_STATUSES = frozenset({408})


class ErrorClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    AUTH = "AUTH"
    QUOTA = "QUOTA"
    MALFORMED = "MALFORMED"


def classify_status(status: int) -> ErrorClass:
    """Map an HTTP status to a failover class.

    Request validation happens at the edge, so every failure reaching this
    point is the provider's problem, not the caller's. There is deliberately
    no fail-fast class.
    """
    if status in AUTH_STATUSES:
        return ErrorClass.AUTH
    if status in QUOTA_STATUSES:
        return ErrorClass.QUOTA
    if status in TRANSIENT_STATUSES or status >= 500:
        return ErrorClass.TRANSIENT
    return ErrorClass.MALFORMED


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        error_class: ErrorClass,
        status: int | None = None,
        message: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        self.provider = provider
        self.error_class = error_class
        self.status = status
        self.retry_after_s = retry_after_s
        super().__init__(f"{provider}: {error_class} status={status} {message}".strip())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_errors.py -v`
Expected: PASS — 16 passed

- [ ] **Step 5: Commit**

```bash
git add src/search_meta_api/errors.py tests/test_errors.py
git commit -m "feat: provider error hierarchy and failover classification"
```

---

### Task 3: Circuit breaker

**Files:**
- Create: `src/search_meta_api/breaker.py`
- Test: `tests/test_breaker.py`

**Interfaces:**
- Consumes: `ErrorClass` from Task 2.
- Produces: `BreakerState`, `BreakerSettings(fault_threshold, cooldown_s, quota_cooldown_s)`, `CircuitBreaker(settings, clock)`.

State is per provider **instance** — `tavily-a` and `tavily-b` are independent. It lives in process memory and is deliberately not persisted: after a restart, re-probing every provider once is cheap and correct.

- [ ] **Step 1: Write the failing test**

Create `tests/test_breaker.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_breaker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.breaker'`

- [ ] **Step 3: Implement `src/search_meta_api/breaker.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_breaker.py -v`
Expected: PASS — 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/search_meta_api/breaker.py tests/test_breaker.py
git commit -m "feat: per-instance circuit breaker with quota-aware cooldowns"
```

---

### Task 4: Adapter contract and namespaced parameter resolution

**Files:**
- Create: `src/search_meta_api/providers/base.py`
- Test: `tests/providers/test_base.py`, `tests/providers/__init__.py`

**Interfaces:**
- Consumes: `EffectiveRequest`, `SearchRequest`, `SearchResult`, `UNIFIED_FIELDS`, `FILTER_FIELDS` (Task 1); `ProviderError`, `ErrorClass`, `classify_status` (Task 2).
- Produces: `Capabilities`, `Adapter`, `resolve_request`, `domain_operators`.

This is the load-bearing task. `resolve_request` implements the scoping rules: precedence is `*` → `<type>` → `<instance name>`, a flat top-level field and its `*/` twin are the same slot, and a scoped key may name either a unified field (override it for that provider) or a vendor passthrough parameter (sent raw).

- [ ] **Step 1: Write the failing test**

Create `tests/providers/__init__.py` (empty) and `tests/providers/test_base.py`:

```python
import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import SearchRequest, SearchResult
from search_meta_api.providers.base import (
    Adapter, Capabilities, domain_operators, resolve_request,
)

CAPS = Capabilities(
    supported_filters=frozenset({"include_domains", "freshness", "country", "topic"}),
    max_results_cap=20,
    passthrough_params={"search_depth": str, "chunks_per_source": int},
)


def resolve(req):
    return resolve_request(req, instance_name="tavily-b", type_name="tavily", capabilities=CAPS)


def test_flat_fields_pass_through_untouched():
    eff, passthrough, degraded = resolve(SearchRequest(query="hi", country="fr"))
    assert eff.query == "hi" and eff.country == "fr"
    assert passthrough == {}


def test_universal_scope_overrides_flat_default():
    eff, _, _ = resolve(SearchRequest(query="hi", params={"*/freshness": "week"}))
    assert eff.freshness == "week"


def test_type_scope_beats_universal():
    eff, _, _ = resolve(SearchRequest(
        query="hi", params={"*/max_results": 5, "tavily/max_results": 8}))
    assert eff.max_results == 8


def test_instance_scope_beats_type_scope():
    eff, _, _ = resolve(SearchRequest(
        query="hi", params={"tavily/max_results": 8, "tavily-b/max_results": 15}))
    assert eff.max_results == 15


def test_other_instances_scope_is_ignored():
    eff, _, _ = resolve(SearchRequest(query="hi", params={"tavily-a/max_results": 3}))
    assert eff.max_results == 10


def test_max_results_is_clamped_to_the_provider_cap():
    eff, _, _ = resolve(SearchRequest(query="hi", max_results=100))
    assert eff.max_results == 20


def test_scoped_vendor_params_become_passthrough():
    _, passthrough, _ = resolve(SearchRequest(
        query="hi", params={"tavily/search_depth": "advanced", "tavily-b/chunks_per_source": 2}))
    assert passthrough == {"search_depth": "advanced", "chunks_per_source": 2}


def test_instance_passthrough_beats_type_passthrough():
    _, passthrough, _ = resolve(SearchRequest(
        query="hi",
        params={"tavily/search_depth": "basic", "tavily-b/search_depth": "advanced"}))
    assert passthrough == {"search_depth": "advanced"}


def test_unsupported_filters_are_reported_as_degraded():
    eff, _, degraded = resolve(SearchRequest(query="hi", language="fr", safe_search="strict"))
    assert degraded == ["language", "safe_search"]
    assert eff.language == "fr"   # left on the request; the adapter simply ignores it


def test_supported_filters_are_not_degraded():
    _, _, degraded = resolve(SearchRequest(query="hi", country="fr", freshness="day"))
    assert degraded == []


def test_unset_filters_are_never_degraded():
    _, _, degraded = resolve(SearchRequest(query="hi"))
    assert degraded == []


def test_default_topic_is_not_treated_as_requested():
    caps = Capabilities(supported_filters=frozenset(), max_results_cap=None)
    _, _, degraded = resolve_request(
        SearchRequest(query="hi", topic="general"), "x", "x", caps)
    assert degraded == []
    _, _, degraded = resolve_request(
        SearchRequest(query="hi", topic="news"), "x", "x", caps)
    assert degraded == ["topic"]


def test_domain_operators_builds_a_google_style_query():
    assert domain_operators("cats", ["a.com", "b.com"], ["c.com"]) == (
        "cats (site:a.com OR site:b.com) -site:c.com")
    assert domain_operators("cats", [], []) == "cats"
    assert domain_operators("cats", ["a.com"], []) == "cats (site:a.com)"


def test_extra_degraded_defaults_to_empty():
    probe = _Probe("p", httpx.AsyncClient())
    assert probe.extra_degraded(SearchRequest(query="hi")) == []


def test_extra_degraded_can_be_overridden_for_value_dependent_gaps():
    class Picky(_Probe):
        def extra_degraded(self, req):
            return ["freshness"] if req.freshness == "week" else []

    picky = Picky("p", httpx.AsyncClient())
    assert picky.extra_degraded(SearchRequest(query="hi", freshness="week")) == ["freshness"]
    assert picky.extra_degraded(SearchRequest(query="hi", freshness="day")) == []


class _Probe(Adapter):
    type_name = "probe"
    capabilities = CAPS

    async def search(self, req, passthrough):
        return []


@pytest.fixture
def probe():
    return _Probe("probe-1", httpx.AsyncClient(), timeout_ms=5000)


@respx.mock
async def test_request_json_returns_parsed_body(probe):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await probe.request_json("GET", "https://x.test/s") == {"ok": True}


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, ErrorClass.AUTH), (432, ErrorClass.QUOTA), (503, ErrorClass.TRANSIENT),
     (400, ErrorClass.MALFORMED)],
)
async def test_request_json_classifies_error_statuses(probe, status, expected):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is expected
    assert exc.value.status == status
    assert exc.value.provider == "probe-1"


@respx.mock
async def test_request_json_reads_retry_after(probe):
    respx.get("https://x.test/s").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"}, json={}))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.retry_after_s == 120.0


@respx.mock
async def test_timeout_becomes_a_transient_error(probe):
    respx.get("https://x.test/s").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is ErrorClass.TRANSIENT


@respx.mock
async def test_unparseable_body_becomes_malformed(probe):
    respx.get("https://x.test/s").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as exc:
        await probe.request_json("GET", "https://x.test/s")
    assert exc.value.error_class is ErrorClass.MALFORMED


def test_build_results_assigns_rank_and_truncates():
    rows = [("t1", "u1", "s1"), ("t2", "u2", "s2"), ("t3", "u3", "s3")]
    results = [SearchResult(title=t, url=u, snippet=s, rank=i + 1)
               for i, (t, u, s) in enumerate(rows)][:2]
    assert [r.rank for r in results] == [1, 2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.base'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/base.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

import httpx

from search_meta_api.errors import ErrorClass, ProviderError, classify_status
from search_meta_api.models import (
    FILTER_FIELDS, UNIFIED_FIELDS, EffectiveRequest, SearchRequest, SearchResult, UnifiedFilters,
)

UNIVERSAL_SCOPE = "*"


@dataclass(frozen=True)
class Capabilities:
    """What a vendor can actually honour.

    `supported_filters` names unified fields this vendor has an equivalent for.
    Anything requested but absent here is dropped and reported as degraded.
    """

    supported_filters: frozenset[str]
    max_results_cap: int | None = None
    passthrough_params: Mapping[str, type] = field(default_factory=dict)


def _field_default(name: str) -> Any:
    return UnifiedFilters.model_fields[name].get_default(call_default_factory=True)


def _split(key: str) -> tuple[str, str]:
    scope, _, name = key.partition("/")
    return scope, name


def resolve_request(
    req: SearchRequest,
    instance_name: str,
    type_name: str,
    capabilities: Capabilities,
) -> tuple[EffectiveRequest, dict[str, Any], list[str]]:
    """Bind a request to one provider instance.

    Returns the effective unified request, the raw vendor passthrough params,
    and the sorted list of requested filters this provider cannot honour.
    """
    values = req.model_dump(exclude={"params"})
    passthrough: dict[str, Any] = {}

    for scope in (UNIVERSAL_SCOPE, type_name, instance_name):
        for key, value in req.params.items():
            key_scope, name = _split(key)
            if key_scope != scope:
                continue
            if name in UNIFIED_FIELDS:
                values[name] = value
            elif name in capabilities.passthrough_params and scope != UNIVERSAL_SCOPE:
                passthrough[name] = value

    if capabilities.max_results_cap is not None:
        values["max_results"] = min(values["max_results"], capabilities.max_results_cap)

    effective = EffectiveRequest(**values)

    degraded = sorted(
        name
        for name in FILTER_FIELDS
        if getattr(effective, name) != _field_default(name)
        and name not in capabilities.supported_filters
    )
    return effective, passthrough, degraded


def domain_operators(query: str, include: list[str], exclude: list[str]) -> str:
    """Emulate domain filters with search operators, for vendors that lack them."""
    parts = [query]
    if include:
        parts.append("(" + " OR ".join(f"site:{d}" for d in include) + ")")
    parts.extend(f"-site:{d}" for d in exclude)
    return " ".join(parts)


class Adapter(ABC):
    type_name: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    def __init__(
        self,
        name: str,
        client: httpx.AsyncClient,
        timeout_ms: int = 10_000,
        **options: Any,
    ) -> None:
        self.name = name
        self.client = client
        self.timeout_s = timeout_ms / 1000
        self.options = options

    @abstractmethod
    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        """Translate, call, translate back. Raise ProviderError on any failure."""

    def extra_degraded(self, req: EffectiveRequest) -> list[str]:
        """Filters this adapter cannot honour for *this particular* request.

        `capabilities.supported_filters` covers static gaps. This covers
        value-dependent ones — e.g. SearXNG has day/month/year windows but no
        week, so `freshness` is supported in general yet degraded for one value.
        """
        return []

    async def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method, url, timeout=self.timeout_s, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name, ErrorClass.TRANSIENT, message=f"timeout: {exc}"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                self.name, ErrorClass.TRANSIENT, message=f"connection: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self.name,
                self.classify(response),
                status=response.status_code,
                message=response.text[:200],
                retry_after_s=_retry_after(response),
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                self.name, ErrorClass.MALFORMED, status=response.status_code,
                message="response body is not JSON",
            ) from exc

        if not isinstance(body, dict):
            raise ProviderError(
                self.name, ErrorClass.MALFORMED, status=response.status_code,
                message="response body is not a JSON object",
            )
        return body

    def classify(self, response: httpx.Response) -> ErrorClass:
        """Override where a vendor needs body inspection to classify (see google_cse)."""
        return classify_status(response.status_code)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/providers/test_base.py -v`
Expected: PASS — 23 passed

- [ ] **Step 5: Commit**

```bash
git add src/search_meta_api/providers tests/providers
git commit -m "feat: adapter contract and namespaced parameter resolution"
```

---

### Task 5: Failover router

**Files:**
- Create: `src/search_meta_api/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `Attempt` (Task 1); `ProviderError`, `ErrorClass` (Task 2); `CircuitBreaker` (Task 3).
- Produces: `ProviderInstance`, `FailoverOutcome`, `run_failover`.

The router must not import anything search-specific. It takes instances, an `attempt` coroutine, and returns whichever items that coroutine produced. That is what lets `/extract` reuse it later.

- [ ] **Step 1: Write the failing test**

Create `tests/test_router.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_router.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.router'`

- [ ] **Step 3: Implement `src/search_meta_api/router.py`**

```python
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

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
        if breaker.is_open(instance.name):
            attempts.append(Attempt(provider=instance.name, outcome="skipped",
                                    reason="breaker-open"))
            continue

        if clock() - started >= deadline_s:
            attempts.append(Attempt(provider=instance.name, outcome="skipped",
                                    reason="deadline-exceeded"))
            break

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_router.py -v`
Expected: PASS — 13 passed

- [ ] **Step 5: Commit**

```bash
git add src/search_meta_api/router.py tests/test_router.py
git commit -m "feat: capability-agnostic failover router"
```

---

### Task 6: Tavily adapter (reference implementation)

**Files:**
- Create: `src/search_meta_api/providers/tavily.py`, `tests/conftest.py`
- Test: `tests/providers/test_tavily.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities` (Task 4); `EffectiveRequest`, `SearchResult` (Task 1).
- Produces: `TavilyAdapter` with `type_name = "tavily"`. Every later adapter follows this exact shape: a `capabilities` class attribute, a `search` that builds the vendor payload, and a private `_parse` that returns `list[SearchResult]`.

**Two deliberate capability decisions:**

- `country` is supported, but Tavily takes country *names* (`"france"`), not ISO codes. Converted with `pycountry`.
- `safe_search` is declared **unsupported** even though the parameter exists, because Tavily gates it to enterprise plans and sending it on a normal key risks a 400 on every request. Enterprise users can still reach it via `tavily/safe_search`.

- [ ] **Step 1: Create the shared test fixture file**

Create `tests/conftest.py`:

```python
import httpx
import pytest


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c
```

- [ ] **Step 2: Write the failing test**

Create `tests/providers/test_tavily.py`:

```python
import json
from datetime import date

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.tavily import TavilyAdapter

URL = "https://api.tavily.com/search"

BODY = {
    "query": "cats",
    "results": [
        {"title": "Cats", "url": "https://a.com", "content": "about cats",
         "score": 0.91, "favicon": "https://a.com/f.ico"},
        {"title": "More", "url": "https://b.com", "content": "more cats", "score": 0.7},
    ],
    "response_time": 0.64,
}


@pytest.fixture
def adapter(client):
    return TavilyAdapter("tavily-a", client, timeout_ms=5000, api_key="tvly-AAAAA")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_bearer_auth_and_query(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["authorization"] == "Bearer tvly-AAAAA"
    assert sent(route)["query"] == "cats"


@respx.mock
async def test_parses_results_into_unified_shape(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].url == "https://a.com"
    assert results[0].snippet == "about cats"
    assert results[0].score == 0.91
    assert results[0].favicon == "https://a.com/f.ico"
    assert results[1].favicon is None


@respx.mock
async def test_truncates_to_max_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=1), {})
    assert len(results) == 1


@respx.mock
async def test_translates_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, include_domains=["a.com"], exclude_domains=["b.com"],
        freshness="week", start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
        country="fr", topic="news",
    ), {})
    payload = sent(route)
    assert payload["max_results"] == 7
    assert payload["include_domains"] == ["a.com"]
    assert payload["exclude_domains"] == ["b.com"]
    assert payload["time_range"] == "week"
    assert payload["start_date"] == "2026-01-01"
    assert payload["end_date"] == "2026-02-01"
    assert payload["country"] == "france"       # Tavily wants names, not ISO codes
    assert payload["topic"] == "news"


@respx.mock
async def test_omits_unset_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    payload = sent(route)
    for absent in ("time_range", "start_date", "end_date", "country",
                   "include_domains", "exclude_domains"):
        assert absent not in payload


@respx.mock
async def test_passthrough_params_are_merged_into_the_payload(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"),
                         {"search_depth": "advanced", "chunks_per_source": 2})
    payload = sent(route)
    assert payload["search_depth"] == "advanced"
    assert payload["chunks_per_source"] == 2


@respx.mock
async def test_empty_results_is_an_empty_list_not_an_error(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"query": "x", "results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, ErrorClass.AUTH), (429, ErrorClass.QUOTA), (432, ErrorClass.QUOTA),
     (433, ErrorClass.QUOTA), (500, ErrorClass.TRANSIENT)],
)
async def test_error_statuses_classify_correctly(adapter, status, expected):
    respx.post(URL).mock(return_value=httpx.Response(
        status, json={"detail": {"error": "nope"}}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is expected


@respx.mock
async def test_missing_results_key_is_malformed(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"query": "x"}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.MALFORMED


def test_capabilities():
    caps = TavilyAdapter.capabilities
    assert caps.max_results_cap == 20
    assert "country" in caps.supported_filters
    assert "language" not in caps.supported_filters
    assert "safe_search" not in caps.supported_filters   # enterprise-gated
    assert "search_depth" in caps.passthrough_params
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_tavily.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.tavily'`

- [ ] **Step 4: Implement `src/search_meta_api/providers/tavily.py`**

```python
from __future__ import annotations

from typing import Any

import pycountry

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities

ENDPOINT = "https://api.tavily.com/search"


def country_name(alpha_2: str) -> str | None:
    """Tavily takes country names, not ISO codes."""
    entry = pycountry.countries.get(alpha_2=alpha_2.upper())
    return entry.name.lower() if entry else None


class TavilyAdapter(Adapter):
    type_name = "tavily"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "start_date", "end_date", "country", "topic",
        }),
        max_results_cap=20,
        passthrough_params={
            "search_depth": str,
            "chunks_per_source": int,
            "include_answer": object,
            "include_raw_content": object,
            "include_images": bool,
            "include_image_descriptions": bool,
            "include_favicon": bool,
            "auto_parameters": bool,
            "exact_match": bool,
            "safe_search": bool,
            "include_usage": bool,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": req.query,
            "max_results": req.max_results,
            "topic": req.topic,
        }
        if req.include_domains:
            payload["include_domains"] = req.include_domains
        if req.exclude_domains:
            payload["exclude_domains"] = req.exclude_domains
        if req.freshness:
            payload["time_range"] = req.freshness
        if req.start_date:
            payload["start_date"] = req.start_date.isoformat()
        if req.end_date:
            payload["end_date"] = req.end_date.isoformat()
        if req.country and (name := country_name(req.country)):
            payload["country"] = name
        payload.update(passthrough)

        body = await self.request_json(
            "POST",
            ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {self.options['api_key']}"},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        return [
            SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=row.get("content") or "",
                rank=index + 1,
                score=row.get("score"),
                favicon=row.get("favicon"),
            )
            for index, row in enumerate(rows[:limit])
            if row.get("url")
        ]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/providers/test_tavily.py -v`
Expected: PASS — 14 passed

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/tavily.py tests/conftest.py tests/providers/test_tavily.py
git commit -m "feat: tavily adapter"
```

---

### Task 7: Config loading and adapter registry

**Files:**
- Create: `src/search_meta_api/config.py`, `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `TavilyAdapter` (Task 6); `BreakerSettings` (Task 3).
- Produces: `Settings`, `ServerConfig`, `DefaultsConfig`, `ProviderConfig`, `load_settings(path) -> Settings`; `ADAPTERS: dict[str, type[Adapter]]`, `build_adapter(cfg, client, defaults) -> Adapter`.

Only `tavily` is registered here. **Each remaining adapter task appends one line to `ADAPTERS` and one line to `REQUIRED_OPTIONS`.**

The loader refuses to boot on a bad config rather than degrading silently — a misconfigured provider that quietly vanishes from the chain is a failure you find out about months later.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
import pytest
import yaml

from search_meta_api.config import ConfigError, load_settings

MINIMAL = {
    "server": {"auth_token": "secret"},
    "providers": [
        {"name": "tavily-a", "type": "tavily", "api_key": "tvly-AAAAA"},
        {"name": "tavily-b", "type": "tavily", "api_key": "tvly-BBBBB", "timeout_ms": 15000},
    ],
}


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_loads_providers_in_declaration_order(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    assert [p.name for p in settings.providers] == ["tavily-a", "tavily-b"]


def test_applies_defaults(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.defaults.timeout_ms == 10000
    assert settings.defaults.max_results == 10
    assert settings.server.request_deadline_ms == 30000
    assert settings.breaker.fault_threshold == 3
    assert settings.breaker.cooldown_ms == 60000
    assert settings.breaker.quota_cooldown_ms == 300000


def test_per_instance_timeout_overrides_the_default(tmp_path):
    settings = load_settings(write(tmp_path, MINIMAL))
    assert settings.providers[0].timeout_ms is None
    assert settings.providers[1].timeout_ms == 15000


def test_auth_token_may_be_absent(tmp_path):
    data = {**MINIMAL, "server": {}}
    assert load_settings(write(tmp_path, data)).server.auth_token is None


def test_rejects_unknown_provider_type(tmp_path):
    data = {**MINIMAL, "providers": [{"name": "x", "type": "altavista", "api_key": "k"}]}
    with pytest.raises(ConfigError, match="unknown provider type"):
        load_settings(write(tmp_path, data))


def test_rejects_duplicate_instance_names(tmp_path):
    data = {**MINIMAL, "providers": [
        {"name": "dup", "type": "tavily", "api_key": "a"},
        {"name": "dup", "type": "tavily", "api_key": "b"},
    ]}
    with pytest.raises(ConfigError, match="duplicate provider name"):
        load_settings(write(tmp_path, data))


def test_rejects_instance_name_colliding_with_a_type_name(tmp_path):
    """Otherwise a `tavily/...` scope prefix would be ambiguous."""
    data = {**MINIMAL, "providers": [{"name": "tavily", "type": "tavily", "api_key": "a"}]}
    with pytest.raises(ConfigError, match="collides with a provider type"):
        load_settings(write(tmp_path, data))


def test_rejects_missing_required_option(tmp_path):
    data = {**MINIMAL, "providers": [{"name": "t", "type": "tavily"}]}
    with pytest.raises(ConfigError, match="requires 'api_key'"):
        load_settings(write(tmp_path, data))


def test_rejects_empty_provider_list(tmp_path):
    with pytest.raises(ConfigError, match="at least one provider"):
        load_settings(write(tmp_path, {**MINIMAL, "providers": []}))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


def test_example_config_is_valid():
    """The tracked template must always load — it is the onboarding path."""
    settings = load_settings("config.example.yaml")
    assert len(settings.providers) == 9
    assert [p.name for p in settings.providers][:2] == ["tavily-a", "tavily-b"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.config'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/registry.py`**

```python
from __future__ import annotations

from typing import Any

import httpx

from search_meta_api.providers.base import Adapter
from search_meta_api.providers.tavily import TavilyAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    TavilyAdapter.type_name: TavilyAdapter,
}

# Options each type must be given in config, beyond name/type.
REQUIRED_OPTIONS: dict[str, tuple[str, ...]] = {
    "tavily": ("api_key",),
}

OPTION_FIELDS = ("api_key", "base_url", "cx")


def build_adapter(cfg: Any, client: httpx.AsyncClient, default_timeout_ms: int) -> Adapter:
    options = {f: getattr(cfg, f) for f in OPTION_FIELDS if getattr(cfg, f, None) is not None}
    return ADAPTERS[cfg.type](
        cfg.name,
        client,
        timeout_ms=cfg.timeout_ms or default_timeout_ms,
        **options,
    )
```

- [ ] **Step 4: Implement `src/search_meta_api/config.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from search_meta_api.breaker import BreakerSettings
from search_meta_api.providers.registry import ADAPTERS, REQUIRED_OPTIONS


class ConfigError(Exception):
    pass


class ServerConfig(BaseModel):
    auth_token: str | None = None
    request_deadline_ms: int = 30_000


class DefaultsConfig(BaseModel):
    timeout_ms: int = 10_000
    max_results: int = 10


class BreakerConfig(BaseModel):
    fault_threshold: int = 3
    cooldown_ms: int = 60_000
    quota_cooldown_ms: int = 300_000

    def to_settings(self) -> BreakerSettings:
        return BreakerSettings(
            fault_threshold=self.fault_threshold,
            cooldown_s=self.cooldown_ms / 1000,
            quota_cooldown_s=self.quota_cooldown_ms / 1000,
        )


class ProviderConfig(BaseModel):
    name: str
    type: str
    api_key: str | None = None
    base_url: str | None = None
    cx: str | None = None
    timeout_ms: int | None = None


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    providers: list[ProviderConfig]


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {exc}") from exc

    try:
        settings = Settings(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config: {exc}") from exc

    _validate_providers(settings)
    return settings


def _validate_providers(settings: Settings) -> None:
    if not settings.providers:
        raise ConfigError("config must declare at least one provider")

    seen: set[str] = set()
    for provider in settings.providers:
        if provider.type not in ADAPTERS:
            raise ConfigError(
                f"unknown provider type {provider.type!r} for instance {provider.name!r}; "
                f"known types: {', '.join(sorted(ADAPTERS))}"
            )
        if provider.name in seen:
            raise ConfigError(f"duplicate provider name {provider.name!r}")
        if provider.name in ADAPTERS:
            raise ConfigError(
                f"provider name {provider.name!r} collides with a provider type name, "
                "which would make a params scope prefix ambiguous"
            )
        for option in REQUIRED_OPTIONS[provider.type]:
            if getattr(provider, option, None) is None:
                raise ConfigError(
                    f"provider {provider.name!r} of type {provider.type!r} "
                    f"requires {option!r}"
                )
        seen.add(provider.name)
```

- [ ] **Step 5: Create `config.example.yaml`**

All nine instances are listed now so the template is complete; the loader will
reject the seven types that are not yet registered, which is exactly why
`test_example_config_is_valid` stays red until Task 14. Comment out everything
below `tinyfish` for now and uncomment each line as its adapter lands.

```yaml
# Copy to config.yaml and fill in real keys. config.yaml is gitignored.
server:
  auth_token: "changeme"        # remove or set null to disable auth entirely
  request_deadline_ms: 30000

defaults:
  timeout_ms: 10000
  max_results: 10

breaker:
  fault_threshold: 3
  cooldown_ms: 60000
  quota_cooldown_ms: 300000

# Order is failover order.
providers:
  - {name: tavily-a,  type: tavily,     api_key: "tvly-REPLACE-A"}
  - {name: tavily-b,  type: tavily,     api_key: "tvly-REPLACE-B", timeout_ms: 15000}
  - {name: tinyfish,  type: tinyfish,   api_key: "REPLACE"}
  - {name: brave,     type: brave,      api_key: "REPLACE"}
  - {name: exa,       type: exa,        api_key: "REPLACE"}
  - {name: linkup,    type: linkup,     api_key: "REPLACE"}
  - {name: firecrawl, type: firecrawl,  api_key: "fc-REPLACE"}
  - {name: google,    type: google_cse, api_key: "REPLACE", cx: "REPLACE"}
  - {name: searxng,   type: searxng,    base_url: "https://xng.xmpl.me"}
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_config.py -v --deselect tests/test_config.py::test_example_config_is_valid`
Expected: PASS — 10 passed

Run: `.venv/bin/pytest tests/test_config.py::test_example_config_is_valid -v`
Expected: FAIL — `unknown provider type 'tinyfish'`. This test is the tracker for the remaining adapters and goes green in Task 14.

- [ ] **Step 7: Commit**

```bash
git add src/search_meta_api/config.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/test_config.py
git commit -m "feat: config loading with strict validation and adapter registry"
```

---

### Registering an adapter (applies to Tasks 8–14)

Every remaining adapter task ends with the same two-line registry edit. Do it as part of that task's implementation step, not as a separate commit:

```python
# src/search_meta_api/providers/registry.py
from search_meta_api.providers.<module> import <Class>          # add import
ADAPTERS = {..., <Class>.type_name: <Class>}                     # add entry
REQUIRED_OPTIONS = {..., "<type_name>": ("api_key",)}            # add entry
```

Then uncomment that provider's line in `config.example.yaml`.

---

### Task 8: TinyFish adapter

**Files:**
- Create: `src/search_meta_api/providers/tinyfish.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_tinyfish.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities` (Task 4).
- Produces: `TinyFishAdapter`, `type_name = "tinyfish"`.

The endpoint is the **bare base URL with no path** — `GET https://api.search.tinyfish.ai?query=…`. There is no result-count parameter, so `max_results` is honoured by truncating client-side. `location` takes region identifiers rather than ISO codes, so `country` is declared unsupported and `location` is offered as a passthrough instead of guessing at a mapping.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_tinyfish.py`:

```python
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.tinyfish import TinyFishAdapter

URL = "https://api.search.tinyfish.ai"

BODY = {
    "query": "cats",
    "results": [
        {"position": 1, "site_name": "a.com", "title": "Cats",
         "snippet": "about cats", "url": "https://a.com"},
        {"position": 2, "site_name": "b.com", "title": "More",
         "snippet": "more cats", "url": "https://b.com"},
    ],
    "total_results": 10,
    "page": 0,
}


@pytest.fixture
def adapter(client):
    return TinyFishAdapter("tinyfish", client, timeout_ms=5000, api_key="XXXXX")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_sends_api_key_header_and_query(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["x-api-key"] == "XXXXX"
    assert query_of(route)["query"] == ["cats"]


@respx.mock
async def test_parses_results(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].snippet == "about cats"
    assert results[0].score is None


@respx.mock
async def test_max_results_is_applied_client_side(adapter):
    """TinyFish has no count parameter, so the adapter truncates."""
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=1), {})
    assert len(results) == 1
    assert "max_results" not in query_of(route)


@respx.mock
@pytest.mark.parametrize(
    ("freshness", "minutes"),
    [("day", "1440"), ("week", "10080"), ("month", "43200"), ("year", "525600")],
)
async def test_freshness_becomes_recency_minutes(adapter, freshness, minutes):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["recency_minutes"] == [minutes]


@respx.mock
async def test_translates_dates_language_and_topic(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
        language="fr", topic="news"), {})
    params = query_of(route)
    assert params["after_date"] == ["2026-01-01"]
    assert params["before_date"] == ["2026-02-01"]
    assert params["language"] == ["fr"]
    assert params["domain_type"] == ["news"]


@respx.mock
async def test_passthrough_params_reach_the_query_string(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {"location": "Paris", "page": 2})
    params = query_of(route)
    assert params["location"] == ["Paris"] and params["page"] == ["2"]


@respx.mock
async def test_empty_results(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"query": "x", "results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
async def test_server_error_is_transient(adapter):
    respx.get(URL).mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.TRANSIENT


def test_capabilities():
    caps = TinyFishAdapter.capabilities
    assert caps.max_results_cap is None
    assert "freshness" in caps.supported_filters
    assert "country" not in caps.supported_filters
    assert "include_domains" not in caps.supported_filters
    assert "location" in caps.passthrough_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_tinyfish.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.tinyfish'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/tinyfish.py`**

```python
from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities

ENDPOINT = "https://api.search.tinyfish.ai"

RECENCY_MINUTES = {"day": 1440, "week": 10_080, "month": 43_200, "year": 525_600}


class TinyFishAdapter(Adapter):
    type_name = "tinyfish"
    capabilities = Capabilities(
        supported_filters=frozenset({"freshness", "start_date", "end_date",
                                     "language", "topic"}),
        max_results_cap=None,
        passthrough_params={
            "location": str,
            "purpose": str,
            "page": int,
            "pub_year_min": int,
            "pub_year_max": int,
            "domain_type": str,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "query": req.query,
            "domain_type": "news" if req.topic == "news" else "web",
        }
        if req.freshness:
            params["recency_minutes"] = RECENCY_MINUTES[req.freshness]
        if req.start_date:
            params["after_date"] = req.start_date.isoformat()
        if req.end_date:
            params["before_date"] = req.end_date.isoformat()
        if req.language:
            params["language"] = req.language
        params.update(passthrough)

        body = await self.request_json(
            "GET", ENDPOINT, params=params,
            headers={"X-API-Key": self.options["api_key"]},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        return [
            SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=row.get("snippet") or "",
                rank=index + 1,
            )
            for index, row in enumerate(rows[:limit])
            if row.get("url")
        ]
```

- [ ] **Step 4: Register it**

In `registry.py` add `from search_meta_api.providers.tinyfish import TinyFishAdapter`, add `TinyFishAdapter.type_name: TinyFishAdapter` to `ADAPTERS`, and `"tinyfish": ("api_key",)` to `REQUIRED_OPTIONS`. Uncomment the `tinyfish` line in `config.example.yaml`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_tinyfish.py tests/test_config.py -v`
Expected: PASS on tinyfish (12 passed); `test_example_config_is_valid` still fails on `unknown provider type 'brave'`.

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/tinyfish.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_tinyfish.py
git commit -m "feat: tinyfish adapter"
```

---

### Task 9: Brave adapter

**Files:**
- Create: `src/search_meta_api/providers/brave.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_brave.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities`, `domain_operators` (Task 4).
- Produces: `BraveAdapter`, `type_name = "brave"`.

Brave has no domain include/exclude parameters, so they are emulated with `site:` operators folded into `q` — which is why `include_domains` and `exclude_domains` are still declared supported. `freshness` carries both the relative values (`pd`/`pw`/`pm`/`py`) and custom ranges (`YYYY-MM-DDtoYYYY-MM-DD`); a date range wins over a relative value since it is more specific. Brave uses uppercase country codes and lowercase language codes.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_brave.py`:

```python
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.brave import BraveAdapter

URL = "https://api.search.brave.com/res/v1/web/search"

BODY = {
    "query": {"original": "cats", "more_results_available": True},
    "web": {"results": [
        {"title": "Cats", "url": "https://a.com", "description": "about cats",
         "page_age": "2026-01-05T00:00:00"},
        {"title": "More", "url": "https://b.com", "description": "more cats"},
    ]},
}


@pytest.fixture
def adapter(client):
    return BraveAdapter("brave", client, timeout_ms=5000, api_key="BSA-KEY")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_sends_subscription_token(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["x-subscription-token"] == "BSA-KEY"


@respx.mock
async def test_parses_results_and_published_date(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert results[0].snippet == "about cats"
    assert results[0].published_date == "2026-01-05T00:00:00"
    assert results[1].published_date is None


@respx.mock
async def test_domains_become_search_operators(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com", "b.com"], exclude_domains=["c.com"]), {})
    assert query_of(route)["q"] == ["cats (site:a.com OR site:b.com) -site:c.com"]


@respx.mock
@pytest.mark.parametrize(
    ("freshness", "code"),
    [("day", "pd"), ("week", "pw"), ("month", "pm"), ("year", "py")],
)
async def test_relative_freshness_codes(adapter, freshness, code):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["freshness"] == [code]


@respx.mock
async def test_date_range_overrides_relative_freshness(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", freshness="day",
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)), {})
    assert query_of(route)["freshness"] == ["2026-01-01to2026-02-01"]


@respx.mock
async def test_one_sided_date_range_uses_open_bounds(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", start_date=date(2026, 1, 1)), {})
    assert query_of(route)["freshness"] == ["2026-01-01to2999-12-31"]


@respx.mock
async def test_count_country_language_and_safesearch(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, country="fr", language="fr",
        safe_search="strict"), {})
    params = query_of(route)
    assert params["count"] == ["7"]
    assert params["country"] == ["FR"]        # Brave wants uppercase
    assert params["search_lang"] == ["fr"]    # and lowercase here
    assert params["safesearch"] == ["strict"]


@respx.mock
async def test_missing_web_key_is_an_empty_result_set_not_an_error(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"query": {"original": "x"}}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
async def test_rate_limit_is_quota(adapter):
    respx.get(URL).mock(return_value=httpx.Response(429, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.QUOTA


def test_capabilities():
    caps = BraveAdapter.capabilities
    assert caps.max_results_cap == 20
    assert "include_domains" in caps.supported_filters   # via site: operators
    assert "topic" not in caps.supported_filters          # separate endpoints
    assert "extra_snippets" in caps.passthrough_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_brave.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.brave'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/brave.py`**

```python
from __future__ import annotations

from typing import Any

from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, domain_operators

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

FRESHNESS_CODES = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
OPEN_START = "1970-01-01"
OPEN_END = "2999-12-31"


class BraveAdapter(Adapter):
    type_name = "brave"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "start_date", "end_date", "country", "language", "safe_search",
        }),
        max_results_cap=20,
        passthrough_params={
            "extra_snippets": bool,
            "offset": int,
            "ui_lang": str,
            "enable_rich_callback": int,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "q": domain_operators(req.query, req.include_domains, req.exclude_domains),
            "count": req.max_results,
        }
        if freshness := self._freshness(req):
            params["freshness"] = freshness
        if req.country:
            params["country"] = req.country.upper()
        if req.language:
            params["search_lang"] = req.language
        if req.safe_search:
            params["safesearch"] = req.safe_search
        params.update(passthrough)

        body = await self.request_json(
            "GET", ENDPOINT, params=params,
            headers={"X-Subscription-Token": self.options["api_key"],
                     "Accept": "application/json"},
        )
        return self._parse(body, req.max_results)

    @staticmethod
    def _freshness(req: EffectiveRequest) -> str | None:
        """A date range is more specific than a relative window, so it wins."""
        if req.start_date or req.end_date:
            start = req.start_date.isoformat() if req.start_date else OPEN_START
            end = req.end_date.isoformat() if req.end_date else OPEN_END
            return f"{start}to{end}"
        if req.freshness:
            return FRESHNESS_CODES[req.freshness]
        return None

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = (body.get("web") or {}).get("results") or []
        return [
            SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=row.get("description") or "",
                rank=index + 1,
                published_date=row.get("page_age"),
            )
            for index, row in enumerate(rows[:limit])
            if row.get("url")
        ]
```

- [ ] **Step 4: Register it**

Add the import, the `ADAPTERS` entry, and `"brave": ("api_key",)` to `REQUIRED_OPTIONS`. Uncomment the `brave` line in `config.example.yaml`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_brave.py -v`
Expected: PASS — 13 passed

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/brave.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_brave.py
git commit -m "feat: brave adapter with operator-based domain filtering"
```

---

### Task 10: Exa adapter

**Files:**
- Create: `src/search_meta_api/providers/exa.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_exa.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities` (Task 4).
- Produces: `ExaAdapter`, `type_name = "exa"`.

Exa returns **no snippet at all** unless `contents` is requested, so the adapter always asks for `contents.highlights` — without it every result would have an empty snippet and be useless to an agent. Dates are ISO 8601 *datetimes*, not plain dates.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_exa.py`:

```python
import json
from datetime import date

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.exa import ExaAdapter

URL = "https://api.exa.ai/search"

BODY = {
    "requestId": "abc",
    "results": [
        {"title": "Cats", "url": "https://a.com", "publishedDate": "2026-01-05T00:00:00.000Z",
         "favicon": "https://a.com/f.ico", "highlights": ["about cats", "more"],
         "highlightScores": [0.88, 0.4]},
        {"title": "More", "url": "https://b.com", "highlights": []},
    ],
    "costDollars": {"total": 0.007},
}


@pytest.fixture
def adapter(client):
    return ExaAdapter("exa", client, timeout_ms=5000, api_key="exa-key")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_api_key_header(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["x-api-key"] == "exa-key"


@respx.mock
async def test_always_requests_highlights_so_snippets_exist(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert sent(route)["contents"] == {"highlights": {"maxCharacters": 500}}


@respx.mock
async def test_snippet_comes_from_the_first_highlight(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert results[0].snippet == "about cats"
    assert results[0].score == 0.88
    assert results[0].published_date == "2026-01-05T00:00:00.000Z"
    assert results[0].favicon == "https://a.com/f.ico"
    assert results[1].snippet == ""
    assert results[1].score is None


@respx.mock
async def test_translates_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, include_domains=["a.com"], exclude_domains=["b.com"],
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1), country="fr",
        safe_search="strict", topic="news"), {})
    payload = sent(route)
    assert payload["numResults"] == 7
    assert payload["includeDomains"] == ["a.com"]
    assert payload["excludeDomains"] == ["b.com"]
    assert payload["startPublishedDate"] == "2026-01-01T00:00:00.000Z"
    assert payload["endPublishedDate"] == "2026-02-01T00:00:00.000Z"
    assert payload["userLocation"] == "FR"
    assert payload["moderation"] is True
    assert payload["category"] == "news"


@respx.mock
async def test_safe_search_off_disables_moderation(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", safe_search="off"), {})
    assert sent(route)["moderation"] is False


@respx.mock
async def test_general_topic_sends_no_category(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert "category" not in sent(route)


@respx.mock
async def test_passthrough_params(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {"type": "fast"})
    assert sent(route)["type"] == "fast"


@respx.mock
async def test_payment_required_is_quota(adapter):
    respx.post(URL).mock(return_value=httpx.Response(402, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.QUOTA


@respx.mock
async def test_empty_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = ExaAdapter.capabilities
    assert caps.max_results_cap == 100
    assert "freshness" not in caps.supported_filters   # no relative window
    assert "language" not in caps.supported_filters
    assert "start_date" in caps.supported_filters
    assert "type" in caps.passthrough_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_exa.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.exa'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/exa.py`**

```python
from __future__ import annotations

from datetime import date
from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities

ENDPOINT = "https://api.exa.ai/search"


def _iso_datetime(value: date) -> str:
    return f"{value.isoformat()}T00:00:00.000Z"


class ExaAdapter(Adapter):
    type_name = "exa"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "start_date", "end_date",
            "country", "safe_search", "topic",
        }),
        max_results_cap=100,
        passthrough_params={
            "type": str,
            "category": str,
            "additionalQueries": list,
            "compliance": str,
            "contents": dict,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": req.query,
            "numResults": req.max_results,
            # Without `contents`, Exa returns no snippet at all.
            "contents": {"highlights": {"maxCharacters": 500}},
        }
        if req.include_domains:
            payload["includeDomains"] = req.include_domains
        if req.exclude_domains:
            payload["excludeDomains"] = req.exclude_domains
        if req.start_date:
            payload["startPublishedDate"] = _iso_datetime(req.start_date)
        if req.end_date:
            payload["endPublishedDate"] = _iso_datetime(req.end_date)
        if req.country:
            payload["userLocation"] = req.country.upper()
        if req.safe_search:
            payload["moderation"] = req.safe_search != "off"
        if req.topic == "news":
            payload["category"] = "news"
        payload.update(passthrough)

        body = await self.request_json(
            "POST", ENDPOINT, json=payload,
            headers={"x-api-key": self.options["api_key"]},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        results = []
        for index, row in enumerate(rows[:limit]):
            if not row.get("url"):
                continue
            highlights = row.get("highlights") or []
            scores = row.get("highlightScores") or []
            results.append(SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=highlights[0] if highlights else "",
                rank=index + 1,
                published_date=row.get("publishedDate"),
                score=scores[0] if scores else None,
                favicon=row.get("favicon"),
            ))
        return results
```

- [ ] **Step 4: Register it**

Add the import, the `ADAPTERS` entry, and `"exa": ("api_key",)`. Uncomment the `exa` line in `config.example.yaml`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_exa.py -v`
Expected: PASS — 12 passed

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/exa.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_exa.py
git commit -m "feat: exa adapter"
```

---

### Task 11: Linkup adapter

**Files:**
- Create: `src/search_meta_api/providers/linkup.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_linkup.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities` (Task 4).
- Produces: `LinkupAdapter`, `type_name = "linkup"`.

Linkup has the narrowest filter surface of the fleet — no freshness window, country, language, safe search or topic. `depth` and `outputType` are required by the vendor, so the adapter always sends `standard` and `searchResults`; both are overridable via passthrough. Results are a mixed `text`/`image` list and only `text` rows are kept.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_linkup.py`:

```python
import json
from datetime import date

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.linkup import LinkupAdapter

URL = "https://api.linkup.so/v1/search"

BODY = {"results": [
    {"type": "text", "name": "Cats", "url": "https://a.com", "content": "about cats",
     "favicon": "https://a.com/f.ico"},
    {"type": "image", "name": "Cat pic", "url": "https://a.com/cat.png"},
    {"type": "text", "name": "More", "url": "https://b.com", "content": "more cats"},
]}


@pytest.fixture
def adapter(client):
    return LinkupAdapter("linkup", client, timeout_ms=5000, api_key="lk-key")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_bearer_auth_and_required_vendor_fields(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["authorization"] == "Bearer lk-key"
    payload = sent(route)
    assert payload["q"] == "cats"
    assert payload["depth"] == "standard"
    assert payload["outputType"] == "searchResults"


@respx.mock
async def test_image_rows_are_dropped_and_ranks_are_contiguous(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert [r.rank for r in results] == [1, 2]
    assert results[0].snippet == "about cats"
    assert results[0].favicon == "https://a.com/f.ico"


@respx.mock
async def test_translates_supported_filters(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", max_results=7, include_domains=["a.com"], exclude_domains=["b.com"],
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)), {})
    payload = sent(route)
    assert payload["maxResults"] == 7
    assert payload["includeDomains"] == ["a.com"]
    assert payload["excludeDomains"] == ["b.com"]
    assert payload["fromDate"] == "2026-01-01"
    assert payload["toDate"] == "2026-02-01"


@respx.mock
async def test_passthrough_overrides_depth(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {"depth": "deep"})
    assert sent(route)["depth"] == "deep"


@respx.mock
async def test_truncation_counts_text_rows_only(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats", max_results=1), {})
    assert [r.title for r in results] == ["Cats"]


@respx.mock
@pytest.mark.parametrize(("status", "expected"),
                         [(402, ErrorClass.QUOTA), (429, ErrorClass.QUOTA),
                          (401, ErrorClass.AUTH)])
async def test_error_classification(adapter, status, expected):
    respx.post(URL).mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is expected


@respx.mock
async def test_empty_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = LinkupAdapter.capabilities
    assert caps.supported_filters == frozenset({
        "include_domains", "exclude_domains", "start_date", "end_date"})
    assert caps.max_results_cap is None
    assert "depth" in caps.passthrough_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_linkup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.linkup'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/linkup.py`**

```python
from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities

ENDPOINT = "https://api.linkup.so/v1/search"


class LinkupAdapter(Adapter):
    type_name = "linkup"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "start_date", "end_date",
        }),
        max_results_cap=None,
        passthrough_params={
            "depth": str,
            "outputType": str,
            "structuredOutputSchema": str,
            "includeImages": bool,
            "includeInlineCitations": bool,
            "includeSources": bool,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "q": req.query,
            "depth": "standard",          # required by the vendor
            "outputType": "searchResults",
            "maxResults": req.max_results,
        }
        if req.include_domains:
            payload["includeDomains"] = req.include_domains
        if req.exclude_domains:
            payload["excludeDomains"] = req.exclude_domains
        if req.start_date:
            payload["fromDate"] = req.start_date.isoformat()
        if req.end_date:
            payload["toDate"] = req.end_date.isoformat()
        payload.update(passthrough)

        body = await self.request_json(
            "POST", ENDPOINT, json=payload,
            headers={"Authorization": f"Bearer {self.options['api_key']}"},
        )
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        results = []
        for row in rows:
            if row.get("type") != "text" or not row.get("url"):
                continue
            results.append(SearchResult(
                title=row.get("name") or "",
                url=row["url"],
                snippet=row.get("content") or "",
                rank=len(results) + 1,
                favicon=row.get("favicon"),
            ))
            if len(results) == limit:
                break
        return results
```

- [ ] **Step 4: Register it**

Add the import, the `ADAPTERS` entry, and `"linkup": ("api_key",)`. Uncomment the `linkup` line in `config.example.yaml`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_linkup.py -v`
Expected: PASS — 10 passed

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/linkup.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_linkup.py
git commit -m "feat: linkup adapter"
```

---

### Task 12: Firecrawl adapter

**Files:**
- Create: `src/search_meta_api/providers/firecrawl.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_firecrawl.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities`, `domain_operators` (Task 4).
- Produces: `FirecrawlAdapter`, `type_name = "firecrawl"`.

Two vendor quirks drive the implementation:

- `includeDomains` and `excludeDomains` are **mutually exclusive**. Rather than dropping one, the adapter only ever sends `includeDomains` as a parameter and folds exclusions into the query as `-site:` operators, so both filters survive.
- A `200` response carrying `{"success": false}` is a failure. It must be classified as `MALFORMED` and fail over, not returned as an empty result set.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_firecrawl.py`:

```python
import json

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.firecrawl import FirecrawlAdapter

URL = "https://api.firecrawl.dev/v2/search"

BODY = {
    "success": True,
    "data": {"web": [
        {"title": "Cats", "description": "about cats", "url": "https://a.com"},
        {"title": "More", "description": "more cats", "url": "https://b.com"},
    ]},
    "creditsUsed": 1,
}


@pytest.fixture
def adapter(client):
    return FirecrawlAdapter("firecrawl", client, timeout_ms=5000, api_key="fc-key")


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_sends_bearer_auth_and_web_source(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert route.calls.last.request.headers["authorization"] == "Bearer fc-key"
    payload = sent(route)
    assert payload["query"] == "cats"
    assert payload["sources"] == [{"type": "web"}]


@respx.mock
async def test_parses_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert results[0].snippet == "about cats"
    assert [r.rank for r in results] == [1, 2]


@respx.mock
async def test_news_topic_switches_the_source_and_reads_the_news_array(adapter):
    respx.post(URL).mock(return_value=httpx.Response(200, json={
        "success": True,
        "data": {"news": [{"title": "N", "snippet": "news snippet",
                           "url": "https://n.com", "date": "2026-01-05"}]},
    }))
    results = await adapter.search(EffectiveRequest(query="cats", topic="news"), {})
    assert results[0].title == "N"
    assert results[0].snippet == "news snippet"
    assert results[0].published_date == "2026-01-05"


@respx.mock
async def test_exclusions_become_query_operators_not_a_parameter(adapter):
    """includeDomains and excludeDomains are mutually exclusive at the vendor."""
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com"], exclude_domains=["c.com"]), {})
    payload = sent(route)
    assert payload["includeDomains"] == ["a.com"]
    assert "excludeDomains" not in payload
    assert payload["query"] == "cats -site:c.com"


@respx.mock
@pytest.mark.parametrize(("freshness", "tbs"),
                         [("day", "qdr:d"), ("week", "qdr:w"),
                          ("month", "qdr:m"), ("year", "qdr:y")])
async def test_freshness_becomes_tbs(adapter, freshness, tbs):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert sent(route)["tbs"] == tbs


@respx.mock
async def test_limit_and_country(adapter):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", max_results=7, country="fr"), {})
    payload = sent(route)
    assert payload["limit"] == 7
    assert payload["country"] == "fr"


@respx.mock
async def test_success_false_on_a_200_is_a_failover_error(adapter):
    respx.post(URL).mock(return_value=httpx.Response(
        200, json={"success": False, "error": "upstream exploded"}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.MALFORMED


@respx.mock
async def test_timeout_status_is_transient(adapter):
    respx.post(URL).mock(return_value=httpx.Response(408, json={"success": False}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.TRANSIENT


@respx.mock
async def test_empty_results(adapter):
    respx.post(URL).mock(return_value=httpx.Response(
        200, json={"success": True, "data": {"web": []}}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = FirecrawlAdapter.capabilities
    assert caps.max_results_cap == 100
    assert "freshness" in caps.supported_filters
    assert "language" not in caps.supported_filters
    assert "start_date" not in caps.supported_filters
    assert "scrapeOptions" in caps.passthrough_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_firecrawl.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.firecrawl'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/firecrawl.py`**

```python
from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, domain_operators

ENDPOINT = "https://api.firecrawl.dev/v2/search"

TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


class FirecrawlAdapter(Adapter):
    type_name = "firecrawl"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness", "country", "topic",
        }),
        max_results_cap=100,
        passthrough_params={
            "scrapeOptions": dict,
            "categories": list,
            "location": str,
            "highlights": bool,
            "ignoreInvalidURLs": bool,
            "timeout": int,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        source = "news" if req.topic == "news" else "web"
        payload: dict[str, Any] = {
            # excludeDomains cannot coexist with includeDomains, so exclusions
            # ride along as query operators instead of being dropped.
            "query": domain_operators(req.query, [], req.exclude_domains),
            "limit": req.max_results,
            "sources": [{"type": source}],
        }
        if req.include_domains:
            payload["includeDomains"] = req.include_domains
        if req.freshness:
            payload["tbs"] = TBS[req.freshness]
        if req.country:
            payload["country"] = req.country
        payload.update(passthrough)

        body = await self.request_json(
            "POST", ENDPOINT, json=payload,
            headers={"Authorization": f"Bearer {self.options['api_key']}"},
        )
        if body.get("success") is False:
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message=str(body.get("error"))[:200])
        return self._parse(body, source, req.max_results)

    def _parse(self, body: dict[str, Any], source: str, limit: int) -> list[SearchResult]:
        rows = (body.get("data") or {}).get(source) or []
        return [
            SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=row.get("description") or row.get("snippet") or "",
                rank=index + 1,
                published_date=row.get("date"),
            )
            for index, row in enumerate(rows[:limit])
            if row.get("url")
        ]
```

- [ ] **Step 4: Register it**

Add the import, the `ADAPTERS` entry, and `"firecrawl": ("api_key",)`. Uncomment the `firecrawl` line in `config.example.yaml`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_firecrawl.py -v`
Expected: PASS — 13 passed

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/firecrawl.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_firecrawl.py
git commit -m "feat: firecrawl adapter"
```

---

### Task 13: Google Programmable Search adapter

**Files:**
- Create: `src/search_meta_api/providers/google_cse.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_google_cse.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities`, `domain_operators` (Task 4).
- Produces: `GoogleCseAdapter`, `type_name = "google_cse"`. This is the only type requiring **two** config options: `api_key` and `cx`.

Three constraints make this the most awkward adapter:

- `num` caps at **10**, so `max_results_cap = 10`. A caller asking for 25 gets 10 from this provider.
- `siteSearch` accepts only one domain, so domain filters go through `site:` operators instead — which handles multiple domains and include/exclude together.
- A `403` means either a bad key **or** an exhausted quota. The two need opposite treatment, so `classify` is overridden to read `error.errors[].reason` from the body.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_google_cse.py`:

```python
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.google_cse import GoogleCseAdapter

URL = "https://www.googleapis.com/customsearch/v1"

BODY = {
    "kind": "customsearch#search",
    "items": [
        {"title": "Cats", "link": "https://a.com", "snippet": "about cats"},
        {"title": "More", "link": "https://b.com", "snippet": "more cats"},
    ],
}


def quota_error(reason: str) -> dict:
    return {"error": {"code": 403, "message": "quota", "errors": [{"reason": reason}]}}


@pytest.fixture
def adapter(client):
    return GoogleCseAdapter("google", client, timeout_ms=5000,
                            api_key="g-key", cx="cx-123")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_sends_key_and_cx_as_query_params(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    params = query_of(route)
    assert params["key"] == ["g-key"]
    assert params["cx"] == ["cx-123"]
    assert params["q"] == ["cats"]


@respx.mock
async def test_parses_items_using_link_not_url(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.url for r in results] == ["https://a.com", "https://b.com"]
    assert results[0].snippet == "about cats"
    assert [r.rank for r in results] == [1, 2]


@respx.mock
async def test_max_results_is_capped_at_ten_by_the_vendor(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", max_results=10), {})
    assert query_of(route)["num"] == ["10"]
    assert GoogleCseAdapter.capabilities.max_results_cap == 10


@respx.mock
async def test_domains_become_operators_since_sitesearch_takes_only_one(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com", "b.com"], exclude_domains=["c.com"]), {})
    assert query_of(route)["q"] == ["cats (site:a.com OR site:b.com) -site:c.com"]
    assert "siteSearch" not in query_of(route)


@respx.mock
@pytest.mark.parametrize(("freshness", "restrict"),
                         [("day", "d1"), ("week", "w1"), ("month", "m1"), ("year", "y1")])
async def test_freshness_becomes_date_restrict(adapter, freshness, restrict):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["dateRestrict"] == [restrict]


@respx.mock
async def test_country_language_and_safesearch(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", country="fr", language="fr", safe_search="strict"), {})
    params = query_of(route)
    assert params["gl"] == ["fr"]
    assert params["lr"] == ["lang_fr"]
    assert params["safe"] == ["active"]


@respx.mock
async def test_safe_search_off_maps_to_off(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", safe_search="off"), {})
    assert query_of(route)["safe"] == ["off"]


@respx.mock
async def test_missing_items_key_means_no_matches(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"kind": "customsearch#search"}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


@respx.mock
@pytest.mark.parametrize("reason", ["dailyLimitExceeded", "rateLimitExceeded",
                                    "userRateLimitExceeded"])
async def test_403_with_a_quota_reason_is_quota_not_auth(adapter, reason):
    respx.get(URL).mock(return_value=httpx.Response(403, json=quota_error(reason)))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.QUOTA


@respx.mock
async def test_403_without_a_quota_reason_stays_auth(adapter):
    respx.get(URL).mock(return_value=httpx.Response(403, json=quota_error("keyInvalid")))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.AUTH


@respx.mock
async def test_403_with_an_unparseable_body_stays_auth(adapter):
    respx.get(URL).mock(return_value=httpx.Response(403, text="nope"))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.AUTH


def test_capabilities():
    caps = GoogleCseAdapter.capabilities
    assert "start_date" not in caps.supported_filters
    assert "topic" not in caps.supported_filters
    assert "include_domains" in caps.supported_filters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_google_cse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.google_cse'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/google_cse.py`**

```python
from __future__ import annotations

from typing import Any

import httpx

from search_meta_api.errors import ErrorClass, classify_status
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, domain_operators

ENDPOINT = "https://www.googleapis.com/customsearch/v1"

DATE_RESTRICT = {"day": "d1", "week": "w1", "month": "m1", "year": "y1"}
QUOTA_REASONS = frozenset({"dailyLimitExceeded", "rateLimitExceeded",
                           "userRateLimitExceeded", "quotaExceeded"})


class GoogleCseAdapter(Adapter):
    """Programmable Search Engine, not Google web search.

    The configured engine (`cx`) must be set to search the entire web, and the
    vendor caps a single call at 10 results.
    """

    type_name = "google_cse"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "country", "language", "safe_search",
        }),
        max_results_cap=10,
        passthrough_params={
            "searchType": str,
            "fileType": str,
            "rights": str,
            "sort": str,
            "start": int,
            "exactTerms": str,
            "excludeTerms": str,
        },
    )

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "key": self.options["api_key"],
            "cx": self.options["cx"],
            # siteSearch takes a single domain, so operators do the work instead.
            "q": domain_operators(req.query, req.include_domains, req.exclude_domains),
            "num": req.max_results,
        }
        if req.freshness:
            params["dateRestrict"] = DATE_RESTRICT[req.freshness]
        if req.country:
            params["gl"] = req.country
        if req.language:
            params["lr"] = f"lang_{req.language}"
        if req.safe_search:
            params["safe"] = "off" if req.safe_search == "off" else "active"
        params.update(passthrough)

        body = await self.request_json("GET", ENDPOINT, params=params)
        return self._parse(body, req.max_results)

    def classify(self, response: httpx.Response) -> ErrorClass:
        """A 403 is either a bad key or an exhausted quota — the body decides."""
        if response.status_code != 403:
            return classify_status(response.status_code)
        try:
            errors = (response.json().get("error") or {}).get("errors") or []
        except ValueError:
            return ErrorClass.AUTH
        if any(entry.get("reason") in QUOTA_REASONS for entry in errors):
            return ErrorClass.QUOTA
        return ErrorClass.AUTH

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("items") or []
        return [
            SearchResult(
                title=row.get("title") or "",
                url=row["link"],
                snippet=row.get("snippet") or "",
                rank=index + 1,
            )
            for index, row in enumerate(rows[:limit])
            if row.get("link")
        ]
```

- [ ] **Step 4: Register it**

Add the import, the `ADAPTERS` entry, and `"google_cse": ("api_key", "cx")` — note the two required options. Uncomment the `google` line in `config.example.yaml`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/providers/test_google_cse.py -v`
Expected: PASS — 17 passed

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/google_cse.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_google_cse.py
git commit -m "feat: google programmable search adapter with quota-aware 403 handling"
```

---

### Task 14: SearXNG adapter

**Files:**
- Create: `src/search_meta_api/providers/searxng.py`
- Modify: `src/search_meta_api/providers/registry.py`, `config.example.yaml`
- Test: `tests/providers/test_searxng.py`

**Interfaces:**
- Consumes: `Adapter`, `Capabilities`, `domain_operators` (Task 4).
- Produces: `SearxngAdapter`, `type_name = "searxng"`. The only type requiring `base_url` and **no** `api_key`.

Self-hosted, so no auth and no quota. Two things to get right:

- `format=json` must be enabled in the instance's `settings.yml`, otherwise every request returns **403**. That is a config error, not a transient one, so it classifies as `AUTH` and gets logged loudly by the router.
- `time_range` accepts `day`, `month`, `year` — there is **no week**. This is the value-dependent gap `extra_degraded` exists for: `freshness` is supported in general but reported degraded when the caller asks for `week`.

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_searxng.py`:

```python
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest
from search_meta_api.providers.searxng import SearxngAdapter

URL = "https://xng.example.test/search"

BODY = {"query": "cats", "results": [
    {"title": "Cats", "url": "https://a.com", "content": "about cats",
     "score": 1.5, "publishedDate": "2026-01-05T00:00:00"},
    {"title": "More", "url": "https://b.com", "content": "more cats", "score": 0.9},
]}


@pytest.fixture
def adapter(client):
    return SearxngAdapter("searxng", client, timeout_ms=5000,
                          base_url="https://xng.example.test")


def query_of(route) -> dict:
    return parse_qs(urlparse(str(route.calls.last.request.url)).query)


@respx.mock
async def test_requests_json_format_from_the_configured_instance(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    params = query_of(route)
    assert params["q"] == ["cats"]
    assert params["format"] == ["json"]


@respx.mock
async def test_trailing_slash_in_base_url_does_not_double_up(client):
    adapter = SearxngAdapter("searxng", client, base_url="https://xng.example.test/")
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats"), {})
    assert str(route.calls.last.request.url).startswith(URL)


@respx.mock
async def test_parses_results_with_score_and_published_date(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    results = await adapter.search(EffectiveRequest(query="cats"), {})
    assert [r.title for r in results] == ["Cats", "More"]
    assert results[0].snippet == "about cats"
    assert results[0].score == 1.5
    assert results[0].published_date == "2026-01-05T00:00:00"
    assert results[1].published_date is None


@respx.mock
async def test_domains_become_operators(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(
        query="cats", include_domains=["a.com"], exclude_domains=["c.com"]), {})
    assert query_of(route)["q"] == ["cats (site:a.com) -site:c.com"]


@respx.mock
@pytest.mark.parametrize("freshness", ["day", "month", "year"])
async def test_supported_time_ranges_pass_through(adapter, freshness):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness=freshness), {})
    assert query_of(route)["time_range"] == [freshness]


@respx.mock
async def test_week_is_dropped_because_searxng_has_no_week_window(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", freshness="week"), {})
    assert "time_range" not in query_of(route)


def test_week_is_reported_as_degraded_but_other_windows_are_not(adapter):
    assert adapter.extra_degraded(EffectiveRequest(query="c", freshness="week")) == ["freshness"]
    assert adapter.extra_degraded(EffectiveRequest(query="c", freshness="day")) == []
    assert adapter.extra_degraded(EffectiveRequest(query="c")) == []


@respx.mock
@pytest.mark.parametrize(("safe_search", "level"),
                         [("off", "0"), ("moderate", "1"), ("strict", "2")])
async def test_safesearch_levels(adapter, safe_search, level):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", safe_search=safe_search), {})
    assert query_of(route)["safesearch"] == [level]


@respx.mock
async def test_language_and_news_category(adapter):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=BODY))
    await adapter.search(EffectiveRequest(query="cats", language="fr", topic="news"), {})
    params = query_of(route)
    assert params["language"] == ["fr"]
    assert params["categories"] == ["news"]


@respx.mock
async def test_403_means_json_format_is_disabled_and_classifies_as_auth(adapter):
    respx.get(URL).mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(ProviderError) as exc:
        await adapter.search(EffectiveRequest(query="x"), {})
    assert exc.value.error_class is ErrorClass.AUTH


@respx.mock
async def test_empty_results(adapter):
    respx.get(URL).mock(return_value=httpx.Response(200, json={"results": []}))
    assert await adapter.search(EffectiveRequest(query="x"), {}) == []


def test_capabilities():
    caps = SearxngAdapter.capabilities
    assert caps.max_results_cap is None
    assert "country" not in caps.supported_filters
    assert "freshness" in caps.supported_filters
    assert "pageno" in caps.passthrough_params
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/providers/test_searxng.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.providers.searxng'`

- [ ] **Step 3: Implement `src/search_meta_api/providers/searxng.py`**

```python
from __future__ import annotations

from typing import Any

from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import EffectiveRequest, SearchResult
from search_meta_api.providers.base import Adapter, Capabilities, domain_operators

# SearXNG offers day/month/year windows. There is no week.
TIME_RANGES = {"day": "day", "month": "month", "year": "year"}
SAFESEARCH_LEVELS = {"off": 0, "moderate": 1, "strict": 2}


class SearxngAdapter(Adapter):
    type_name = "searxng"
    capabilities = Capabilities(
        supported_filters=frozenset({
            "include_domains", "exclude_domains", "freshness",
            "language", "safe_search", "topic",
        }),
        max_results_cap=None,
        passthrough_params={"pageno": int, "engines": str, "categories": str},
    )

    def extra_degraded(self, req: EffectiveRequest) -> list[str]:
        if req.freshness and req.freshness not in TIME_RANGES:
            return ["freshness"]
        return []

    async def search(
        self, req: EffectiveRequest, passthrough: dict[str, Any]
    ) -> list[SearchResult]:
        params: dict[str, Any] = {
            "q": domain_operators(req.query, req.include_domains, req.exclude_domains),
            "format": "json",
            "categories": "news" if req.topic == "news" else "general",
        }
        if req.freshness in TIME_RANGES:
            params["time_range"] = TIME_RANGES[req.freshness]
        if req.language:
            params["language"] = req.language
        if req.safe_search:
            params["safesearch"] = SAFESEARCH_LEVELS[req.safe_search]
        params.update(passthrough)

        base = self.options["base_url"].rstrip("/")
        body = await self.request_json("GET", f"{base}/search", params=params)
        return self._parse(body, req.max_results)

    def _parse(self, body: dict[str, Any], limit: int) -> list[SearchResult]:
        rows = body.get("results")
        if not isinstance(rows, list):
            raise ProviderError(self.name, ErrorClass.MALFORMED, status=200,
                                message="missing 'results' array")
        return [
            SearchResult(
                title=row.get("title") or "",
                url=row["url"],
                snippet=row.get("content") or "",
                rank=index + 1,
                score=row.get("score"),
                published_date=row.get("publishedDate"),
            )
            for index, row in enumerate(rows[:limit])
            if row.get("url")
        ]
```

- [ ] **Step 4: Register it**

Add the import, the `ADAPTERS` entry, and `"searxng": ("base_url",)` — this one needs no `api_key`. Uncomment the `searxng` line in `config.example.yaml`; every provider line should now be active.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/pytest -v`
Expected: PASS — everything green, including `test_example_config_is_valid`, which has been red since Task 7 and now finds all nine instances resolvable.

- [ ] **Step 6: Commit**

```bash
git add src/search_meta_api/providers/searxng.py src/search_meta_api/providers/registry.py \
        config.example.yaml tests/providers/test_searxng.py
git commit -m "feat: searxng adapter completing the provider fleet"
```

---

### Task 15: Edge validation and the FastAPI app

**Files:**
- Create: `src/search_meta_api/validation.py`, `src/search_meta_api/app.py`
- Modify: `src/search_meta_api/models.py` (one field), `tests/test_models.py`
- Test: `tests/test_validation.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `ParamError`, `validate_params(req, instances)`, `create_app(settings, instances=None) -> FastAPI`.

Validation runs **before any provider is called**, which is what makes the error classification in Task 2 safe to have no fail-fast class. A typo like `tvaily/search_depth` returns 422 immediately instead of becoming a vendor 400 halfway down the chain.

- [ ] **Step 1: Widen `SearchResponse.provider` to allow "nobody won"**

When every provider answers but all return zero results, the response is a legitimate `200` with no winner. In `src/search_meta_api/models.py` change:

```python
class SearchResponse(BaseModel):
    query: str
    provider: str | None = None      # None when every provider answered emptily
```

Add to `tests/test_models.py`:

```python
def test_search_response_allows_no_winner():
    from search_meta_api.models import SearchResponse
    response = SearchResponse(query="x", results=[], elapsed_ms=12)
    assert response.provider is None
```

- [ ] **Step 2: Write the failing validation test**

Create `tests/test_validation.py`:

```python
import httpx
import pytest

from search_meta_api.models import SearchRequest
from search_meta_api.providers.base import Adapter, Capabilities
from search_meta_api.router import ProviderInstance
from search_meta_api.validation import ParamError, validate_params


class FakeTavily(Adapter):
    type_name = "tavily"
    capabilities = Capabilities(
        supported_filters=frozenset({"country"}),
        max_results_cap=20,
        passthrough_params={"search_depth": str, "chunks_per_source": int},
    )

    async def search(self, req, passthrough):
        return []


@pytest.fixture
def instances():
    client = httpx.AsyncClient()
    return [
        ProviderInstance("tavily-a", FakeTavily("tavily-a", client)),
        ProviderInstance("tavily-b", FakeTavily("tavily-b", client)),
    ]


def check(params, instances):
    validate_params(SearchRequest(query="x", params=params), instances)


def test_valid_params_pass(instances):
    check({"*/country": "fr", "tavily/search_depth": "advanced",
           "tavily-b/chunks_per_source": 2}, instances)


def test_unscoped_key_is_rejected(instances):
    with pytest.raises(ParamError, match="must be scoped"):
        check({"country": "fr"}, instances)


def test_unknown_scope_is_rejected(instances):
    with pytest.raises(ParamError, match="unknown scope 'tvaily'"):
        check({"tvaily/search_depth": "advanced"}, instances)


def test_universal_scope_only_accepts_unified_fields(instances):
    with pytest.raises(ParamError, match="not a unified field"):
        check({"*/search_depth": "advanced"}, instances)


def test_unknown_vendor_param_is_rejected(instances):
    with pytest.raises(ParamError, match="no parameter 'serch_depth'"):
        check({"tavily/serch_depth": "advanced"}, instances)


def test_wrong_passthrough_type_is_rejected(instances):
    with pytest.raises(ParamError, match="must be int"):
        check({"tavily/chunks_per_source": "two"}, instances)


def test_scoped_unified_field_is_allowed(instances):
    check({"tavily-b/max_results": 15}, instances)


def test_flat_field_and_universal_twin_together_is_rejected(instances):
    req = SearchRequest(query="x", country="fr", params={"*/country": "de"})
    with pytest.raises(ParamError, match="both as a top-level field"):
        validate_params(req, instances)


def test_flat_field_alone_is_fine(instances):
    validate_params(SearchRequest(query="x", country="fr"), instances)


def test_bad_value_for_a_unified_field_is_caught_before_any_provider_runs(instances):
    with pytest.raises(ParamError, match="invalid value"):
        check({"*/freshness": "fortnight"}, instances)


def test_out_of_range_value_is_caught(instances):
    with pytest.raises(ParamError, match="invalid value"):
        check({"tavily/max_results": 0}, instances)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_validation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.validation'`

- [ ] **Step 4: Implement `src/search_meta_api/validation.py`**

```python
from __future__ import annotations

from typing import Sequence

from pydantic import ValidationError

from search_meta_api.models import UNIFIED_FIELDS, SearchRequest, UnifiedFilters
from search_meta_api.providers.base import UNIVERSAL_SCOPE, resolve_request
from search_meta_api.router import ProviderInstance


class ParamError(ValueError):
    """A namespaced parameter the service can reject before calling anyone."""


def _flat_is_set(req: SearchRequest, name: str) -> bool:
    default = UnifiedFilters.model_fields[name].get_default(call_default_factory=True)
    return getattr(req, name) != default


def validate_params(req: SearchRequest, instances: Sequence[ProviderInstance]) -> None:
    by_scope = {}
    for instance in instances:
        by_scope[instance.name] = instance.adapter
        by_scope.setdefault(instance.adapter.type_name, instance.adapter)

    for key, value in req.params.items():
        if "/" not in key:
            raise ParamError(
                f"param {key!r} must be scoped as '<scope>/<name>', "
                f"e.g. '*/country' or 'tavily/search_depth'"
            )
        scope, name = key.split("/", 1)

        if scope == UNIVERSAL_SCOPE:
            if name not in UNIFIED_FIELDS:
                raise ParamError(
                    f"'*/{name}' is not a unified field; vendor-specific parameters "
                    f"must be scoped to a provider type or instance"
                )
            if _flat_is_set(req, name):
                raise ParamError(
                    f"{name!r} was given both as a top-level field and as '*/{name}'"
                )
            continue

        adapter = by_scope.get(scope)
        if adapter is None:
            raise ParamError(
                f"unknown scope {scope!r} in param {key!r}; "
                f"known scopes: *, {', '.join(sorted(by_scope))}"
            )
        if name in UNIFIED_FIELDS:
            continue

        declared = adapter.capabilities.passthrough_params.get(name)
        if declared is None:
            raise ParamError(f"{scope!r} has no parameter {name!r}")
        if declared is not object and not isinstance(value, declared):
            raise ParamError(f"param {key!r} must be {declared.__name__}")

    # Dry-run resolution so a bad value surfaces as a 422 rather than as an
    # adapter failure repeated across every provider in the chain.
    for instance in instances:
        try:
            resolve_request(req, instance.name, instance.adapter.type_name,
                            instance.adapter.capabilities)
        except ValidationError as exc:
            raise ParamError(f"invalid value for {instance.name}: {exc}") from exc
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_validation.py -v`
Expected: PASS — 11 passed

- [ ] **Step 6: Write the failing app test**

Create `tests/test_app.py`:

```python
import httpx
import pytest

from search_meta_api.app import create_app
from search_meta_api.config import Settings
from search_meta_api.errors import ErrorClass, ProviderError
from search_meta_api.models import SearchResult
from search_meta_api.providers.base import Adapter, Capabilities
from search_meta_api.router import ProviderInstance

SETTINGS_NO_AUTH = Settings(providers=[{"name": "fake-a", "type": "tavily",
                                        "api_key": "k"}])


class Fake(Adapter):
    type_name = "tavily"
    capabilities = Capabilities(
        supported_filters=frozenset({"country"}),
        max_results_cap=20,
        passthrough_params={"search_depth": str},
    )

    def __init__(self, name, outcome):
        super().__init__(name, client=None)
        self.outcome = outcome

    async def search(self, req, passthrough):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def hit(name, results):
    return ProviderInstance(name, Fake(name, [
        SearchResult(title=t, url=u, snippet="s", rank=i + 1)
        for i, (t, u) in enumerate(results)
    ]))


def fail(name, status, error_class=ErrorClass.TRANSIENT):
    return ProviderInstance(name, Fake(name, ProviderError(name, error_class, status)))


async def call(app, path="/search", json=None, headers=None, method="POST"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, json=json, headers=headers)


async def test_first_success_is_returned():
    app = create_app(SETTINGS_NO_AUTH, instances=[
        fail("a", 500), hit("b", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "b"
    assert body["results"][0]["title"] == "Cats"
    assert [a["outcome"] for a in body["attempts"]] == ["error", "success"]
    assert body["elapsed_ms"] >= 0


async def test_all_empty_is_a_200_with_no_winner():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    response = await call(app, json={"query": "cats"})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] is None
    assert body["results"] == []


async def test_all_failed_is_a_503_carrying_the_attempts():
    app = create_app(SETTINGS_NO_AUTH, instances=[fail("a", 500), fail("b", 401,
                                                                      ErrorClass.AUTH)])
    response = await call(app, json={"query": "cats"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "all_providers_failed"
    assert [a["provider"] for a in detail["attempts"]] == ["a", "b"]


async def test_degraded_filters_are_reported():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    response = await call(app, json={"query": "cats", "language": "fr"})
    body = response.json()
    assert body["degraded"] == ["language"]
    assert body["attempts"][0]["degraded"] == ["language"]


async def test_supported_filter_is_not_degraded():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    body = (await call(app, json={"query": "cats", "country": "fr"})).json()
    assert body["degraded"] == []


async def test_bad_namespaced_param_is_422_and_calls_nobody():
    app = create_app(SETTINGS_NO_AUTH, instances=[fail("a", 500)])
    response = await call(app, json={"query": "cats",
                                     "params": {"tavily/serch_depth": "advanced"}})
    assert response.status_code == 422
    assert "no parameter" in response.json()["detail"]


async def test_missing_query_is_422():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [])])
    assert (await call(app, json={})).status_code == 422


async def test_configured_default_max_results_applies_when_the_caller_omits_it():
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    captured = {}

    class Capturing(Fake):
        async def search(self, req, passthrough):
            captured["max_results"] = req.max_results
            return []

    app = create_app(settings, instances=[ProviderInstance("a", Capturing("a", []))])
    await call(app, json={"query": "cats"})
    assert captured["max_results"] == 3


async def test_explicit_max_results_beats_the_configured_default():
    settings = Settings(defaults={"max_results": 3},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    captured = {}

    class Capturing(Fake):
        async def search(self, req, passthrough):
            captured["max_results"] = req.max_results
            return []

    app = create_app(settings, instances=[ProviderInstance("a", Capturing("a", []))])
    await call(app, json={"query": "cats", "max_results": 7})
    assert captured["max_results"] == 7


async def test_auth_is_enforced_when_a_token_is_configured():
    settings = Settings(server={"auth_token": "secret"},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    app = create_app(settings, instances=[hit("a", [("Cats", "https://a.com")])])
    assert (await call(app, json={"query": "c"})).status_code == 401
    assert (await call(app, json={"query": "c"},
                       headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await call(app, json={"query": "c"},
                       headers={"Authorization": "Bearer secret"})).status_code == 200


async def test_auth_is_disabled_when_no_token_is_configured():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", [("Cats", "https://a.com")])])
    assert (await call(app, json={"query": "c"})).status_code == 200


async def test_health_is_public_and_reports_breaker_state():
    settings = Settings(server={"auth_token": "secret"},
                        providers=[{"name": "a", "type": "tavily", "api_key": "k"}])
    app = create_app(settings, instances=[hit("a", [])])
    response = await call(app, "/health", method="GET")
    assert response.status_code == 200
    assert response.json()["providers"] == [{"name": "a", "state": "closed"}]


async def test_providers_endpoint_lists_order_and_capabilities():
    app = create_app(SETTINGS_NO_AUTH, instances=[hit("a", []), hit("b", [])])
    body = (await call(app, "/providers", method="GET")).json()
    assert [p["name"] for p in body["providers"]] == ["a", "b"]
    assert body["providers"][0]["type"] == "tavily"
    assert "country" in body["providers"][0]["supported_filters"]
```

- [ ] **Step 7: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'search_meta_api.app'`

- [ ] **Step 8: Implement `src/search_meta_api/app.py`**

```python
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Sequence

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

from search_meta_api.breaker import CircuitBreaker
from search_meta_api.config import Settings, load_settings
from search_meta_api.models import SearchRequest, SearchResponse
from search_meta_api.providers.base import resolve_request
from search_meta_api.providers.registry import build_adapter
from search_meta_api.router import ProviderInstance, run_failover
from search_meta_api.validation import ParamError, validate_params

logging.basicConfig(level=logging.INFO)


def require_auth(request: Request) -> None:
    token = request.app.state.settings.server.auth_token
    if token is None:
        return
    if request.headers.get("Authorization") != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    if app.state.instances is None:
        async with httpx.AsyncClient() as client:
            app.state.instances = [
                ProviderInstance(
                    cfg.name,
                    build_adapter(cfg, client, settings.defaults.timeout_ms),
                )
                for cfg in settings.providers
            ]
            yield
    else:
        yield


def create_app(
    settings: Settings, instances: Sequence[ProviderInstance] | None = None
) -> FastAPI:
    app = FastAPI(title="search-meta-api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.instances = list(instances) if instances is not None else None
    app.state.breaker = CircuitBreaker(settings.breaker.to_settings())

    @app.post("/search", response_model=SearchResponse,
              dependencies=[Depends(require_auth)])
    async def search(req: SearchRequest, request: Request) -> SearchResponse:
        state = request.app.state
        # An omitted max_results takes the deployment's configured default;
        # an explicit one always wins.
        if "max_results" not in req.model_fields_set:
            req.max_results = state.settings.defaults.max_results
        try:
            validate_params(req, state.instances)
        except ParamError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def attempt(instance: ProviderInstance):
            adapter = instance.adapter
            effective, passthrough, degraded = resolve_request(
                req, instance.name, adapter.type_name, adapter.capabilities
            )
            degraded = sorted(set(degraded) | set(adapter.extra_degraded(effective)))
            return await adapter.search(effective, passthrough), degraded

        outcome = await run_failover(
            state.instances, attempt, state.breaker,
            deadline_ms=state.settings.server.request_deadline_ms,
        )

        if outcome.winner is None and not outcome.any_answer:
            raise HTTPException(status_code=503, detail={
                "error": "all_providers_failed",
                "query": req.query,
                "attempts": [a.model_dump() for a in outcome.attempts],
                "elapsed_ms": outcome.elapsed_ms,
            })

        return SearchResponse(
            query=req.query,
            provider=outcome.winner,
            results=outcome.items,
            degraded=outcome.degraded,
            attempts=outcome.attempts,
            elapsed_ms=outcome.elapsed_ms,
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        state = request.app.state
        return {
            "status": "ok",
            "providers": [
                {"name": i.name, "state": str(state.breaker.state(i.name))}
                for i in state.instances
            ],
        }

    @app.get("/providers", dependencies=[Depends(require_auth)])
    async def providers(request: Request) -> dict[str, Any]:
        state = request.app.state
        return {
            "providers": [
                {
                    "name": i.name,
                    "type": i.adapter.type_name,
                    "state": str(state.breaker.state(i.name)),
                    "supported_filters": sorted(i.adapter.capabilities.supported_filters),
                    "max_results_cap": i.adapter.capabilities.max_results_cap,
                    "passthrough_params": sorted(
                        i.adapter.capabilities.passthrough_params),
                }
                for i in state.instances
            ]
        }

    return app


def build() -> FastAPI:
    """Entry point for uvicorn: `uvicorn search_meta_api.app:build --factory`."""
    return create_app(load_settings(os.environ.get("CONFIG_PATH", "config.yaml")))
```

- [ ] **Step 9: Run the whole suite**

Run: `.venv/bin/pytest -v`
Expected: PASS — everything green.

- [ ] **Step 10: Commit**

```bash
git add src/search_meta_api/validation.py src/search_meta_api/app.py \
        src/search_meta_api/models.py tests/test_validation.py tests/test_app.py \
        tests/test_models.py
git commit -m "feat: edge parameter validation and FastAPI application"
```

---

### Task 16: Containerise and expose on the home server

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`

**Interfaces:**
- Consumes: `search_meta_api.app:build` (Task 15).
- Produces: a running container reachable at `https://search.local.alban.me`.

`config.yaml` is **bind-mounted, not copied into the image**, so rotating a key is a restart rather than a rebuild — and so keys never end up in an image layer.

- [ ] **Step 1: Create `.dockerignore`**

```
.venv/
.git/
.pytest_cache/
.ruff_cache/
__pycache__/
config.yaml
docs/
tests/
```

- [ ] **Step 2: Create `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

EXPOSE 8000
ENV CONFIG_PATH=/app/config.yaml

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
  sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "search_meta_api.app:build", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 3: Build and run against a real config**

```bash
cp config.example.yaml config.yaml   # then fill in at least one real key
docker build -t search-meta-api .
docker run --rm -p 8000:8000 -v "$PWD/config.yaml:/app/config.yaml:ro" search-meta-api
```

Verify in a second shell:

```bash
curl -s localhost:8000/health | python -m json.tool
```

Expected: `{"status": "ok", "providers": [...]}` with every provider `"closed"`.

- [ ] **Step 4: Verify a bad config refuses to boot**

```bash
printf 'providers:\n  - {name: x, type: altavista, api_key: k}\n' > /tmp/bad.yaml
docker run --rm -v /tmp/bad.yaml:/app/config.yaml:ro search-meta-api
```

Expected: container exits non-zero with `unknown provider type 'altavista'`. A misconfigured provider must never boot into a silently shortened chain.

- [ ] **Step 5: Write `docker-compose.yml` and wire up the edge**

**Use the `exposing-service-on-local-alban-me` skill for this step.** It owns the label schema, the `edge` network requirement, and the landing-page card — do not hand-write those labels from memory. The service-specific parts to feed it:

- service name: `search-meta-api`
- subdomain: `search.local.alban.me`
- internal port: `8000`
- volume: `./config.yaml:/app/config.yaml:ro`
- `restart: unless-stopped`
- no scheduled jobs, so the host systemd-timer guidance does not apply here

- [ ] **Step 6: Bring it up and verify through the proxy**

```bash
docker compose up -d
curl -s https://search.local.alban.me/health | python -m json.tool
curl -s -X POST https://search.local.alban.me/search \
  -H 'Authorization: Bearer <your token>' \
  -H 'Content-Type: application/json' \
  -d '{"query":"claude opus 5 pricing","max_results":3}' | python -m json.tool
```

Expected: a `200` with `provider`, `results`, and an `attempts` array naming which provider answered.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore
git commit -m "feat: containerise and expose at search.local.alban.me"
```

---

### Task 17: Live smoke script and README

**Files:**
- Create: `scripts/smoke.py`, `README.md`

**Interfaces:**
- Consumes: `load_settings`, `build_adapter` (Tasks 7, 15).
- Produces: an opt-in script that calls every configured provider for real.

This is the only thing in the repo that touches the network. It is **not** part of the test suite and must never run in CI — it spends credits. Its job is to answer "is this key still good, and has this vendor changed its response shape".

- [ ] **Step 1: Create `scripts/smoke.py`**

```python
"""Call every configured provider once, for real. Spends credits.

Usage: python scripts/smoke.py [query]
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx

from search_meta_api.config import load_settings
from search_meta_api.models import SearchRequest
from search_meta_api.providers.base import resolve_request
from search_meta_api.providers.registry import build_adapter


async def main() -> int:
    query = " ".join(sys.argv[1:]) or "claude opus 5 pricing"
    settings = load_settings(os.environ.get("CONFIG_PATH", "config.yaml"))
    request = SearchRequest(query=query, max_results=3)
    failures = 0

    async with httpx.AsyncClient() as client:
        for cfg in settings.providers:
            adapter = build_adapter(cfg, client, settings.defaults.timeout_ms)
            effective, passthrough, degraded = resolve_request(
                request, cfg.name, cfg.type, adapter.capabilities
            )
            try:
                results = await adapter.search(effective, passthrough)
            except Exception as exc:  # noqa: BLE001 — the point is to report anything
                failures += 1
                print(f"FAIL  {cfg.name:<12} {exc}")
                continue
            note = f" degraded={degraded}" if degraded else ""
            print(f"ok    {cfg.name:<12} {len(results)} results{note}")
            for result in results[:2]:
                print(f"        {result.title[:60]} — {result.url}")

    print(f"\n{len(settings.providers) - failures}/{len(settings.providers)} providers healthy")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Run it against real keys**

Run: `.venv/bin/python scripts/smoke.py "claude opus 5 pricing"`
Expected: one line per provider. Any `FAIL` line names a dead key or a changed response shape — investigate before deploying. A provider with no key configured is expected to fail here.

- [ ] **Step 3: Write `README.md`**

Cover, in this order:

1. **What it is** — one paragraph: ordered failover across search vendors, first non-empty result set wins, `attempts` explains what happened.
2. **Quick start** — `cp config.example.yaml config.yaml`, add keys, `docker compose up -d`.
3. **Request examples** — a minimal `{"query": "..."}` and one exercising `params` with all three scopes.
4. **The `params` scoping rules** — `*` / `<type>` / `<instance>`, precedence, and the distinction that `*/` filters are best-effort while scoped ones are literal passthrough.
5. **Capability table** — which unified filters each provider honours, copied from the design spec §6, with a note that anything unhonoured appears in `degraded`.
6. **Failover semantics** — the success definition, the error classes, breaker thresholds and cooldowns.
7. **Adding a provider** — write the adapter, add two lines to `registry.py`, add a line to `config.example.yaml`, write the fixture tests.
8. **Note that Bing is excluded** and why, so nobody adds it back.

- [ ] **Step 4: Run the full suite one last time**

Run: `.venv/bin/pytest -v && .venv/bin/ruff check src tests`
Expected: all tests pass, no lint errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/smoke.py README.md
git commit -m "docs: readme and live provider smoke script"
```

---

## Done when

- `pytest` is green with no network access in any test.
- `scripts/smoke.py` reports every provider you hold a key for as healthy.
- `https://search.local.alban.me/search` returns results, and killing a provider's key makes the `attempts` array show it failing over to the next.
- `/docs` renders a tool schema an agent can call directly.
