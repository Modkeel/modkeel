"""Persistent user configuration for ModForge."""

import uuid
from pathlib import Path
from typing import Dict

import toml


class ModForgeConfig:
    """Persistent user config stored at ~/.modforge/config.toml."""

    CONFIG_DIR = Path.home() / ".modforge"
    CONFIG_FILE = CONFIG_DIR / "config.toml"

    def __init__(self):
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._data: Dict = {}
        self._is_first_run = not self.CONFIG_FILE.exists()
        self._load()

    def _load(self) -> None:
        if self.CONFIG_FILE.exists():
            try:
                self._data = toml.load(str(self.CONFIG_FILE))
            except Exception:
                self._data = {}
        # Ensure client_id exists
        if not self._data.get("client_id"):
            self._data["client_id"] = str(uuid.uuid4())
            self._save()

    def _save(self) -> None:
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            toml.dump(self._data, f)

    @property
    def sharing(self) -> str:
        """Data sharing preference: 'always', 'ask', or 'never'."""
        return self._data.get("sharing", "ask")

    @sharing.setter
    def sharing(self, value: str) -> None:
        if value not in ("always", "ask", "never"):
            raise ValueError(f"Invalid sharing value: {value}")
        self._data["sharing"] = value
        self._save()

    @property
    def client_id(self) -> str:
        return self._data.get("client_id", "")

    @property
    def is_first_run(self) -> bool:
        return self._is_first_run

    def mark_prompted(self) -> None:
        """Mark that the first-run prompt has been shown."""
        self._data["_prompted"] = True
        self._is_first_run = False
        self._save()

    @property
    def was_prompted(self) -> bool:
        return self._data.get("_prompted", False)


def prompt_sharing_preference(config: ModForgeConfig) -> str:
    """
    Show the opt-in prompt on first run. Returns the user's choice.
    """
    print(f"\n{'='*60}")
    print("  Help improve ModForge for everyone!")
    print(f"{'='*60}")
    print()
    print("  When a mod compiles/loads successfully, ModForge")
    print("  can share anonymous compatibility data with the")
    print("  community. This helps other players find working")
    print("  mods faster.")
    print()
    print("  What's shared: mod name, version, MC version,")
    print("  loader, result (works/fails), Java version, OS.")
    print("  What's NOT shared: personal info, IP, file paths.")
    print()
    print("  [A] Always share (recommended)")
    print("  [K] Ask each time")
    print("  [N] Never share, don't ask again")
    print()

    while True:
        try:
            choice = input("  Your choice [A/K/N]: ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            choice = "K"
            break
        if choice in ("A", "K", "N"):
            break
        print("  Please enter A, K, or N.")

    mapping = {"A": "always", "K": "ask", "N": "never"}
    value = mapping.get(choice, "ask")
    config.sharing = value
    config.mark_prompted()
    print(f"\n  Saved: sharing = {value}")
    print(f"  Change anytime: edit {config.CONFIG_FILE}")
    print()
    return value
