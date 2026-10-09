"""Shared test fixtures."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_release_manifest():
    """The target layer reads Mojang's release list (mappings.release_versions); tests never
    reach the network for it. A test that needs releases passes them or patches this."""
    with patch("modkeel.mappings.release_versions", return_value=()):
        yield


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    """Every test gets its own empty config folder: nothing saved on the machine running the
    tests (a real GitHub token, sharing choices) leaks into what a test sees, and no test
    writes there. A test that needs a config writes it, or points these elsewhere."""
    from modkeel.config import ModkeelConfig

    home = tmp_path_factory.mktemp("modkeel-home")
    monkeypatch.setattr(ModkeelConfig, "CONFIG_DIR", home)
    monkeypatch.setattr(ModkeelConfig, "CONFIG_FILE", home / "config.toml")
    monkeypatch.setattr(ModkeelConfig, "LEGACY_FILE", home / "no-legacy" / "config.toml")
