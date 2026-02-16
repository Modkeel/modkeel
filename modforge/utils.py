"""Utility functions for ModForge."""

import logging
import os
import platform
import shutil
import stat
import sys
from pathlib import Path
from typing import Optional


logger = logging.getLogger("modforge")


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
        print(f"   You may need to manually delete this directory.")
