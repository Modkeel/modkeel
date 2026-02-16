"""Crowdsource reporting functions for ModForge."""

import hashlib
import hmac
import json
import logging
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modforge.constants import (
    MODFORGE_API_URL,
    MODFORGE_HMAC_KEY,
    MODFORGE_VERSION,
    MODRINTH_USER_AGENT,
)
from modforge.config import ModForgeConfig
from modforge.models import CompilationResult

logger = logging.getLogger("modforge")


def sign_report(report: Dict) -> str:
    """
    Create HMAC-SHA256 signature for a crowdsource report.

    Uses deterministic JSON serialization (sorted keys, compact
    separators) to ensure the signature is reproducible.
    """
    # Exclude the signature field itself from the payload
    payload = {k: v for k, v in report.items() if k != "signature"}
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    )
    sig = hmac.new(
        MODFORGE_HMAC_KEY, serialized.encode("utf-8"), hashlib.sha256,
    )
    return f"hmac-sha256:{sig.hexdigest()}"


def detect_java_version() -> str:
    """Detect the installed Java version."""
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stderr or result.stdout
        match = re.search(r'"([\d._]+)"', output)
        if match:
            return match.group(1)
        return output.strip().split("\n")[0][:50]
    except Exception:
        return "unknown"


def build_report(
    result: CompilationResult,
    mc_version: str,
    loader: str,
    loader_version: str,
    parse_repo_url_fn,
) -> Optional[Dict]:
    """
    Build a crowdsource report from a CompilationResult.

    Returns None if the result should not be reported (failed
    compilation, client-only, or inconclusive).
    """
    if not result.success:
        return None

    # Determine status
    if result.docker_tested and result.docker_test_passed is True:
        status = "works"
    elif result.docker_tested and result.docker_test_passed is False:
        status = "fails"
    elif (result.docker_tested
          and result.docker_test_passed is None):
        return None  # client-only or inconclusive
    else:
        status = "compiled"

    # Compute JAR hash
    jar_hash = ""
    if result.jar_path and Path(result.jar_path).exists():
        h = hashlib.sha256()
        with open(result.jar_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        jar_hash = h.hexdigest()

    # Source repo info
    source_repo = result.repo_url
    try:
        owner, repo, _ = parse_repo_url_fn(result.repo_url)
        source_repo = f"{owner}/{repo}"
    except Exception:
        pass

    return {
        "mod_name": result.mod_name or "unknown",
        "mod_version": result.mod_version or "unknown",
        "jar_hash_sha256": jar_hash,
        "source_repo": source_repo,
        "source_branch": result.branch or "",
        "mc_version": mc_version,
        "loader": loader,
        "loader_version": loader_version,
        "status": status,
        "log_snippet": (result.docker_error or "")[:500],
        "load_time_ms": result.docker_load_time_ms,
        "java_version": detect_java_version(),
        "os": platform.system().lower(),
        "os_version": platform.release(),
        "is_cross_loader": result.is_cross_loader,
        "modrinth_download": result.modrinth_download,
        "cli_version": MODFORGE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def submit_reports(
    results: List[CompilationResult],
    modforge_config: ModForgeConfig,
    mc_version: str,
    loader: str,
    loader_version: str,
    parse_repo_url_fn,
) -> None:
    """
    Submit crowdsource reports for all successful results.

    Respects the user's sharing preference. Silent failure on
    network errors -- never blocks or slows down the CLI.
    """
    if modforge_config.sharing == "never":
        return

    if not MODFORGE_API_URL:
        logger.debug("No API URL configured, skipping reports")
        return

    reports = []
    for result in results:
        report = build_report(result, mc_version, loader, loader_version,
                              parse_repo_url_fn)
        if report:
            reports.append(report)

    if not reports:
        return

    # If "ask", prompt user
    if modforge_config.sharing == "ask":
        print(
            f"\n\U0001f4ca Share {len(reports)} anonymous compatibility "
            f"report(s) with the community? [Y/n] ",
            end="",
        )
        try:
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer not in ("", "y", "yes"):
            print("  Skipped.")
            return

    # Submit each report
    client_id = modforge_config.client_id
    submitted = 0
    for report in reports:
        report["client_id"] = client_id
        report["signature"] = sign_report(report)

        try:
            resp = requests.post(
                MODFORGE_API_URL,
                json=report,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": MODRINTH_USER_AGENT,
                },
                timeout=10,
            )
            if resp.status_code in (200, 201):
                submitted += 1
            else:
                logger.debug(
                    "Report submission failed (%d): %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as e:
            logger.debug("Report submission error: %s", e)

    if submitted:
        print(
            f"  \U0001f4ca Shared {submitted}/{len(reports)} "
            f"report(s). Thank you!"
        )
