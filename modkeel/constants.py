"""Global constants for Modkeel."""

import os
from pathlib import Path

# config, caches and downloads; the MODKEEL_HOME environment variable moves it
MODKEEL_HOME = Path(os.environ.get("MODKEEL_HOME", Path.home() / ".modkeel"))

MODKEEL_VERSION = "1.0.0"
MODRINTH_USER_AGENT = f"Modkeel/{MODKEEL_VERSION} (github.com/Modkeel/modkeel)"

# Crowdsource API (Supabase Edge Function)
MODKEEL_API_URL = "https://pcczpdyhytvzqcnmddfv.supabase.co/functions/v1/submit-report"

# HMAC key for report signing. This is NOT a secret -- it's embedded in the
# CLI to raise the bar for casual API abuse. The real anti-spam protection
# comes from statistical consensus, rate limiting, and reputation scoring.
MODKEEL_HMAC_KEY = b"modforge-crowdsource-v1-hmac-signing-key"
