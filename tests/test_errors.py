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
