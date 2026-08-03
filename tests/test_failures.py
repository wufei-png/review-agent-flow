from urllib.error import HTTPError, URLError

from review_agent_flow.failures import classify_exception
from review_agent_flow.repository import ShaFetchError, ShaUnavailableError


def test_http_4xx_is_terminal_gitlab_api_failure() -> None:
    for status in (401, 404):
        failure = classify_exception(HTTPError("https://gitlab.example", status, "failed", {}, None))
        assert failure.failure_class == "gitlab_api"
        assert failure.retryable is False


def test_http_429_is_retryable_rate_limit() -> None:
    failure = classify_exception(HTTPError("https://gitlab.example", 429, "limited", {}, None))
    assert failure.failure_class == "gitlab_rate_limited"
    assert failure.retryable is True


def test_http_5xx_is_retryable_server_failure() -> None:
    failure = classify_exception(HTTPError("https://gitlab.example", 503, "unavailable", {}, None))
    assert failure.failure_class == "gitlab_server_error"
    assert failure.retryable is True


def test_urLError_without_http_status_is_retryable_network_failure() -> None:
    failure = classify_exception(URLError("temporary DNS failure"))
    assert failure.failure_class == "gitlab_network"
    assert failure.retryable is True


def test_temporary_sha_fetch_failure_is_retryable_but_missing_sha_is_terminal() -> None:
    transient = classify_exception(ShaFetchError("git fetch timed out"))
    assert transient.failure_class == "repository_fetch"
    assert transient.retryable is True

    permanent = classify_exception(ShaUnavailableError("commit is unavailable"))
    assert permanent.failure_class == "repository_sha_unavailable"
    assert permanent.retryable is False
