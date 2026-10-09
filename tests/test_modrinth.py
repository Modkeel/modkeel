"""ModrinthClient.check_modrinth: "not on Modrinth" must stay distinguishable from
"Modrinth could not be asked" (network, HTTP error, timeout), via ModrinthClient.last_error.
modrinth_call: Modrinth's rate limit (429, X-Ratelimit-*) is waited out, never read as a miss."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from requests.structures import CaseInsensitiveDict

import modkeel.modrinth as modrinth_module
from modkeel.models import ModCompilerConfig
from modkeel.modrinth import ModrinthClient, modrinth_call


def make_client() -> ModrinthClient:
    return ModrinthClient(ModCompilerConfig(mc_version="1.21.10", loader="neoforge",
                                            loader_version="64"))


def response(status: int, payload) -> MagicMock:
    resp = MagicMock(status_code=status)
    resp.json.return_value = payload
    return resp


HIT = {"slug": "continuity", "title": "Continuity", "downloads": 10}
VERSION = {"version_number": "3.0.0", "version_type": "release", "dependencies": [],
           "files": [{"primary": True, "url": "https://cdn/x.jar", "filename": "x.jar",
                      "size": 1024}]}


@patch("modkeel.modrinth.requests.get")
def test_no_hits_is_not_an_error(get):
    get.return_value = response(200, {"hits": []})
    client = make_client()
    assert client.check_modrinth("Continuity") is None
    assert client.last_error is None


@patch("modkeel.modrinth.requests.get")
def test_connection_error_sets_last_error(get):
    get.side_effect = requests.exceptions.ConnectionError("Tunnel connection failed: 403")
    client = make_client()
    assert client.check_modrinth("Continuity") is None
    assert client.last_error == "could not connect (network, proxy or DNS)"


@patch("modkeel.modrinth.requests.get")
def test_timeout_sets_last_error(get):
    get.side_effect = requests.exceptions.Timeout()
    client = make_client()
    assert client.check_modrinth("Continuity") is None
    assert client.last_error == "timed out"


@patch("modkeel.modrinth.requests.get")
def test_http_error_on_search_sets_last_error(get):
    get.return_value = response(503, {})
    client = make_client()
    assert client.check_modrinth("Continuity") is None
    assert client.last_error == "HTTP 503"


@patch("modkeel.modrinth.requests.get")
def test_http_error_on_version_lookup_sets_last_error(get):
    get.side_effect = [response(200, {"hits": [HIT]}), response(500, [])]
    client = make_client()
    assert client.check_modrinth("Continuity") is None
    assert client.last_error == "HTTP 500"


@patch("modkeel.modrinth.requests.get")
def test_no_version_for_target_is_not_an_error(get):
    get.side_effect = [response(200, {"hits": [HIT]}), response(200, [])]
    client = make_client()
    assert client.check_modrinth("Continuity") is None
    assert client.last_error is None


@patch("modkeel.modrinth.requests.get")
def test_success_clears_previous_error(get):
    client = make_client()
    get.side_effect = requests.exceptions.Timeout()
    client.check_modrinth("Continuity")
    get.side_effect = [response(200, {"hits": [HIT]}), response(200, [VERSION])]
    result = client.check_modrinth("Continuity")
    assert result is not None and result["slug"] == "continuity"
    assert client.last_error is None


# ── Rate limit (modrinth_call) ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def open_window():
    """Each test starts with Modrinth's window open (the pause is module state)."""
    modrinth_module._paused_until = 0.0
    yield
    modrinth_module._paused_until = 0.0


class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    def __call__(self):
        return self.t


def limited(status: int, **headers) -> MagicMock:
    resp = response(status, {})
    resp.headers = CaseInsensitiveDict({k.replace("_", "-"): v for k, v in headers.items()})
    return resp


class TestRateLimit:
    def call(self, send, clock):
        return modrinth_call(send, "https://api.modrinth.com/v2/search", sleep=clock.sleep,
                             clock=clock, timeout=5)

    def test_a_429_waits_for_the_window_then_retries(self):
        clock = Clock()
        send = MagicMock(side_effect=[limited(429, X_Ratelimit_Reset="3"),
                                      limited(200, X_Ratelimit_Remaining="299")])
        assert self.call(send, clock).status_code == 200
        assert clock.slept == [3.0] and send.call_count == 2
        send.assert_called_with("https://api.modrinth.com/v2/search", timeout=5)

    def test_none_left_pauses_the_next_call_not_this_one(self):
        clock = Clock()
        send = MagicMock(return_value=limited(200, X_Ratelimit_Remaining="0",
                                              X_Ratelimit_Reset="5"))
        assert self.call(send, clock).status_code == 200 and clock.slept == []
        self.call(send, clock)
        assert clock.slept == [5.0]

    def test_gives_up_after_the_retries_and_caps_each_wait(self):
        clock = Clock()
        send = MagicMock(return_value=limited(429, X_Ratelimit_Reset="600"))
        assert self.call(send, clock).status_code == 429
        assert send.call_count == modrinth_module.RATE_LIMIT_RETRIES + 1
        assert clock.slept == [modrinth_module.RATE_LIMIT_MAX_WAIT] * modrinth_module.RATE_LIMIT_RETRIES

    def test_a_429_without_reset_uses_retry_after_or_a_default(self):
        clock = Clock()
        send = MagicMock(side_effect=[limited(429, Retry_After="2"), limited(429), limited(200)])
        self.call(send, clock)
        assert clock.slept == [2.0, 10.0]

    def test_a_429_with_a_reset_of_0_still_waits_a_second(self):
        clock = Clock()
        send = MagicMock(side_effect=[limited(429, X_Ratelimit_Reset="0"), limited(200)])
        self.call(send, clock)
        assert clock.slept == [1.0]

    def test_answers_without_rate_headers_never_wait(self):
        clock = Clock()
        send = MagicMock(return_value=response(200, {}))     # a MagicMock's headers
        self.call(send, clock)
        self.call(send, clock)
        assert clock.slept == []

    @patch("modkeel.modrinth.requests.get")
    def test_a_429_no_longer_reads_as_not_on_modrinth(self, get):
        search = limited(200)
        search.json.return_value = {"hits": [HIT]}
        versions = limited(200)
        versions.json.return_value = [VERSION]
        get.side_effect = [limited(429, X_Ratelimit_Reset="1"), search, versions]
        clock, client = Clock(), make_client()
        with patch("modkeel.modrinth.time.sleep", clock.sleep), \
                patch("modkeel.modrinth.time.monotonic", clock):
            found = client.check_modrinth("Continuity")
        assert found and client.last_error is None
        assert clock.slept == [1.0]
