"""Utility functions for Modkeel."""

import logging
import os
import platform
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import List, Optional, Tuple


logger = logging.getLogger("modkeel")


def setup_logging(log_file: Optional[str] = None) -> None:
    """Configure logging with stdout and optional file output."""
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (always)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        print(f"Logging to file: {log_file}")


def setup_windows_console():
    """Setup console for emoji support on Windows."""
    if platform.system() == 'Windows':
        # Try to enable UTF-8 output
        try:
            import codecs
            sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
            sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')
        except Exception:
            # If UTF-8 doesn't work, replace print with safe version
            import builtins
            original_print = builtins.print

            def safe_print(*args, **kwargs):
                try:
                    original_print(*args, **kwargs)
                except UnicodeEncodeError:
                    # Replace emojis with ASCII
                    safe_args = []
                    for arg in args:
                        if isinstance(arg, str):
                            replacements = {
                                '\U0001f4e6': '[PKG]', '\U0001f50d': '[SEARCH]',
                                '\u2705': '[OK]', '\u274c': '[FAIL]',
                                '\u26a0\ufe0f': '[WARN]', '\U0001f33f': '[BRANCH]',
                                '\U0001f4e5': '[DOWN]', '\U0001f528': '[BUILD]',
                                '\U0001f4cb': '[LIST]', '\U0001f3af': '[TARGET]',
                                '\u2b50': '*', '\U0001f374': '[FORK]',
                                '\U0001f512': '[LOCK]', '\U0001f6a8': '[ALERT]',
                                '\U0001f4ca': '[STAT]', '\U0001f4cd': '[LOC]',
                                '\U0001f4be': '[SAVE]', '\U0001f50e': '[FIND]',
                            }
                            for emoji, ascii_rep in replacements.items():
                                arg = arg.replace(emoji, ascii_rep)
                            safe_args.append(arg)
                        else:
                            safe_args.append(arg)
                    original_print(*safe_args, **kwargs)

            builtins.print = safe_print


# ── Fuzzy Matching ────────────────────────────────────────────────────────


def _normalize(s: str) -> str:
    """Normalize a string for fuzzy comparison."""
    return re.sub(r"[-_ ]", "", s).lower()


def _subsequence_score(query: str, candidate: str) -> float:
    """Score how well query characters appear in order within candidate.

    Returns a value between 0.0 and 1.0.
    1.0 means all query chars found in order (perfect subsequence).
    Bonus for consecutive matches (tighter grouping).
    """
    if not query:
        return 0.0

    q = query.lower()
    c = candidate.lower()
    qi = 0
    consecutive = 0
    max_consecutive = 0
    last_pos = -2

    for ci, ch in enumerate(c):
        if qi < len(q) and ch == q[qi]:
            if ci == last_pos + 1:
                consecutive += 1
            else:
                consecutive = 1
            max_consecutive = max(max_consecutive, consecutive)
            last_pos = ci
            qi += 1

    if qi == 0:
        return 0.0

    matched_ratio = qi / len(q)
    # Bonus for consecutive matches (tighter = better)
    consecutive_bonus = max_consecutive / len(q) * 0.3
    return min(1.0, matched_ratio + consecutive_bonus)


def fuzzy_score(query: str, candidate: str) -> float:
    """Score how well a query matches a candidate string.

    Combines multiple heuristics:
    - Exact match (normalized)
    - Prefix match
    - Subsequence match (characters in order)
    - Length similarity

    Returns a score between 0.0 and 100.0. Higher is better.

    Examples:
        fuzzy_score("Sodum", "Sodium")       -> ~75  (close match)
        fuzzy_score("Sodium", "Sodium")       -> 100  (exact)
        fuzzy_score("sodium", "Sodium Extra") -> ~60  (partial)
        fuzzy_score("Diumlon", "Sodium")      -> ~20  (weak)
    """
    if not query or not candidate:
        return 0.0

    q_norm = _normalize(query)
    c_norm = _normalize(candidate)

    if not q_norm or not c_norm:
        return 0.0

    # Exact match
    if q_norm == c_norm:
        return 100.0

    score = 0.0

    # Prefix match: query is prefix of candidate or vice versa
    if c_norm.startswith(q_norm):
        score = max(score, 80.0 + 15.0 * (len(q_norm) / len(c_norm)))
    elif q_norm.startswith(c_norm):
        score = max(score, 70.0 + 10.0 * (len(c_norm) / len(q_norm)))

    # Contains: query is a substring of candidate
    if q_norm in c_norm:
        position_bonus = 10.0 * (1.0 - c_norm.index(q_norm) / len(c_norm))
        score = max(score, 60.0 + position_bonus)

    # Subsequence matching (characters in order)
    sub_score = _subsequence_score(q_norm, c_norm)
    score = max(score, sub_score * 70.0)

    # Character overlap ratio (bag of characters)
    q_chars = set(q_norm)
    c_chars = set(c_norm)
    if q_chars:
        overlap = len(q_chars & c_chars) / len(q_chars)
        score = max(score, overlap * 40.0)

    # Length penalty: very different lengths reduce score
    len_ratio = min(len(q_norm), len(c_norm)) / max(len(q_norm), len(c_norm))
    score *= (0.5 + 0.5 * len_ratio)

    return round(min(100.0, score), 1)


def fuzzy_match(
    query: str,
    candidates: List[Tuple[str, str]],
    threshold: float = 30.0,
    max_results: int = 5,
) -> List[Tuple[str, str, float]]:
    """Find the best fuzzy matches for a query among candidates.

    Args:
        query: The search string.
        candidates: List of (id, display_name) tuples to match against.
        threshold: Minimum score to include in results (0-100).
        max_results: Maximum number of results to return.

    Returns:
        List of (id, display_name, score) tuples, sorted by score descending.
    """
    scored = []
    for cid, cname in candidates:
        # Score against both ID and display name, take the best
        score_id = fuzzy_score(query, cid)
        score_name = fuzzy_score(query, cname)
        best = max(score_id, score_name)
        if best >= threshold:
            scored.append((cid, cname, best))

    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:max_results]


def safe_rmtree(path: Path) -> None:
    """
    Safely remove directory tree, handling Windows permission errors with Git files.
    """
    def handle_remove_readonly(func, fpath, exc):
        """Error handler for Windows read-only files."""
        if not os.access(fpath, os.W_OK):
            # Try to make the file writable
            os.chmod(fpath, stat.S_IWUSR | stat.S_IREAD)
            func(fpath)
        else:
            raise

    try:
        shutil.rmtree(path, onerror=handle_remove_readonly)
    except Exception as e:
        print(f"\u26a0\ufe0f  Warning: Could not fully clean up {path}: {e}")
        print("   You may need to manually delete this directory.")
