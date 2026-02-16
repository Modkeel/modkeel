"""Global constants for ModForge."""

MODFORGE_VERSION = "1.0.0"
MODRINTH_USER_AGENT = f"ModForge/{MODFORGE_VERSION} (github.com/juanzab/ModForge)"

# Crowdsource API (Supabase Edge Function)
MODFORGE_API_URL = "https://pcczpdyhytvzqcnmddfv.supabase.co/functions/v1/submit-report"

# HMAC key for report signing. This is NOT a secret -- it's embedded in the
# CLI to raise the bar for casual API abuse. The real anti-spam protection
# comes from statistical consensus, rate limiting, and reputation scoring.
MODFORGE_HMAC_KEY = b"modforge-crowdsource-v1-hmac-signing-key"
