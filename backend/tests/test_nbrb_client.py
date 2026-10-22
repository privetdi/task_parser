from datetime import date

import httpx
import pytest

from app.sources.nbrb import BASE_URL, NbrbClient, NbrbError, RatePoint
from tests.helpers import load_fixture


def make_client(handler, **kwargs) -> tuple[NbrbClient, list[float]]:
    """Клиент на подменённом транспорте; паузы не ждём, а записываем."""
    sleeps: list[float] = []
    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return NbrbClient(http, sleep=sleeps.append, **kwargs), sleeps


def test_dynamics_parses_real_response_and_sends_period() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("dynamics_usd_431_recent.json"))

    client, _ = make_client(handler)
    points = client.dynamics(431, date(2026, 10, 18), date(2026, 10, 23))

    assert points[0] == RatePoint(date(2026, 10, 18), 3.0091)
    assert points[-1] == RatePoint(date(2026, 10, 22), 2.9713)
    assert seen[0].url.path == "/exrates/rates/dynamics/431"
    assert seen[0].url.params["startdate"] == "2026-10-18"
    assert seen[0].url.params["enddate"] == "2026-10-23"


def test_monthly_currency_returns_only_dates_when_rate_was_set() -> None:
    client, _ = make_client(
        lambda request: httpx.Response(200, json=load_fixture("dynamics_azn_507_monthly.json"))
    )
    points = client.dynamics(507, date(2026, 5, 1), date(2026, 10, 22))
    dates = [p.rate_date for p in points]
    assert dates[:3] == [date(2026, 5, 4), date(2026, 6, 1), date(2026, 7, 1)]


def test_empty_response_is_not_an_error() -> None:
    client, _ = make_client(lambda request: httpx.Response(200, json=[]))
    assert client.dynamics(431, date(2026, 10, 23), date(2026, 10, 23)) == []


def test_window_longer_than_365_days_is_rejected_before_request() -> None:
    called = []
    client, _ = make_client(lambda request: called.append(request) or httpx.Response(200, json=[]))

    with pytest.raises(ValueError, match="365"):
        client.dynamics(431, date(2024, 1, 1), date(2026, 1, 1))
    assert called == []


def test_window_of_exactly_365_days_is_allowed() -> None:
    client, _ = make_client(lambda request: httpx.Response(200, json=[]))
    client.dynamics(431, date(2025, 10, 22), date(2026, 10, 21))  # ровно 365 дней


def test_end_before_start_is_rejected() -> None:
    client, _ = make_client(lambda request: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        client.dynamics(431, date(2026, 10, 22), date(2026, 10, 21))


def test_server_error_is_retried_with_growing_pause_then_succeeds() -> None:
    answers = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, json=[])])
    client, sleeps = make_client(lambda request: next(answers), retry_delay=2.0)

    assert client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2)) == []
    assert sleeps == [2.0, 4.0]


def test_network_error_is_retried() -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("таймаут")
        return httpx.Response(200, json=[])

    client, _ = make_client(handler)
    client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2))
    assert len(attempts) == 2


def test_persistent_failure_raises_error_with_currency_and_period() -> None:
    client, _ = make_client(lambda request: httpx.Response(500), retries=3)

    with pytest.raises(NbrbError) as exc:
        client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2))

    message = str(exc.value)
    assert "Cur_ID=431" in message and "2026-10-01..2026-10-02" in message
    assert "HTTP 500" in message and "3" in message


def test_client_error_is_not_retried() -> None:
    attempts = []
    client, _ = make_client(lambda request: attempts.append(1) or httpx.Response(404))

    with pytest.raises(NbrbError, match="HTTP 404"):
        client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2))
    assert len(attempts) == 1


def test_non_list_response_is_an_error() -> None:
    client, _ = make_client(lambda request: httpx.Response(200, json={"message": "oops"}))
    with pytest.raises(NbrbError, match="ожидался список"):
        client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2))


def test_unexpected_item_format_is_an_nbrb_error_not_a_crash() -> None:
    client, _ = make_client(lambda request: httpx.Response(200, json=[{"Date": "2026-10-01"}]))
    with pytest.raises(NbrbError, match="Cur_ID=431.*формат"):
        client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2))


def test_request_delay_is_applied_before_each_request() -> None:
    client, sleeps = make_client(lambda request: httpx.Response(200, json=[]), request_delay=0.5)
    client.dynamics(431, date(2026, 10, 1), date(2026, 10, 2))
    client.dynamics(431, date(2026, 10, 3), date(2026, 10, 4))
    assert sleeps == [0.5, 0.5]


def test_currencies_are_parsed_with_versions_and_periodicity() -> None:
    catalog = load_fixture("currencies.json")
    client, _ = make_client(lambda request: httpx.Response(200, json=catalog))
    by_id = {c.cur_id: c for c in client.currencies()}

    assert by_id[145].parent_id == by_id[431].parent_id == 145
    assert by_id[145].date_end == date(2021, 7, 8)
    assert by_id[431].date_start == date(2021, 7, 9)
    assert by_id[507].periodicity == 1 and by_id[431].periodicity == 0
    assert by_id[431].scale == 1
