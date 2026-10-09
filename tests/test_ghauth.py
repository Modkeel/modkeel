"""Sign in with GitHub (modkeel/ghauth.py, the OAuth device flow) and where it is offered:
the device flow against a scripted GitHub, the token saved, the engine's SIGN_IN answer,
the protocol (sign_in method, github query, the saved token used) and `modkeel login`.

GitHub is a double that answers each POST from a script; time never passes for real.
"""

from unittest.mock import patch

import pytest

from modkeel.config import ModkeelConfig
from modkeel.core.decisions import SIGN_IN, Cancelled, NeedToken, SignIn, TokenAsker
from modkeel.core.events import GitHubCode, Message
from modkeel.ghauth import DEVICE_CODE_URL, TOKEN_URL, SignInError, sign_in

START = {"device_code": "dev123", "user_code": "ABCD-1234",
         "verification_uri": "https://github.com/login/device", "expires_in": 900,
         "interval": 5}


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeGitHub:
    """POSTs answered in order from `polls` after the device code; GET /user gives a login."""

    def __init__(self, polls, start=START, user="juan"):
        self.start, self.polls, self.user = start, list(polls), user
        self.posted = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posted.append((url, dict(data)))
        if url == DEVICE_CODE_URL:
            return Resp(self.start)
        assert url == TOKEN_URL and data["device_code"] == "dev123"
        return Resp(self.polls.pop(0))

    def get(self, url, headers=None, timeout=None):
        assert headers["Authorization"].startswith("Bearer ")
        return Resp({"login": self.user})


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


def run(github, **kw):
    clock, events = Clock(), []
    result = sign_in(events.append, http=github, sleep=clock.sleep, clock=clock, **kw)
    return result, events, clock


class TestDeviceFlow:
    def test_pending_then_slow_down_then_the_token_saved(self):
        github = FakeGitHub([{"error": "authorization_pending"},
                             {"error": "slow_down", "interval": 10},
                             {"access_token": "gho_x", "token_type": "bearer"}])
        result, events, clock = run(github)
        assert (result.token, result.user) == ("gho_x", "juan")
        assert events == [GitHubCode("ABCD-1234", "https://github.com/login/device", 900)]
        assert clock.t == pytest.approx(5 + 5 + 10)       # slow_down raised the interval
        assert ModkeelConfig().github_token == "gho_x"
        # no scopes, and the public client id only (never a secret)
        assert github.posted[0][1]["scope"] == "" and all(
            "client_secret" not in data for _, data in github.posted)

    @pytest.mark.parametrize("error, says", [("access_denied", "refused"),
                                             ("expired_token", "expired")])
    def test_refused_or_expired(self, error, says):
        with pytest.raises(SignInError, match=says):
            run(FakeGitHub([{"error": error}]))
        assert ModkeelConfig().github_token is None

    def test_the_code_runs_out_while_waiting(self):
        github = FakeGitHub([{"error": "authorization_pending"}] * 400,
                            start={**START, "expires_in": 12})
        with pytest.raises(SignInError, match="expired"):
            run(github)

    def test_github_refuses_to_start(self):
        github = FakeGitHub([], start={"error": "device_flow_disabled"})
        with pytest.raises(SignInError, match="turned off"):
            run(github)

    def test_network_down(self):
        class Down:
            def post(self, *a, **k):
                raise OSError("no route")

        with pytest.raises(SignInError, match="could not reach GitHub"):
            run(Down())

    def test_cancel_stops_the_wait(self):
        with pytest.raises(Cancelled):
            run(FakeGitHub([{"error": "authorization_pending"}] * 10),
                cancelled=lambda: True)


class TestTokenAsker:
    def test_sign_in_answer_signs_in_once(self):
        from modkeel.ghauth import SignedIn

        asked = []
        with patch("modkeel.ghauth.sign_in", return_value=SignedIn("gho_y", "juan")):
            token = TokenAsker(None, lambda q: asked.append(q) or SIGN_IN, lambda e: None)
            assert token() == "gho_y" and token() == "gho_y" and token.current == "gho_y"
        assert asked == [NeedToken("forks")]

    def test_a_failed_sign_in_skips_forks_and_says_why(self):
        events = []
        with patch("modkeel.ghauth.sign_in", side_effect=SignInError("access was refused")):
            token = TokenAsker(None, lambda q: SIGN_IN, events.append)
            assert token() is None
        assert isinstance(events[0], Message) and "access was refused" in events[0].text

    def test_a_known_token_is_never_asked_for(self):
        token = TokenAsker("ghp_z", lambda q: pytest.fail("asked"), lambda e: None)
        assert token() == "ghp_z"


class TestProtocol:
    def test_need_token_takes_a_sign_in_answer(self):
        from modkeel.core.wire import accepted_answer

        q = NeedToken("forks")
        assert isinstance(accepted_answer(q, {"sign_in": True}), SignIn)
        assert accepted_answer(q, {"sign_in": True, "open_browser": True}).open_browser
        assert accepted_answer(q, "ghp_a") == "ghp_a"
        assert accepted_answer(q, {"sign_in": "yes"}) is None
        assert accepted_answer(q, None) is None

    def test_github_query_and_the_saved_token_used_by_requests(self):
        from modkeel.core.wire import METHODS, QUERIES

        assert QUERIES["github"]({}) == {"signed_in": False}
        ModkeelConfig().github_token = "gho_saved"
        assert QUERIES["github"]({}) == {"signed_in": True}
        seen = {}

        def fake_get(request, **_):
            seen["token"] = request.github_token
            raise RuntimeError("stop here")

        with patch("modkeel.core.engine.get_mod", fake_get), pytest.raises(RuntimeError):
            METHODS["get"]({"query": "x", "mc_version": "1.21.10", "loader": "fabric"},
                           lambda e: None, lambda q: None, lambda: False)
        assert seen["token"] == "gho_saved"

    def test_sign_in_method_reports_a_refusal_as_a_result(self):
        from modkeel.core.wire import METHODS

        with patch("modkeel.ghauth.sign_in", side_effect=SignInError("access was refused")):
            result = METHODS["sign_in"]({}, lambda e: None, lambda q: None, lambda: False)
        assert result == {"signed_in": False, "user": None, "reason": "access was refused"}


class TestLoginCommand:
    def run(self, *args):
        from typer.testing import CliRunner

        from modkeel.cli import app

        return CliRunner().invoke(app, ["login", "--no-browser", *args])

    def test_shows_the_code_and_who_signed_in(self):
        from modkeel.ghauth import SignedIn

        def fake(events, *a, **k):
            events(GitHubCode("ABCD-1234", "https://github.com/login/device", 900))
            return SignedIn("gho_x", "juan")

        with patch("modkeel.ghauth.sign_in", fake):
            out = self.run()
        assert out.exit_code == 0
        assert "ABCD-1234" in out.output and "Signed in with GitHub as juan" in out.output

    def test_a_failure_exits_1(self):
        with patch("modkeel.ghauth.sign_in", side_effect=SignInError("access was refused")):
            out = self.run()
        assert out.exit_code == 1 and "access was refused" in out.output
