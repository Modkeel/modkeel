"""Shared test fixtures."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_release_manifest():
    """The target layer reads Mojang's release list (mappings.release_versions); tests never
    reach the network for it. A test that needs releases passes them or patches this."""
    with patch("modkeel.mappings.release_versions", return_value=()):
        yield
