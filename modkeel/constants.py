"""Global constants for Modkeel."""

import os
from pathlib import Path

# config, caches and downloads; the MODKEEL_HOME environment variable moves it
MODKEEL_HOME = Path(os.environ.get("MODKEEL_HOME", Path.home() / ".modkeel"))

MODKEEL_VERSION = "0.1.7"
MODRINTH_USER_AGENT = f"Modkeel/{MODKEEL_VERSION} (github.com/Modkeel/modkeel)"

# "Sign in with GitHub" (modkeel/ghauth.py, OAuth device flow). A client id is public by
# design: it names the app on GitHub's consent page. There is no client secret anywhere.
GITHUB_CLIENT_ID = "Ov23liLvi9KLQbbssGgH"

# Crowdsource API. Empty = sharing off: no first-run prompt, no reports sent.
# The old Supabase endpoint is gone; the Reports API will fill this in.
MODKEEL_API_URL = ""

# HMAC key for report signing. This is NOT a secret -- it's embedded in the
# CLI to raise the bar for casual API abuse. The real anti-spam protection
# comes from statistical consensus, rate limiting, and reputation scoring.
MODKEEL_HMAC_KEY = b"modforge-crowdsource-v1-hmac-signing-key"
