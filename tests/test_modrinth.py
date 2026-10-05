"""ModrinthClient.check_modrinth: "not on Modrinth" must stay distinguishable from
"Modrinth could not be asked" (network, HTTP error, timeout), via ModrinthClient.last_error."""

from unittest.mock import MagicMock, patch

import requests

from modkeel.models import ModCompilerConfig
from modkeel.modrinth import ModrinthClient


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
