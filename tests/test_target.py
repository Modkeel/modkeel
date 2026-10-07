"""Tests for modkeel/target.py (which Minecraft version the pack aims at) and the countdown
that announces a change of target (modkeel/commands/_shared.py)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from modkeel.target import (
    TargetOption,
    default_mode,
    fallback_decision,
    fallback_output,
    official_versions,
    propose_targets,
)


def modrinth_with(builds):
    """builds: {project id: [game versions of its loader builds]} (None: Modrinth failed)."""
    client = MagicMock()

    def project_versions(project_id, loader, game_version=None):
        found = builds.get(project_id)
        return None if found is None else [{"game_versions": found}]

    client.project_versions.side_effect = project_versions
    return client


class TestOfficialVersions:
    def test_release_versions_only(self):
        client = modrinth_with({"p": ["1.21.1", "25w14a", "1.21.11-rc1", "1.21.10"]})
        assert official_versions("p", "neoforge", client) == {"1.21.1", "1.21.10"}

    def test_unknown_when_modrinth_fails(self):
        assert official_versions("p", "neoforge", modrinth_with({})) is None


class TestProposeTargets:
    def test_pack_of_one_nearest_version_with_a_build(self):
        client = modrinth_with({"create": ["1.20.1", "1.21.1"]})
        options = propose_targets([("Create", "create")], "neoforge", "1.21.10", client,
                                  resolved=0)
        assert [o.mc_version for o in options] == ["1.21.1", "1.20.1"]
        assert options[0].summary == "MC 1.21.1 has an official build of Create"

    def test_most_mods_covered_wins_over_nearness(self):
        client = modrinth_with({"a": ["1.21.1", "1.21.9"], "b": ["1.21.1"], "c": ["1.21.1"]})
        mods = [("A", "a"), ("B", "b"), ("C", "c")]
        best = propose_targets(mods, "neoforge", "1.21.10", client, resolved=1)[0]
        assert (best.mc_version, best.covered, best.missing) == ("1.21.1", ["A", "B", "C"], [])

    def test_never_proposes_what_covers_no_more_than_was_resolved(self):
        """2 of 3 mods resolved on 1.21.10 (forks, older builds...): a version where only 2
        have a build would move the whole pack for nothing."""
        client = modrinth_with({"a": ["1.21.1"], "b": ["1.21.1"], "c": []})
        mods = [("A", "a"), ("B", "b"), ("C", "c")]
        assert propose_targets(mods, "neoforge", "1.21.10", client, resolved=2) == []
        assert propose_targets(mods, "neoforge", "1.21.10", client, resolved=1)[0].summary \
            == "MC 1.21.1 has official builds of 2 of the 3 mods (not C)"

    def test_equally_near_versions_prefer_the_older(self):
        client = modrinth_with({"a": ["1.21.9", "1.21.11"]})
        options = propose_targets([("A", "a")], "fabric", "1.21.10", client, resolved=0)
        assert [o.mc_version for o in options] == ["1.21.9", "1.21.11"]

    def test_mods_without_a_project_count_as_missing(self):
        client = modrinth_with({"a": ["26.2"]})
        best = propose_targets([("A", "a"), ("repo-only", None)], "fabric", "26.3", client,
                               resolved=0)[0]
        assert best.missing == ["repo-only"]

    def test_nothing_known(self):
        assert propose_targets([("A", None)], "fabric", "26.3", modrinth_with({}), 0) == []
        assert propose_targets([("A", "a")], "fabric", "26.3",
                               modrinth_with({"a": ["26.3"]}), 0) == []


class TestDecision:
    def test_modes(self):
        never_called = MagicMock(side_effect=AssertionError("no countdown"))
        assert fallback_decision("auto", False, never_called) is True
        assert fallback_decision("never", True, never_called) is False
        # ask without a terminal (CI, scripts) never changes target
        assert fallback_decision("ask", False, never_called) is False
        assert fallback_decision("ask", True, lambda: True) is True
        assert fallback_decision("ask", True, lambda: False) is False

    def test_default_mode(self):
        assert default_mode(True) == "ask" and default_mode(False) == "never"

    def test_fallback_output_is_its_own_folder(self):
        assert fallback_output(Path("out"), "1.21.1") == Path("out/mc-1.21.1")


class TestCountdown:
    def run(self, keys):
        from modkeel.commands import _shared
        keys = iter(keys)
        with patch.object(_shared, "_read_key", lambda timeout: next(keys, None)), \
                patch.object(_shared.time, "monotonic", side_effect=[0, 0, 1, 2, 3, 4, 5]):
            return _shared.countdown("Searching MC 1.21.1", 3)

    def test_timeout_continues(self):
        assert self.run([None, None, None]) is True

    def test_enter_starts_now(self):
        assert self.run(["\n"]) is True

    def test_n_stops(self):
        assert self.run([None, "n\n"]) is False


def test_option_summary_for_a_full_pack():
    option = TargetOption("1.21.1", ["A", "B"], [])
    assert option.summary == "MC 1.21.1 has official builds of 2 of the 2 mods"
