"""Sign in with GitHub: a token for the player without making one by hand.

    sign_in(events) -> SignedIn(token, user)

GitHub's OAuth device flow for the Modkeel OAuth App (constants.GITHUB_CLIENT_ID):

1. Ask GitHub for a code (POST /login/device/code). It returns a short user code, the page
   to enter it on (github.com/login/device), how long it lives and how often to poll.
2. Show both (GitHubCode event: the terminal prints it, the app shows it with a button).
3. Poll POST /login/oauth/access_token until the player approves, refuses, or the code
   expires; "slow_down" adds 5 seconds to the interval, as GitHub asks.

No scopes are requested: the token can read public data only (forks, branches, releases),
under the player's own 5000 requests/hour instead of the 60/hour GitHub gives anonymous
calls. Only the client id ships with Modkeel; it is public by design, and the device flow
needs no client secret. The token is saved like `modkeel token --set` does (config.toml);
the player revokes it in GitHub's settings (Applications -> Authorized OAuth Apps).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from modkeel.constants import GITHUB_CLIENT_ID, MODRINTH_USER_AGENT
from modkeel.core.decisions import Cancel, check_cancel
from modkeel.core.events import Emitter, GitHubCode
from modkeel.core.text import print_event

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# GitHub's error codes that end the sign-in, as the player should read them
_FINAL = {
    "expired_token": "the code expired before it was entered; try again",
    "access_denied": "access was refused on GitHub",
    "device_flow_disabled": "GitHub sign-in is turned off for Modkeel",
    "incorrect_client_credentials": "GitHub does not know this Modkeel build",
    "incorrect_device_code": "GitHub rejected the code; try again",
    "unsupported_grant_type": "GitHub rejected the sign-in request",
}


class SignInError(Exception):
    """The sign-in did not give a token; the message says why, for the player."""


@dataclass
class SignedIn:
    token: str
    user: Optional[str] = None            # the GitHub login, when it could be read


def _http():
    import requests

    return requests


def _post(http, url: str, data: dict) -> dict:
    try:
        resp = http.post(url, data=data, timeout=20,
                         headers={"Accept": "application/json",
                                  "User-Agent": MODRINTH_USER_AGENT})
        body = resp.json()
    except Exception as e:  # network down, not JSON: the player can retry
        raise SignInError(f"could not reach GitHub ({e.__class__.__name__})") from e
    if not isinstance(body, dict):
        raise SignInError("unexpected answer from GitHub")
    return body


def github_user(token: str, http=None) -> Optional[str]:
    """The login the token belongs to, or None if it cannot be read."""
    try:
        resp = (http or _http()).get(USER_URL, timeout=15, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "User-Agent": MODRINTH_USER_AGENT})
        return resp.json().get("login") if resp.status_code == 200 else None
    except Exception:
        return None


def sign_in(events: Emitter = print_event, cancelled: Optional[Cancel] = None,
            http=None, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic, save: bool = True,
            open_browser: bool = False) -> SignedIn:
    """Run the device flow (see the module docstring); raises SignInError or Cancelled."""
    http = http or _http()
    start = _post(http, DEVICE_CODE_URL, {"client_id": GITHUB_CLIENT_ID, "scope": ""})
    if "device_code" not in start:
        reason = start.get("error")
        raise SignInError(_FINAL.get(reason, f"GitHub refused to start the sign-in ({reason})"))
    interval = float(start.get("interval") or 5)
    expires_in = int(start.get("expires_in") or 900)
    deadline = clock() + expires_in
    url = start.get("verification_uri") or "https://github.com/login/device"
    events(GitHubCode(start["user_code"], url, expires_in))
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:   # no browser: the page and code are shown anyway
            pass

    while True:
        waited = 0.0                       # in short steps, so a cancel is seen at once
        while waited < interval:
            check_cancel(cancelled)
            sleep(min(0.5, interval - waited))
            waited += 0.5
        if clock() > deadline:
            raise SignInError(_FINAL["expired_token"])
        answer = _post(http, TOKEN_URL, {"client_id": GITHUB_CLIENT_ID,
                                         "device_code": start["device_code"],
                                         "grant_type": GRANT})
        token = answer.get("access_token")
        if token:
            break
        error = answer.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = float(answer.get("interval") or interval + 5)
            continue
        raise SignInError(_FINAL.get(error, f"GitHub ended the sign-in ({error})"))

    if save:
        from modkeel.config import ModkeelConfig

        ModkeelConfig().github_token = token
    return SignedIn(token, github_user(token, http))
